"""Immutable value objects for policy-change impact analysis.

All amounts are stored as :class:`decimal.Decimal` and only rendered as
fixed-point strings when a report is serialised.  Keeping the amount ``None``
is meaningful: it represents *unknown* eligibility, never a zero benefit.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
from dataclasses import dataclass
from typing import Any, Mapping

D = decimal.Decimal


def money(value: D | None) -> str | None:
    """Render an amount for a report without losing the unknown/zero contrast."""

    return None if value is None else format(value.quantize(D('0.01')), 'f')


def _clean(value: Any) -> Any:
    if isinstance(value, D):
        return money(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    return value


@dataclass(frozen=True)
class PolicyView:
    """One policy version with its validity window.

    ``version`` is the policy version, not the storage row version.  It is used
    to make every change traceable back to the exact policy version that
    produced it.
    """

    policy_id: str
    version: int
    effective_from: dt.date
    effective_to: dt.date | None
    monthly_cap: D
    income_cap: D
    exclusive_group: str
    status: str = 'active'

    def active_on(self, day: dt.date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)

    def row(self) -> dict[str, Any]:
        """Shape consumed by :class:`~housing_relief.accounting.settlement_book.SettlementBook`."""

        return {
            'policy_id': self.policy_id,
            'effective_from': self.effective_from,
            'effective_to': self.effective_to,
            'monthly_cap': str(self.monthly_cap),
            'income_cap': str(self.income_cap),
            'exclusive_group': self.exclusive_group,
            'status': self.status,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            'policy_id': self.policy_id,
            'policy_version': self.version,
            'effective_from': self.effective_from.isoformat(),
            'effective_to': None if self.effective_to is None else self.effective_to.isoformat(),
            'monthly_cap': money(self.monthly_cap),
            'income_cap': money(self.income_cap),
            'exclusive_group': self.exclusive_group,
            'status': self.status,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot()

    @property
    def identity(self) -> tuple[str, int]:
        return self.policy_id, self.version


@dataclass(frozen=True)
class LeaseView:
    """A lease with its validity window and household income.

    ``eligibility`` is one of ``eligible``, ``unknown`` or ``excluded``.
    Unknown leases carry their reasons and must never be aggregated as zero.
    """

    lease_id: str
    owner_id: str
    status: str
    lease_start: dt.date | None
    lease_end: dt.date | None
    rent: D | None
    household_income: D | None
    eligibility: str
    reasons: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return self.eligibility == 'eligible'

    def snapshot(self) -> dict[str, Any]:
        return {
            'lease_id': self.lease_id,
            'owner_id': self.owner_id,
            'status': self.status,
            'lease_start': None if self.lease_start is None else self.lease_start.isoformat(),
            'lease_end': None if self.lease_end is None else self.lease_end.isoformat(),
            'rent': money(self.rent),
            'household_income': money(self.household_income),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot() | {'eligibility': self.eligibility, 'reasons': list(self.reasons)}


@dataclass(frozen=True)
class HistoricalRow:
    """A historical settlement amount supplied for reconciliation.

    Any amount may be ``None``: an omitted field is *not* treated as zero.  A
    row with no readable amount is reported as unreadable instead of silently
    changing the reconciliation result.
    """

    lease_id: str
    period: str | None
    benefit: D | None
    payable: D | None
    gross: D | None
    problems: tuple[str, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            'lease_id': self.lease_id,
            'period': self.period,
            'benefit': money(self.benefit),
            'payable': money(self.payable),
            'gross': money(self.gross),
            'problems': list(self.problems),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot()


@dataclass(frozen=True)
class MonthSlice:
    """One settlement month of one lease."""

    period: str
    fraction: D
    gross: D
    benefit: D
    payable: D
    balanced: bool
    policies: tuple[PolicyView, ...]

    def snapshot(self) -> dict[str, Any]:
        return {
            'period': self.period,
            'fraction': format(self.fraction, 'f'),
            'gross': money(self.gross),
            'benefit': money(self.benefit),
            'payable': money(self.payable),
            'balanced': self.balanced,
            'policies': [policy.snapshot() for policy in self.policies],
        }

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot()


@dataclass(frozen=True)
class PolicyChangeCause:
    """The policy version change responsible for a lease-level difference."""

    policy_id: str
    exclusive_group: str
    change: str
    before: Mapping[str, Any] | None
    after: Mapping[str, Any] | None
    fields: tuple[str, ...]
    periods: tuple[str, ...]

    def snapshot(self) -> dict[str, Any]:
        return {
            'policy_id': self.policy_id,
            'exclusive_group': self.exclusive_group,
            'change': self.change,
            'before': None if self.before is None else _clean(self.before),
            'after': None if self.after is None else _clean(self.after),
            'fields': list(self.fields),
            'periods': list(self.periods),
        }

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot()


@dataclass(frozen=True)
class LeaseImpact:
    lease_id: str
    owner_id: str
    eligibility: str
    comparable: bool
    direction: str
    lease_timing: str
    income_band: str
    baseline_benefit: D | None
    candidate_benefit: D | None
    benefit_delta: D | None
    baseline_payable: D | None
    candidate_payable: D | None
    payable_delta: D | None
    reasons: tuple[str, ...]
    causes: tuple[PolicyChangeCause, ...] = ()
    baseline_months: tuple[MonthSlice, ...] = ()
    candidate_months: tuple[MonthSlice, ...] = ()

    def snapshot(self) -> dict[str, Any]:
        return {
            'lease_id': self.lease_id,
            'owner_id': self.owner_id,
            'eligibility': self.eligibility,
            'comparable': self.comparable,
            'direction': self.direction,
            'lease_timing': self.lease_timing,
            'income_band': self.income_band,
            'baseline_benefit': money(self.baseline_benefit),
            'candidate_benefit': money(self.candidate_benefit),
            'benefit_delta': money(self.benefit_delta),
            'baseline_payable': money(self.baseline_payable),
            'candidate_payable': money(self.candidate_payable),
            'payable_delta': money(self.payable_delta),
            'reasons': list(self.reasons),
            'causes': [cause.snapshot() for cause in self.causes],
            'baseline_months': [month.snapshot() for month in self.baseline_months],
            'candidate_months': [month.snapshot() for month in self.candidate_months],
        }

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot()


@dataclass(frozen=True)
class PolicyImpactReport:
    analysis_id: str
    input_fingerprint: str
    baseline_policies: tuple[PolicyView, ...]
    candidate_policies: tuple[PolicyView, ...]
    leases: tuple[LeaseImpact, ...]
    summary: Mapping[str, Any]
    strata: Mapping[str, Any]
    policy_changes: tuple[Mapping[str, Any], ...]
    reconciliation: Mapping[str, Any]
    checks: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            'analysis_id': self.analysis_id,
            'input_fingerprint': self.input_fingerprint,
            'baseline_policies': [policy.snapshot() for policy in self.baseline_policies],
            'candidate_policies': [policy.snapshot() for policy in self.candidate_policies],
            'leases': [lease.snapshot() for lease in self.leases],
            'summary': _clean(self.summary),
            'strata': _clean(self.strata),
            'policy_changes': _clean(self.policy_changes),
            'reconciliation': _clean(self.reconciliation),
            'checks': _clean(self.checks),
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, indent=indent)

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __contains__(self, key: str) -> bool:
        return key in self.as_dict()

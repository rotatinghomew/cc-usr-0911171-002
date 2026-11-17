"""Read-only before/after analysis for a housing-relief policy adjustment.

The analyser recomputes the *same* batch of leases twice -- once with the
baseline policy set and once with the candidate policy set -- using each lease's
own validity window and household income.  It reports:

* per-lease benefit and tenant-out-of-pocket changes,
* the resulting classification (increased / decreased / unchanged),
* stratified summaries by owner, income band, lease timing and direction,
* the exact policy versions responsible for every change,
* a reconciliation of the baseline recomputation with the original settlement
  basis (the production :class:`SettlementBook`, reached through
  :class:`SettlementEngine`) and with any supplied historical settlement rows.

Guarantees:

* leases with unknown eligibility (missing income, missing rent, missing or
  invalid window, unknown lease id, non-effective status, ...) are reported as
  ``unknown`` and are never folded into a zero amount; their lease ids are
  listed and excluded from monetary totals.  The same applies to historical
  rows that are missing or unreadable.
* leases known not to apply to the analysis window are reported as
  ``not_in_period`` (a real, explained zero), distinct from ``unknown``.
* mutually exclusive policies still select at most one policy per
  ``exclusive_group``; selection is delegated to ``SettlementBook``, so the
  original rule and tie-breaking order are preserved.
* nothing here writes formal payments, settlement entries or historical
  results.  The optional :class:`PolicyImpactService` only stores its own
  analysis snapshot and idempotency response.
* exact duplicate rows are collapsed, conflicting duplicates are rejected and
  all collections are sorted by stable identities.  Re-ordering the input or
  submitting the same request again therefore yields the same conclusion; a
  reused request key with different content is rejected rather than silently
  overwritten.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..accounting.settlement_book import SettlementBook
from ..contracts import canonical_json
from ..settlement.engine import SettlementEngine
from .models import (
    D,
    HistoricalRow,
    LeaseImpact,
    LeaseView,
    MonthSlice,
    PolicyChangeCause,
    PolicyImpactReport,
    PolicyView,
    money,
)

MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')
MAX_MONTHS = 120
DEFAULT_INCOME_EDGES = ('5000', '10000', '20000')
MAX_TOLERANCE = D('1000000')
UNSETTLED_STATUSES = {'cancelled', 'completed'}
UNKNOWN_STATUSES = {'draft', 'suspended'}


class PolicyImpactError(ValueError):
    """Raised when the requested impact analysis is ambiguous or malformed."""


# ---------------------------------------------------------------------------
# parsing helpers


def _text(value: Any, name: str, *, required: bool = True, default: str = '') -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PolicyImpactError(f'{name} is required')
        return default
    return ' '.join(str(value).split())


def _decimal(value: Any, name: str, *, required: bool = True, minimum: D | None = None,
             default: D | None = None) -> D | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PolicyImpactError(f'{name} is required')
        return default
    try:
        result = D(str(value))
    except decimal.InvalidOperation as exc:
        raise PolicyImpactError(f'{name} must be a decimal') from exc
    if not result.is_finite():
        raise PolicyImpactError(f'{name} must be finite')
    if minimum is not None and result < minimum:
        raise PolicyImpactError(f'{name} is below the minimum')
    return result


def _date(value: Any, name: str, *, required: bool = True) -> dt.date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise PolicyImpactError(f'{name} is required')
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise PolicyImpactError(f'{name} must be an ISO date') from exc


def _integer(value: Any, name: str, *, default: int | None = None, minimum: int = 1) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise PolicyImpactError(f'{name} is required')
        return default
    if isinstance(value, bool):
        raise PolicyImpactError(f'{name} must be an integer')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyImpactError(f'{name} must be an integer') from exc
    if result < minimum:
        raise PolicyImpactError(f'{name} is out of range')
    return result


def _period(value: Any) -> str:
    text = _text(value, 'period')
    match = MONTH_RE.fullmatch(text)
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise PolicyImpactError('period must use YYYY-MM')
    return text


def _month_start(period: str) -> dt.date:
    return dt.date.fromisoformat(period + '-01')


def _month_end(period: str) -> dt.date:
    start = _month_start(period)
    return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)


def _canonical(value: Any) -> str:
    def default(item: Any) -> Any:
        if isinstance(item, D):
            return format(item, 'f')
        if isinstance(item, (dt.date, dt.datetime)):
            return item.isoformat()
        return str(item)
    return canonical_json(value, default=default)


# ---------------------------------------------------------------------------
# normalization


def _normalize_policy(row: Mapping[str, Any]) -> PolicyView:
    policy_id = _text(row.get('policy_id') or row.get('id'), 'policy_id')
    version = _integer(row.get('version', row.get('policy_version')), 'policy version', default=1)
    start = _date(row.get('effective_from', row.get('start')), 'policy effective_from')
    end = _date(row.get('effective_to', row.get('end')), 'policy effective_to', required=False)
    if end is not None and end < start:
        raise PolicyImpactError(f'policy {policy_id} has a reversed effective window')
    cap = _decimal(row.get('monthly_cap', row.get('cap')), 'policy monthly_cap', minimum=D('0'))
    income_cap = _decimal(row.get('income_cap'), 'policy income_cap', required=False,
                          minimum=D('0'), default=D('999999999'))
    group = _text(row.get('exclusive_group'), 'policy exclusive_group', required=False, default='default')
    status = _text(row.get('status'), 'policy status', required=False, default='active')
    return PolicyView(
        policy_id=policy_id,
        version=version,
        effective_from=start,
        effective_to=end,
        monthly_cap=cap,
        income_cap=income_cap,
        exclusive_group=group,
        status=status,
    )


def _windows_overlap(left: PolicyView, right: PolicyView) -> bool:
    left_end = left.effective_to or dt.date.max
    right_end = right.effective_to or dt.date.max
    return left.effective_from <= right_end and right.effective_from <= left_end


def _normalize_policies(rows: Sequence[Mapping[str, Any]], side: str) -> tuple[PolicyView, ...]:
    if rows is None:
        raise PolicyImpactError(f'{side}_policies must be a list')
    if not isinstance(rows, (list, tuple)):
        raise PolicyImpactError(f'{side}_policies must be a list')
    by_version: dict[tuple[str, int], PolicyView] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise PolicyImpactError('policy rows must be mappings')
        policy = _normalize_policy(row)
        previous = by_version.get(policy.identity)
        if previous is not None:
            if previous.snapshot() != policy.snapshot():
                raise PolicyImpactError(f'conflicting duplicate policy version: {policy.identity!r}')
            continue
        by_version[policy.identity] = policy
    policies = sorted(by_version.values(), key=lambda item: (item.policy_id, item.version))
    # Two simultaneously active versions of one policy make selection
    # ambiguous; refuse instead of guessing.
    by_id: dict[str, list[PolicyView]] = defaultdict(list)
    for policy in policies:
        by_id[policy.policy_id].append(policy)
    for policy_id, versions in by_id.items():
        for index, left in enumerate(versions):
            for right in versions[index + 1:]:
                if _windows_overlap(left, right):
                    raise PolicyImpactError(
                        f'policy {policy_id} has overlapping versions {left.version} and {right.version}'
                    )
    return tuple(policies)


def _normalize_lease(row: Mapping[str, Any]) -> LeaseView:
    if not isinstance(row, Mapping):
        raise PolicyImpactError('each lease must be a mapping')
    reasons: list[str] = []
    unknown = False

    def date_field(*aliases: str) -> dt.date | None:
        nonlocal unknown
        raw = next((row[name] for name in aliases if name in row and row[name] not in (None, '')), None)
        if raw is None:
            return None
        try:
            return _date(raw, aliases[0])
        except PolicyImpactError:
            reasons.append(f'invalid_{aliases[0]}')
            unknown = True
            return None

    def amount_field(name: str, *aliases: str) -> D | None:
        nonlocal unknown
        raw = next((row[alias] for alias in aliases if alias in row and row[alias] not in (None, '')), None)
        if raw is None:
            return None
        try:
            return _decimal(raw, name, minimum=D('0'))
        except PolicyImpactError:
            reasons.append(f'invalid_{name}')
            unknown = True
            return None

    raw_id = row.get('lease_id', row.get('id'))
    owner_id = _text(row.get('owner_id'), 'owner_id', required=False)
    status = _text(row.get('status'), 'lease status', required=False, default='active')
    start = date_field('lease_start', 'effective_from', 'start')
    end = date_field('lease_end', 'effective_to', 'end')
    rent = amount_field('rent', 'rent', 'amount')
    income = amount_field('household_income', 'household_income', 'income')
    if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
        digest = hashlib.sha256(_canonical(dict(row)).encode('utf-8')).hexdigest()[:12]
        lease_id = f'unidentified-{digest}'
        reasons.append('missing_lease_id')
        unknown = True
    else:
        lease_id = _text(raw_id, 'lease_id')
    if not owner_id:
        reasons.append('missing_owner_id')
        unknown = True
    if start is None and 'invalid_lease_start' not in reasons:
        reasons.append('missing_lease_start')
        unknown = True
    if end is None and 'invalid_lease_end' not in reasons:
        reasons.append('missing_lease_end')
        unknown = True
    elif start is not None and end is not None and end < start:
        reasons.append('reversed_lease_window')
        unknown = True
    if rent is None and 'invalid_rent' not in reasons:
        reasons.append('missing_rent')
        unknown = True
    if income is None and 'invalid_household_income' not in reasons:
        reasons.append('missing_household_income')
        unknown = True
    eligible_status = True
    if status in UNSETTLED_STATUSES:
        eligible_status = False
        reasons.append(f'lease_not_in_force:{status}')
    elif status in UNKNOWN_STATUSES:
        eligible_status = False
        reasons.append(f'lease_status_not_established:{status}')
        unknown = True
    # A cancelled/completed lease is a *known* exclusion: it pays nothing no
    # matter what other fields are missing.  Missing fields on such a lease are
    # still reported as reasons, but the classification stays "excluded" so it
    # is never confused with an unknown-eligibility lease.
    if not eligible_status and status in UNSETTLED_STATUSES:
        eligibility = 'excluded'
    elif unknown:
        eligibility = 'unknown'
    else:
        eligibility = 'eligible'
    return LeaseView(
        lease_id=lease_id,
        owner_id=owner_id,
        status=status,
        lease_start=start,
        lease_end=end,
        rent=rent,
        household_income=income,
        eligibility=eligibility,
        reasons=tuple(sorted(reasons)),
    )


def _normalize_leases(rows: Sequence[Mapping[str, Any]]) -> tuple[LeaseView, ...]:
    if rows is None:
        raise PolicyImpactError('lease batch is required')
    if not isinstance(rows, (list, tuple)):
        raise PolicyImpactError('leases must be a list')
    deduped: dict[str, LeaseView] = {}
    for row in rows:
        lease = _normalize_lease(row)
        previous = deduped.get(lease.lease_id)
        if previous is not None:
            if previous.as_dict() != lease.as_dict():
                raise PolicyImpactError(f'conflicting duplicate lease: {lease.lease_id}')
            continue
        deduped[lease.lease_id] = lease
    return tuple(sorted(deduped.values(), key=lambda item: item.lease_id))


def _normalize_now(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        result = value
    else:
        try:
            result = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError as exc:
            raise PolicyImpactError('now must be an ISO datetime') from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result.astimezone(dt.timezone.utc)


def _normalize_periods(values: Sequence[Any] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple, set)):
        raise PolicyImpactError('periods must be a list of YYYY-MM values')
    return tuple(sorted({_period(value) for value in values}))


def _normalize_income_edges(values: Sequence[Any] | None) -> tuple[D, ...]:
    if not values:
        return tuple(D(value) for value in DEFAULT_INCOME_EDGES)
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise PolicyImpactError('income_band_edges must be a list of amounts')
    return tuple(sorted({_decimal(value, 'income band edge', minimum=D('0')) for value in values}))


def _normalize_historical(rows: Sequence[Mapping[str, Any]] | None) -> tuple[HistoricalRow, ...]:
    if not rows:
        return ()
    if not isinstance(rows, (list, tuple)):
        raise PolicyImpactError('historical must be a list of settlement rows')
    result: list[HistoricalRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise PolicyImpactError('historical settlement rows must be mappings')
        problems: list[str] = []
        raw_id = row.get('lease_id', row.get('id'))
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            problems.append('missing_lease_id')
            lease_id = 'unidentified-history-' + hashlib.sha256(_canonical(dict(row)).encode()).hexdigest()[:12]
        else:
            lease_id = _text(raw_id, 'historical lease_id')
        period = None
        if row.get('period') is not None:
            try:
                period = _period(row['period'])
            except PolicyImpactError:
                problems.append('invalid_period')
        values: dict[str, D | None] = {}
        for field_name, aliases in (
            ('benefit', ('benefit', 'subsidy', 'policy_benefit')),
            ('payable', ('payable', 'tenant_payable', 'tenant_cash', 'cash_due')),
            ('gross', ('gross', 'rent_receivable', 'rent')),
        ):
            raw = next((row[alias] for alias in aliases if alias in row and row[alias] is not None), None)
            if raw is None:
                values[field_name] = None
                continue
            try:
                values[field_name] = _decimal(raw, f'historical {field_name}', minimum=D('0'))
            except PolicyImpactError:
                values[field_name] = None
                problems.append(f'invalid_{field_name}')
        if all(values[field] is None for field in values):
            problems.append('no_amount_fields')
        result.append(HistoricalRow(
            lease_id=lease_id,
            period=period,
            benefit=values['benefit'],
            payable=values['payable'],
            gross=values['gross'],
            problems=tuple(sorted(set(problems))),
        ))
    # Historical rows are keyed by lease + period.  Identical duplicates are
    # collapsed; conflicting duplicates make reconciliation impossible.
    deduped: dict[tuple[str, str | None], tuple[str, HistoricalRow]] = {}
    for row in result:
        key = (row.lease_id, row.period)
        digest = hashlib.sha256(_canonical(row.snapshot()).encode()).hexdigest()
        previous = deduped.get(key)
        if previous is not None:
            if previous[0] != digest:
                raise PolicyImpactError(f'conflicting duplicate historical row: {key!r}')
            continue
        deduped[key] = (digest, row)
    return tuple(deduped[key][1] for key in sorted(deduped, key=lambda item: (item[0], item[1] or '')))


# ---------------------------------------------------------------------------
# computation


@dataclass(frozen=True)
class _LeaseComputation:
    lease: LeaseView
    periods: tuple[str, ...]
    baseline_months: tuple[MonthSlice, ...]
    candidate_months: tuple[MonthSlice, ...]
    problems: tuple[str, ...]

    @property
    def comparable(self) -> bool:
        return not self.problems and self.lease.eligibility == 'eligible' and bool(self.periods)

    @property
    def baseline_benefit(self) -> D | None:
        return None if self.problems else sum((month.benefit for month in self.baseline_months), D('0'))

    @property
    def candidate_benefit(self) -> D | None:
        return None if self.problems else sum((month.benefit for month in self.candidate_months), D('0'))

    @property
    def baseline_payable(self) -> D | None:
        return None if self.problems else sum((month.payable for month in self.baseline_months), D('0'))

    @property
    def candidate_payable(self) -> D | None:
        return None if self.problems else sum((month.payable for month in self.candidate_months), D('0'))


class PolicyImpactAnalyzer:
    """Recompute a lease batch under baseline and candidate policies."""

    analysis_version = 'policy-impact/1'

    def __init__(self, book: SettlementBook | None = None, engine: SettlementEngine | None = None):
        self.book = book or SettlementBook()
        self.engine = engine or SettlementEngine()

    # -- public ----------------------------------------------------------

    def analyze(self, request: Mapping[str, Any]) -> PolicyImpactReport:
        if not isinstance(request, Mapping):
            raise PolicyImpactError('analysis request must be a mapping')
        baseline = _normalize_policies(self._policy_rows(request, 'baseline'), 'baseline')
        candidate = _normalize_policies(self._policy_rows(request, 'candidate'), 'candidate')
        leases = _normalize_leases(self._lease_rows(request))
        periods = _normalize_periods(request.get('periods'))
        income_edges = _normalize_income_edges(request.get('income_band_edges'))
        tolerance = _decimal(request.get('tolerance'), 'tolerance', required=False,
                             minimum=D('0'), default=D('0.00'))
        if tolerance > MAX_TOLERANCE:
            raise PolicyImpactError('tolerance is unreasonably large')
        historical = _normalize_historical(request.get('historical') or request.get('history'))
        _normalize_now(request.get('now'))  # validated; the analysis itself is time-invariant

        computations = tuple(self._compute(lease, baseline, candidate, periods) for lease in leases)
        impacts = tuple(self._impact(computation, income_edges, baseline, candidate)
                         for computation in computations)
        policy_changes = self._policy_changes(impacts, baseline, candidate)
        reconciliation = self._reconcile(computations, historical, tolerance)
        checks = self._checks(computations, baseline, candidate)
        summary = self._summary(impacts)
        strata = self._strata(impacts, income_edges)
        digest = self._request_fingerprint(
            baseline, candidate, leases, periods, income_edges, tolerance, historical,
        )
        return PolicyImpactReport(
            analysis_id='impact-' + digest[:24],
            input_fingerprint=digest,
            baseline_policies=baseline,
            candidate_policies=candidate,
            leases=impacts,
            summary=summary,
            strata=strata,
            policy_changes=policy_changes,
            reconciliation=reconciliation,
            checks=checks,
        )

    # -- request extraction ---------------------------------------------

    def _policy_rows(self, request: Mapping[str, Any], side: str) -> Sequence[Mapping[str, Any]]:
        aliases = {
            'baseline': ('baseline_policies', 'before_policies'),
            'candidate': ('candidate_policies', 'after_policies'),
        }[side]
        direct = None
        for name in aliases:
            if request.get(name) is not None:
                direct = request[name]
                break
        if direct is None and isinstance(request.get(side), Mapping):
            direct = request[side].get('policies')
        if direct is None and isinstance(request.get(f'{side}_context'), Mapping):
            direct = request[f'{side}_context'].get('policies')
        if direct is None:
            raise PolicyImpactError(f'{aliases[0]} is required')
        if not isinstance(direct, (list, tuple)):
            raise PolicyImpactError(f'{aliases[0]} must be a list')
        return direct

    def _lease_rows(self, request: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        direct = request.get('leases', request.get('items'))
        if direct is None:
            raise PolicyImpactError('leases is required')
        if not isinstance(direct, (list, tuple)):
            raise PolicyImpactError('leases must be a list')
        return direct

    # -- per-lease computation ------------------------------------------

    def _lease_periods(self, lease: LeaseView, periods: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return (applicable periods, reasons preventing computation)."""

        if lease.eligibility == 'unknown':
            return (), lease.reasons
        if lease.eligibility == 'excluded':
            # A known, explained exclusion -- not an unknown.
            return (), ()
        if lease.lease_start is None or lease.lease_end is None:
            return (), ('missing_lease_window',)
        if periods:
            applicable = tuple(
                period for period in periods
                if _month_start(period) <= lease.lease_end and lease.lease_start <= _month_end(period)
            )
            return applicable, ()
        months: list[str] = []
        cursor = lease.lease_start.replace(day=1)
        last = lease.lease_end.replace(day=1)
        while cursor <= last:
            if len(months) >= MAX_MONTHS:
                return (), ('lease_window_too_long',)
            months.append(cursor.strftime('%Y-%m'))
            cursor = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        return tuple(months), ()

    def _compute(self, lease: LeaseView, baseline: Sequence[PolicyView],
                 candidate: Sequence[PolicyView], periods: tuple[str, ...]) -> _LeaseComputation:
        applicable, reasons = self._lease_periods(lease, periods)
        if reasons:
            return _LeaseComputation(lease, (), (), (), tuple(sorted(set(reasons))))
        if not applicable:
            return _LeaseComputation(lease, (), (), (), ())
        baseline_months: list[MonthSlice] = []
        candidate_months: list[MonthSlice] = []
        problems: list[str] = []
        for period in applicable:
            for side, policies, sink in (('baseline', baseline, baseline_months),
                                         ('candidate', candidate, candidate_months)):
                try:
                    slice_ = self._settle_month(lease, period, policies)
                except PolicyImpactError as exc:
                    problems.append(f'{side}:{period}:{exc}')
                    continue
                if not slice_.balanced:
                    problems.append(f'{side}:{period}:journal_not_balanced')
                if slice_.payable < 0:
                    problems.append(f'{side}:{period}:negative_payable')
                sink.append(slice_)
        return _LeaseComputation(lease, applicable, tuple(baseline_months), tuple(candidate_months),
                                 tuple(sorted(set(problems))))

    def _settle_month(self, lease: LeaseView, period: str, policies: Sequence[PolicyView]) -> MonthSlice:
        start = max(lease.lease_start, _month_start(period))
        end = min(lease.lease_end, _month_end(period))
        claim = {
            'period': period,
            'rent': str(lease.rent),
            'lease_start': start.isoformat(),
            'lease_end': end.isoformat(),
            'household_income': str(lease.household_income),
        }
        facts = self.book.settle(claim, {'policies': [policy.row() for policy in policies]})
        selected_ids = list(facts.get('policies') or ())
        # The book selects by policy id, so a policy that has several
        # successive (non-overlapping) versions in one set must be resolved to
        # the version that is active on the same day the book uses: the first
        # day of the settlement month.
        selection_day = _month_start(period)
        selected: list[PolicyView] = []
        for policy_id in selected_ids:
            candidates = [policy for policy in policies
                          if policy.policy_id == policy_id and policy.active_on(selection_day)]
            if len(candidates) != 1:
                raise PolicyImpactError(f'cannot resolve active version of policy {policy_id} in {period}')
            selected.append(candidates[0])
        if len({policy.policy_id for policy in selected}) != len(selected):
            raise PolicyImpactError('policy selected more than once')
        fraction = D(str(facts['fraction']))
        gross = D(str(facts['gross']))
        benefit = D(str(facts['benefit']))
        payable = D(str(facts['payable']))
        # Guard against a book that silently returns a partial or unbalanced
        # result: the analysis must reproduce the original settlement basis.
        if gross != (lease.rent * fraction).quantize(D('0.01')):
            raise PolicyImpactError(f'gross mismatch in {period}')
        if (benefit + payable).quantize(D('0.01')) != gross:
            raise PolicyImpactError(f'benefit and payable do not conserve gross in {period}')
        if benefit < 0 or benefit > gross:
            raise PolicyImpactError(f'benefit outside gross in {period}')
        return MonthSlice(
            period=period,
            fraction=fraction,
            gross=gross,
            benefit=benefit,
            payable=payable,
            balanced=bool(facts.get('balanced')),
            policies=tuple(selected),
        )

    # -- presentation ----------------------------------------------------

    def _impact(self, computation: _LeaseComputation, income_edges: tuple[D, ...],
                baseline: Sequence[PolicyView], candidate: Sequence[PolicyView]) -> LeaseImpact:
        lease = computation.lease
        if computation.problems or lease.eligibility == 'unknown':
            eligibility = 'unknown'
        elif lease.eligibility == 'excluded':
            eligibility = 'excluded'
        elif not computation.periods:
            eligibility = 'not_in_period'
        else:
            eligibility = 'eligible'
        comparable = eligibility == 'eligible'
        reasons = tuple(sorted(set(lease.reasons) | set(computation.problems)))
        causes: tuple[PolicyChangeCause, ...] = ()
        if comparable:
            baseline_benefit = computation.baseline_benefit
            candidate_benefit = computation.candidate_benefit
            baseline_payable = computation.baseline_payable
            candidate_payable = computation.candidate_payable
            benefit_delta = candidate_benefit - baseline_benefit
            payable_delta = candidate_payable - baseline_payable
            if benefit_delta > 0:
                direction = 'benefit_increased'
            elif benefit_delta < 0:
                direction = 'benefit_decreased'
            else:
                direction = 'unchanged'
            # Only leases whose entitlement actually moved carry a cause.  A
            # policy version can change without touching a lease (for example
            # when the cap never binds); such changes are still visible in the
            # report-level ``policy_changes`` but are not presented as the
            # cause of a change that did not happen.
            if benefit_delta:
                causes = self._causes(computation, baseline, candidate)
            timing = self._lease_timing(lease)
        elif eligibility == 'not_in_period':
            # A genuine zero: the lease is fully understood, it simply has no
            # month inside the requested analysis window.
            baseline_benefit = candidate_benefit = D('0.00')
            baseline_payable = candidate_payable = D('0.00')
            benefit_delta = payable_delta = D('0.00')
            direction = 'not_in_period'
            timing = self._lease_timing(lease)
        else:
            baseline_benefit = candidate_benefit = None
            baseline_payable = candidate_payable = None
            benefit_delta = payable_delta = None
            direction = eligibility
            timing = 'unknown' if eligibility == 'unknown' else self._lease_timing(lease)
        return LeaseImpact(
            lease_id=lease.lease_id,
            owner_id=lease.owner_id,
            eligibility=eligibility,
            comparable=comparable,
            direction=direction,
            lease_timing=timing,
            income_band=self._income_band(lease.household_income, income_edges),
            baseline_benefit=baseline_benefit,
            candidate_benefit=candidate_benefit,
            benefit_delta=benefit_delta,
            baseline_payable=baseline_payable,
            candidate_payable=candidate_payable,
            payable_delta=payable_delta,
            reasons=reasons,
            causes=causes,
            baseline_months=computation.baseline_months,
            candidate_months=computation.candidate_months,
        )

    def _lease_timing(self, lease: LeaseView) -> str:
        if lease.lease_start is None or lease.lease_end is None:
            return 'unknown'
        starts_first = lease.lease_start.day == 1
        ends_last = lease.lease_end == _month_end(lease.lease_end.strftime('%Y-%m'))
        if starts_first and ends_last:
            return 'full_month'
        if starts_first:
            return 'ends_mid_month'
        if ends_last:
            return 'starts_mid_month'
        return 'partial_both'

    def _income_band(self, income: D | None, edges: tuple[D, ...]) -> str:
        if income is None:
            return 'unknown'
        lower: D | None = None
        for edge in edges:
            if income < edge:
                return f'below_{money(edge)}' if lower is None else f'{money(lower)}_to_{money(edge)}'
            lower = edge
        return f'at_or_above_{money(edges[-1])}' if edges else 'all'

    def _causes(self, computation: _LeaseComputation, baseline: Sequence[PolicyView],
                candidate: Sequence[PolicyView]) -> tuple[PolicyChangeCause, ...]:
        baseline_by_period = {month.period: month for month in computation.baseline_months}
        candidate_by_period = {month.period: month for month in computation.candidate_months}
        aggregated: dict[tuple[str, int | None, str, str], dict[str, Any]] = {}

        def add(change: str, before: PolicyView | None, after: PolicyView | None,
                group: str, period: str) -> None:
            anchor = after or before
            key = (anchor.policy_id, None if after is None else after.version, group, change)
            entry = aggregated.get(key)
            if entry is None:
                entry = {'before': before, 'after': after, 'periods': set(), 'group': group,
                         'policy_id': anchor.policy_id, 'change': change}
                aggregated[key] = entry
            entry['periods'].add(period)

        def candidate_in_group(group: str, period: str) -> PolicyView | None:
            day = _month_start(period)
            available = [policy for policy in candidate
                         if policy.exclusive_group == group and policy.active_on(day)]
            if not available:
                return None
            return min(available, key=lambda policy: (policy.version, policy.policy_id))

        for period in sorted(set(baseline_by_period) | set(candidate_by_period)):
            before_groups = {policy.exclusive_group: policy
                             for policy in baseline_by_period[period].policies} if period in baseline_by_period else {}
            after_groups = {policy.exclusive_group: policy
                            for policy in candidate_by_period[period].policies} if period in candidate_by_period else {}
            for group in sorted(set(before_groups) | set(after_groups)):
                before = before_groups.get(group)
                after = after_groups.get(group)
                if before is None and after is None:
                    continue
                if before is None:
                    add('policy_activated', None, after, group, period)
                elif after is None:
                    # The group has no selected policy after the change.  If a
                    # candidate policy exists but no longer qualifies (for
                    # example a lowered income cap), point at the exact
                    # candidate version that made it ineligible.
                    replacement = candidate_in_group(group, period)
                    if replacement is not None:
                        add('policy_not_applicable', before, replacement, group, period)
                    else:
                        add('policy_deactivated', before, None, group, period)
                elif before.policy_id != after.policy_id:
                    add('policy_deselected', before, None, group, period)
                    add('policy_selected', None, after, group, period)
                elif before.version != after.version or before.snapshot() != after.snapshot():
                    add('policy_version_changed', before, after, group, period)
        causes = []
        for entry in aggregated.values():
            causes.append(PolicyChangeCause(
                policy_id=entry['policy_id'],
                exclusive_group=entry['group'],
                change=entry['change'],
                before=None if entry['before'] is None else entry['before'].snapshot(),
                after=None if entry['after'] is None else entry['after'].snapshot(),
                fields=tuple(sorted(self._policy_fields(entry['before'], entry['after']))),
                periods=tuple(sorted(entry['periods'])),
            ))
        return tuple(sorted(causes, key=lambda item: (
            item.policy_id, item.exclusive_group, item.change,
            (item.after or item.before or {}).get('policy_version', 0),
        )))

    def _policy_fields(self, before: PolicyView | None, after: PolicyView | None) -> set[str]:
        if before is None or after is None:
            return {'selection'}
        fields = set()
        for name in ('monthly_cap', 'income_cap', 'effective_from', 'effective_to',
                     'exclusive_group', 'status'):
            if getattr(before, name) != getattr(after, name):
                fields.add(name)
        if not fields and before.version != after.version:
            fields.add('version')
        if not fields and before.snapshot() != after.snapshot():
            fields.add('content')
        return fields

    def _policy_changes(self, impacts: Sequence[LeaseImpact], baseline: Sequence[PolicyView],
                        candidate: Sequence[PolicyView]) -> tuple[Mapping[str, Any], ...]:
        changes: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        affected: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        amount_changed: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for impact in impacts:
            for cause in impact.causes:
                version = str((cause.after or cause.before or {}).get('policy_version', ''))
                key = (cause.policy_id, cause.change, version)
                affected[key].add(impact.lease_id)
                if impact.benefit_delta:
                    amount_changed[key].add(impact.lease_id)
                if key in seen:
                    continue
                seen.add(key)
                entry = cause.snapshot()
                entry['_key'] = key
                changes.append(entry)
        for entry in changes:
            key = entry.pop('_key')
            entry['affected_leases'] = sorted(affected[key])
            entry['amount_changed_leases'] = sorted(amount_changed[key])
        # Report policies that are part of only one set even when no lease was
        # affected: finance must see the whole adjustment, not just impacts.
        # A version bump of an existing policy is already covered by the
        # ``policy_version_changed`` causes; only genuinely new/removed policy
        # ids are listed here.
        baseline_ids = {policy.policy_id for policy in baseline}
        candidate_ids = {policy.policy_id for policy in candidate}
        for policy in candidate:
            if policy.policy_id in baseline_ids:
                continue
            changes.append({
                'policy_id': policy.policy_id,
                'exclusive_group': policy.exclusive_group,
                'change': 'policy_added',
                'before': None,
                'after': policy.snapshot(),
                'fields': ['selection'],
                'periods': [],
                'affected_leases': [],
                'amount_changed_leases': [],
            })
        for policy in baseline:
            if policy.policy_id in candidate_ids:
                continue
            changes.append({
                'policy_id': policy.policy_id,
                'exclusive_group': policy.exclusive_group,
                'change': 'policy_removed',
                'before': policy.snapshot(),
                'after': None,
                'fields': ['selection'],
                'periods': [],
                'affected_leases': [],
                'amount_changed_leases': [],
            })
        # A policy that exists on both sides but whose versions differ and that
        # never produced a month-level cause (for example because its window
        # falls outside the analysis periods) still has to be traceable.
        version_changed = {item['policy_id'] for item in changes if item['periods']}
        baseline_by_id: dict[str, list[PolicyView]] = defaultdict(list)
        candidate_by_id: dict[str, list[PolicyView]] = defaultdict(list)
        for policy in baseline:
            baseline_by_id[policy.policy_id].append(policy)
        for policy in candidate:
            candidate_by_id[policy.policy_id].append(policy)
        for policy_id in sorted(set(baseline_by_id) & set(candidate_by_id)):
            if policy_id in version_changed:
                continue
            before_versions = [policy.version for policy in baseline_by_id[policy_id]]
            after_versions = [policy.version for policy in candidate_by_id[policy_id]]
            if before_versions == after_versions:
                continue
            before = baseline_by_id[policy_id][-1]
            after = candidate_by_id[policy_id][-1]
            changes.append({
                'policy_id': policy_id,
                'exclusive_group': after.exclusive_group,
                'change': 'policy_version_changed',
                'before': before.snapshot(),
                'after': after.snapshot(),
                'fields': sorted(self._policy_fields(before, after)),
                'periods': [],
                'affected_leases': [],
                'amount_changed_leases': [],
            })
        return tuple(sorted(changes, key=lambda item: (
            item['policy_id'], item['change'],
            str((item.get('after') or item.get('before') or {}).get('policy_version', '')),
        )))

    # -- summaries -------------------------------------------------------

    def _summary(self, impacts: Sequence[LeaseImpact]) -> dict[str, Any]:
        eligible = [impact for impact in impacts if impact.comparable]
        unknown = [impact for impact in impacts if impact.eligibility == 'unknown']
        excluded = [impact for impact in impacts if impact.eligibility == 'excluded']
        not_in_period = [impact for impact in impacts if impact.eligibility == 'not_in_period']
        payable_up = [impact for impact in eligible if (impact.payable_delta or D('0')) > 0]
        payable_down = [impact for impact in eligible if (impact.payable_delta or D('0')) < 0]
        by_owner: dict[str, D] = defaultdict(lambda: D('0'))
        for impact in payable_up:
            by_owner[impact.owner_id] += impact.payable_delta or D('0')
        return {
            'lease_count': len(impacts),
            'eligible_leases': len(eligible),
            'unknown_leases': len(unknown),
            'excluded_leases': len(excluded),
            'not_in_period_leases': len(not_in_period),
            'unknown_lease_ids': sorted(impact.lease_id for impact in unknown),
            'excluded_lease_ids': sorted(impact.lease_id for impact in excluded),
            'not_in_period_lease_ids': sorted(impact.lease_id for impact in not_in_period),
            'treated_as_zero': False,
            'unknown_leases_excluded_from_totals': len(unknown),
            'directions': dict(sorted(Counter(impact.direction for impact in impacts).items())),
            'baseline_benefit_total': money(sum((impact.baseline_benefit for impact in eligible), D('0'))),
            'candidate_benefit_total': money(sum((impact.candidate_benefit for impact in eligible), D('0'))),
            'benefit_delta_total': money(sum((impact.benefit_delta for impact in eligible), D('0'))),
            'baseline_payable_total': money(sum((impact.baseline_payable for impact in eligible), D('0'))),
            'candidate_payable_total': money(sum((impact.candidate_payable for impact in eligible), D('0'))),
            'payable_delta_total': money(sum((impact.payable_delta for impact in eligible), D('0'))),
            'benefit_increased_leases': sum(impact.direction == 'benefit_increased' for impact in eligible),
            'benefit_decreased_leases': sum(impact.direction == 'benefit_decreased' for impact in eligible),
            'benefit_unchanged_leases': sum(impact.direction == 'unchanged' for impact in eligible),
            'payable_increased_leases': len(payable_up),
            'payable_decreased_leases': len(payable_down),
            'payable_increased_lease_ids': sorted(impact.lease_id for impact in payable_up),
            'payable_decreased_lease_ids': sorted(impact.lease_id for impact in payable_down),
            'owners_with_payable_increase': [
                {'owner_id': owner_id, 'payable_delta': money(delta)}
                for owner_id, delta in sorted(by_owner.items())
            ],
            'owners_with_payable_increase_count': len(by_owner),
        }

    def _strata(self, impacts: Sequence[LeaseImpact], income_edges: tuple[D, ...]) -> dict[str, Any]:
        return {
            'by_owner': self._stratum(impacts, lambda impact: impact.owner_id or '(missing)'),
            'by_income_band': self._stratum(impacts, lambda impact: impact.income_band),
            'by_lease_timing': self._stratum(impacts, lambda impact: impact.lease_timing),
            'by_direction': self._stratum(impacts, lambda impact: impact.direction),
            'by_eligibility': self._stratum(impacts, lambda impact: impact.eligibility),
            'income_band_edges': [money(edge) for edge in income_edges],
        }

    def _stratum(self, impacts: Sequence[LeaseImpact], key) -> dict[str, Any]:
        groups: dict[str, list[LeaseImpact]] = defaultdict(list)
        for impact in impacts:
            groups[str(key(impact))].append(impact)
        result: dict[str, Any] = {}
        for name in sorted(groups):
            rows = groups[name]
            eligible = [row for row in rows if row.comparable]
            unknown = [row for row in rows if row.eligibility == 'unknown']
            result[name] = {
                'leases': len(rows),
                'eligible_leases': len(eligible),
                'unknown_leases': len(unknown),
                'excluded_leases': sum(row.eligibility == 'excluded' for row in rows),
                'not_in_period_leases': sum(row.eligibility == 'not_in_period' for row in rows),
                'benefit_increased': sum(row.direction == 'benefit_increased' for row in eligible),
                'benefit_decreased': sum(row.direction == 'benefit_decreased' for row in eligible),
                'benefit_unchanged': sum(row.direction == 'unchanged' for row in eligible),
                'payable_increased': sum((row.payable_delta or D('0')) > 0 for row in eligible),
                'payable_decreased': sum((row.payable_delta or D('0')) < 0 for row in eligible),
                'baseline_benefit_total': money(sum((row.baseline_benefit for row in eligible), D('0'))),
                'candidate_benefit_total': money(sum((row.candidate_benefit for row in eligible), D('0'))),
                'benefit_delta_total': money(sum((row.benefit_delta for row in eligible), D('0'))),
                'baseline_payable_total': money(sum((row.baseline_payable for row in eligible), D('0'))),
                'candidate_payable_total': money(sum((row.candidate_payable for row in eligible), D('0'))),
                'payable_delta_total': money(sum((row.payable_delta for row in eligible), D('0'))),
                'unknown_lease_ids': sorted(row.lease_id for row in unknown),
                'unknown_leases_excluded_from_totals': len(unknown),
                'lease_ids': sorted(row.lease_id for row in rows),
            }
        return result

    # -- checks and reconciliation --------------------------------------

    def _checks(self, computations: Sequence[_LeaseComputation], baseline: Sequence[PolicyView],
                candidate: Sequence[PolicyView]) -> dict[str, Any]:
        problems: list[str] = []
        month_count = 0
        selected_groups = Counter()
        for computation in computations:
            for month in computation.baseline_months + computation.candidate_months:
                month_count += 1
                if not month.balanced:
                    problems.append(f'{computation.lease.lease_id}:{month.period}:unbalanced')
                groups = [policy.exclusive_group for policy in month.policies]
                if len(groups) != len(set(groups)):
                    problems.append(f'{computation.lease.lease_id}:{month.period}:exclusive_group_violated')
                for policy in month.policies:
                    selected_groups[policy.policy_id] += 1
        engine_problems = self._engine_parity(computations, baseline, candidate)
        problems.extend(engine_problems)
        return {
            'settlement_months_recomputed': month_count,
            'settlement_book_balanced': not any(item.endswith(':unbalanced') for item in problems),
            'engine_parity': not engine_problems,
            'mutually_exclusive_groups_enforced': True,
            'policy_selection_counts': dict(sorted(selected_groups.items())),
            'formal_payments_written': 0,
            'historical_settlements_written': 0,
            'baseline_policy_ids': sorted({policy.policy_id for policy in baseline}),
            'candidate_policy_ids': sorted({policy.policy_id for policy in candidate}),
            'problems': sorted(set(problems)),
        }

    def _engine_parity(self, computations: Sequence[_LeaseComputation],
                       baseline: Sequence[PolicyView], candidate: Sequence[PolicyView]) -> list[str]:
        """Compare every recomputed month with the production settlement engine.

        ``SettlementEngine`` routes to ``SettlementBook`` only when the policy
        list is non-empty, so months settled with no applicable policy are
        checked directly against the book instead.
        """

        problems: list[str] = []
        for computation in computations:
            lease = computation.lease
            if not computation.comparable:
                continue
            for side, policies, months in (
                ('baseline', baseline, computation.baseline_months),
                ('candidate', candidate, computation.candidate_months),
            ):
                rows = [policy.row() for policy in policies]
                for month in months:
                    start = max(lease.lease_start, _month_start(month.period))
                    end = min(lease.lease_end, _month_end(month.period))
                    claim = {
                        'id': lease.lease_id,
                        'period': month.period,
                        'rent': str(lease.rent),
                        'lease_start': start.isoformat(),
                        'lease_end': end.isoformat(),
                        'household_income': str(lease.household_income),
                    }
                    if not rows:
                        expected_benefit = D('0')
                        if month.benefit != expected_benefit or month.payable != month.gross:
                            problems.append(f'{lease.lease_id}:{month.period}:{side}:book_mismatch')
                        continue
                    result = self.engine.evaluate(claim, {'policies': rows})
                    if not result.accepted:
                        problems.append(f'{lease.lease_id}:{month.period}:{side}:engine_rejected')
                        continue
                    if result.value != month.benefit:
                        problems.append(f'{lease.lease_id}:{month.period}:{side}:engine_benefit_mismatch')
                    facts = result.facts
                    if D(str(facts.get('payable'))) != month.payable:
                        problems.append(f'{lease.lease_id}:{month.period}:{side}:engine_payable_mismatch')
                    if D(str(facts.get('gross'))) != month.gross:
                        problems.append(f'{lease.lease_id}:{month.period}:{side}:engine_gross_mismatch')
                    engine_selected = tuple(facts.get('policies') or ())
                    book_selected = tuple(policy.policy_id for policy in month.policies)
                    if engine_selected != book_selected:
                        problems.append(f'{lease.lease_id}:{month.period}:{side}:engine_selection_mismatch')
        return sorted(set(problems))

    def _reconcile(self, computations: Sequence[_LeaseComputation],
                   historical: Sequence[HistoricalRow], tolerance: D) -> dict[str, Any]:
        if not historical:
            return {
                'status': 'not_provided',
                'balanced': None,
                'note': 'no historical settlement rows supplied; the baseline recomputation was '
                        'cross-checked against the production settlement engine instead',
                'historical_rows': 0,
                'matched': 0,
                'mismatched': 0,
                'missing_history': 0,
                'missing_leases': 0,
                'unreadable': 0,
                'problems': [],
                'totals': {},
                'tolerance': money(tolerance),
            }
        monthly: dict[tuple[str, str], dict[str, D]] = {}
        lease_totals: dict[str, dict[str, D]] = {}
        batch_ids = {computation.lease.lease_id for computation in computations}
        eligible_ids = set()
        for computation in computations:
            lease = computation.lease
            if not computation.comparable:
                continue
            eligible_ids.add(lease.lease_id)
            totals = {'benefit': D('0'), 'payable': D('0'), 'gross': D('0')}
            for month in computation.baseline_months:
                monthly[(lease.lease_id, month.period)] = {
                    'benefit': month.benefit,
                    'payable': month.payable,
                    'gross': month.gross,
                }
                for field_name in totals:
                    totals[field_name] += getattr(month, field_name)
            lease_totals[lease.lease_id] = totals
        aggregate_leases = {row.lease_id for row in historical if row.period is None and not row.problems}
        problems: list[dict[str, Any]] = []
        matched = mismatched = missing_leases = unreadable = 0
        historical_keys: set[tuple[str, str | None]] = set()
        totals = {'benefit': {'historical': D('0'), 'baseline': D('0')},
                  'payable': {'historical': D('0'), 'baseline': D('0')},
                  'gross': {'historical': D('0'), 'baseline': D('0')}}
        for row in historical:
            historical_keys.add((row.lease_id, row.period))
            if row.problems:
                unreadable += 1
                problems.append({'code': 'unreadable_history', 'lease_id': row.lease_id,
                                 'period': row.period, 'detail': list(row.problems)})
                continue
            if row.lease_id not in batch_ids:
                missing_leases += 1
                problems.append({'code': 'historical_lease_missing_from_batch',
                                 'lease_id': row.lease_id, 'period': row.period})
                continue
            if row.lease_id not in eligible_ids:
                problems.append({'code': 'historical_lease_not_comparable', 'lease_id': row.lease_id,
                                 'period': row.period,
                                 'detail': 'baseline eligibility is unknown or the lease is not in force'})
                continue
            if row.period is None:
                baseline = lease_totals[row.lease_id]
            else:
                baseline = monthly.get((row.lease_id, row.period))
                if baseline is None:
                    problems.append({'code': 'historical_period_not_in_baseline',
                                     'lease_id': row.lease_id, 'period': row.period})
                    continue
            fields = {}
            for field_name in ('benefit', 'payable', 'gross'):
                expected = getattr(row, field_name)
                if expected is None:
                    continue
                actual = baseline[field_name]
                totals[field_name]['historical'] += expected
                totals[field_name]['baseline'] += actual
                delta = abs(expected - actual)
                if delta > tolerance:
                    fields[field_name] = {'historical': money(expected), 'baseline': money(actual),
                                          'delta': money(delta)}
            if fields:
                mismatched += 1
                problems.append({'code': 'historical_amount_mismatch', 'lease_id': row.lease_id,
                                 'period': row.period, 'fields': fields,
                                 'tolerance': money(tolerance)})
            else:
                matched += 1
        missing_history = 0
        for key in sorted(monthly):
            if key[0] in aggregate_leases:
                continue
            if key not in historical_keys:
                missing_history += 1
                problems.append({'code': 'missing_history', 'lease_id': key[0], 'period': key[1],
                                 'detail': 'baseline month has no historical settlement row; '
                                           'it is not treated as zero'})
        totals = {
            field_name: {
                'historical': money(values['historical']),
                'baseline': money(values['baseline']),
                'difference': money(values['historical'] - values['baseline']),
            }
            for field_name, values in totals.items()
        }
        balanced = not problems
        return {
            'status': 'reconciled' if balanced else 'mismatch',
            'balanced': balanced,
            'historical_rows': len(historical),
            'matched': matched,
            'mismatched': mismatched,
            'missing_history': missing_history,
            'missing_leases': missing_leases,
            'unreadable': unreadable,
            'problems': sorted(problems, key=lambda item: (
                item.get('lease_id', ''), item.get('period') or '', item['code'],
            )),
            'totals': totals,
            'tolerance': money(tolerance),
        }

    # -- fingerprint -----------------------------------------------------

    def _request_fingerprint(self, baseline: Sequence[PolicyView], candidate: Sequence[PolicyView],
                             leases: Sequence[LeaseView], periods: Sequence[str],
                             income_edges: Sequence[D], tolerance: D,
                             historical: Sequence[HistoricalRow]) -> str:
        payload = {
            'analysis_version': self.analysis_version,
            'baseline_policies': [policy.snapshot() for policy in baseline],
            'candidate_policies': [policy.snapshot() for policy in candidate],
            'leases': [lease.as_dict() for lease in leases],
            'periods': list(periods),
            'income_band_edges': [money(edge) for edge in income_edges],
            'tolerance': money(tolerance),
            'historical': [row.snapshot() for row in historical],
        }
        return hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()

    def fingerprint(self, request: Mapping[str, Any]) -> str:
        """Return the canonical fingerprint without keeping the report."""

        return self.analyze(request).input_fingerprint


class PolicyImpactService:
    """Persistence/idempotency wrapper around :class:`PolicyImpactAnalyzer`.

    Only the analysis snapshot and the idempotency response are stored.  The
    service never writes ``payment`` or ``entry`` records and never modifies a
    historical settlement row.
    """

    scope = 'policy_impact'
    namespace = 'policy-impact'

    def __init__(self, database=None, analyzer: PolicyImpactAnalyzer | None = None):
        self.database = database
        self.analyzer = analyzer or PolicyImpactAnalyzer()

    def analyze(self, request: Mapping[str, Any]) -> PolicyImpactReport:
        return self.analyzer.analyze(request)

    def submit(self, request: Mapping[str, Any], request_key: str | None = None) -> dict[str, Any]:
        report = self.analyze(request)
        payload = report.as_dict()
        if self.database is None or not request_key:
            return payload
        digest = report.input_fingerprint
        recalled = self.database.recalled(self.scope, request_key, digest)
        if recalled is not None:
            return recalled
        with self.database.transaction() as connection:
            self.database.remember(connection, self.scope, request_key, digest, payload)
        self._persist_snapshot(report)
        return payload

    def _persist_snapshot(self, report: PolicyImpactReport) -> None:
        if self.database is None:
            return
        try:
            latest = self.database.load_snapshot(self.namespace, report.analysis_id)
        except Exception:
            latest = None
        if latest is not None and latest.get('fingerprint') == report.input_fingerprint:
            return
        self.database.save_snapshot(self.namespace, report.analysis_id, report.as_dict(),
                                    report.input_fingerprint)

    def history(self, analysis_id: str, version: int | None = None) -> dict[str, Any]:
        if self.database is None:
            raise PolicyImpactError('history requires a database')
        return self.database.load_snapshot(self.namespace, analysis_id, version)


__all__ = [
    'PolicyImpactAnalyzer',
    'PolicyImpactError',
    'PolicyImpactReport',
    'PolicyImpactService',
]

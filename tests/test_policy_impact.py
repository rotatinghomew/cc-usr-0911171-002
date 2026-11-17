import datetime as dt
import decimal
import json
import random
import tempfile
import unittest
from pathlib import Path

from housing_relief.analysis import PolicyImpactAnalyzer, PolicyImpactError, PolicyImpactService
from housing_relief.storage import ConflictError, Database

D = decimal.Decimal

BASELINE = {
    'policy_id': 'degree',
    'version': 1,
    'effective_from': '2026-01-01',
    'monthly_cap': '600',
    'income_cap': '10000',
    'exclusive_group': 'housing',
}
CANDIDATE = {
    **BASELINE,
    'version': 2,
    'monthly_cap': '450',
}


def lease(lease_id, **changes):
    value = {
        'lease_id': lease_id,
        'owner_id': 'person-1',
        'rent': '2400',
        'household_income': '5000',
        'lease_start': '2026-11-01',
        'lease_end': '2026-11-30',
        'status': 'active',
    }
    value.update(changes)
    return value


class PolicyImpactAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = PolicyImpactAnalyzer()

    def request(self, leases, **extra):
        payload = {
            'baseline_policies': [BASELINE],
            'candidate_policies': [CANDIDATE],
            'leases': leases,
            'periods': ['2026-11'],
        }
        payload.update(extra)
        return payload

    # -- per-lease recomputation ----------------------------------------

    def test_full_month_reduction_raises_tenant_payable(self):
        report = self.analyzer.analyze(self.request([lease('l1')]))
        item = report.as_dict()['leases'][0]
        self.assertEqual(item['eligibility'], 'eligible')
        self.assertEqual(item['baseline_benefit'], '600.00')
        self.assertEqual(item['candidate_benefit'], '450.00')
        self.assertEqual(item['benefit_delta'], '-150.00')
        self.assertEqual(item['baseline_payable'], '1800.00')
        self.assertEqual(item['candidate_payable'], '1950.00')
        self.assertEqual(item['payable_delta'], '150.00')
        self.assertEqual(item['direction'], 'benefit_decreased')
        summary = report.as_dict()['summary']
        self.assertEqual(summary['payable_increased_lease_ids'], ['l1'])
        self.assertEqual(summary['benefit_delta_total'], '-150.00')

    def test_mid_month_lease_is_prorated(self):
        report = self.analyzer.analyze(self.request([
            lease('l-mid', lease_start='2026-11-16', lease_end='2026-11-30', rent='3000'),
        ]))
        item = report.as_dict()['leases'][0]
        self.assertEqual(item['lease_timing'], 'starts_mid_month')
        self.assertEqual(item['baseline_benefit'], '300.00')
        self.assertEqual(item['candidate_benefit'], '225.00')
        self.assertEqual(item['baseline_payable'], '1200.00')
        self.assertEqual(item['candidate_payable'], '1275.00')
        self.assertEqual(item['baseline_months'][0]['fraction'], '0.5')

    def test_multi_month_lease_uses_each_month(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1', lease_start='2026-11-01', lease_end='2026-12-31')],
            periods=['2026-11', '2026-12'],
        ))
        item = report.as_dict()['leases'][0]
        self.assertEqual([month['period'] for month in item['baseline_months']],
                         ['2026-11', '2026-12'])
        self.assertEqual(item['baseline_benefit'], '1200.00')
        self.assertEqual(item['candidate_benefit'], '900.00')

    def test_default_periods_cover_the_lease_window(self):
        report = self.analyzer.analyze({
            'baseline_policies': [BASELINE],
            'candidate_policies': [CANDIDATE],
            'leases': [lease('l1', lease_start='2026-11-16', lease_end='2026-12-15')],
        })
        item = report.as_dict()['leases'][0]
        self.assertEqual([month['period'] for month in item['baseline_months']],
                         ['2026-11', '2026-12'])

    def test_lease_outside_period_is_a_known_zero(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1', lease_start='2026-09-01', lease_end='2026-09-30')],
        ))
        item = report.as_dict()['leases'][0]
        self.assertEqual(item['eligibility'], 'not_in_period')
        self.assertEqual(item['direction'], 'not_in_period')
        self.assertEqual(item['baseline_benefit'], '0.00')
        self.assertEqual(item['candidate_benefit'], '0.00')
        self.assertNotIn('l1', report.as_dict()['summary']['unknown_lease_ids'])

    # -- unknown eligibility is never zero -----------------------------

    def test_missing_income_is_unknown_not_zero(self):
        report = self.analyzer.analyze(self.request([lease('l1', household_income=None)]))
        data = report.as_dict()
        item = data['leases'][0]
        self.assertEqual(item['eligibility'], 'unknown')
        self.assertIsNone(item['baseline_benefit'])
        self.assertIsNone(item['candidate_benefit'])
        self.assertIsNone(item['payable_delta'])
        self.assertIn('missing_household_income', item['reasons'])
        self.assertEqual(data['summary']['unknown_lease_ids'], ['l1'])
        self.assertFalse(data['summary']['treated_as_zero'])
        self.assertEqual(data['summary']['baseline_benefit_total'], '0.00')
        self.assertEqual(data['summary']['eligible_leases'], 0)
        self.assertIn('l1', data['strata']['by_eligibility']['unknown']['unknown_lease_ids'])

    def test_unknown_lease_does_not_pollute_known_totals(self):
        report = self.analyzer.analyze(self.request([
            lease('l1'),
            lease('l2', household_income=''),
            lease('l3', rent=None),
        ]))
        summary = report.as_dict()['summary']
        self.assertEqual(summary['eligible_leases'], 1)
        self.assertEqual(summary['unknown_leases'], 2)
        self.assertEqual(summary['unknown_lease_ids'], ['l2', 'l3'])
        self.assertEqual(summary['baseline_benefit_total'], '600.00')
        self.assertEqual(summary['benefit_delta_total'], '-150.00')

    def test_missing_lease_id_is_reported_without_collapsing(self):
        report = self.analyzer.analyze(self.request([
            {'owner_id': 'p', 'rent': '100', 'household_income': '1000',
             'lease_start': '2026-11-01', 'lease_end': '2026-11-30'},
        ]))
        item = report.as_dict()['leases'][0]
        self.assertEqual(item['eligibility'], 'unknown')
        self.assertTrue(item['lease_id'].startswith('unidentified-'))

    def test_cancelled_lease_is_excluded_not_unknown(self):
        report = self.analyzer.analyze(self.request([lease('l1', status='cancelled')]))
        item = report.as_dict()['leases'][0]
        self.assertEqual(item['eligibility'], 'excluded')
        summary = report.as_dict()['summary']
        self.assertEqual(summary['excluded_lease_ids'], ['l1'])
        self.assertEqual(summary['unknown_lease_ids'], [])

    def test_reversed_window_is_unknown(self):
        report = self.analyzer.analyze(self.request([
            lease('l1', lease_start='2026-11-30', lease_end='2026-11-01'),
        ]))
        self.assertEqual(report.as_dict()['leases'][0]['eligibility'], 'unknown')

    # -- mutually exclusive policies -----------------------------------

    def test_exclusive_group_still_selects_one_policy(self):
        higher = {**BASELINE, 'policy_id': 'doctor', 'monthly_cap': '900'}
        candidate_higher = {**higher, 'version': 2, 'monthly_cap': '700'}
        report = self.analyzer.analyze({
            'baseline_policies': [BASELINE, higher],
            'candidate_policies': [CANDIDATE, candidate_higher],
            'leases': [lease('l1')],
            'periods': ['2026-11'],
        })
        item = report.as_dict()['leases'][0]
        self.assertEqual([policy['policy_id'] for policy in item['baseline_months'][0]['policies']],
                         ['doctor'])
        self.assertEqual(item['baseline_benefit'], '900.00')
        self.assertEqual(item['candidate_benefit'], '700.00')
        self.assertTrue(report.as_dict()['checks']['mutually_exclusive_groups_enforced'])

    def test_new_policy_that_takes_over_is_traceable(self):
        new_policy = {**BASELINE, 'policy_id': 'doctor', 'version': 1, 'monthly_cap': '900'}
        report = self.analyzer.analyze({
            'baseline_policies': [BASELINE],
            'candidate_policies': [CANDIDATE, new_policy],
            'leases': [lease('l1')],
            'periods': ['2026-11'],
        })
        data = report.as_dict()
        causes = data['leases'][0]['causes']
        self.assertTrue(any(cause['change'] == 'policy_selected' and cause['policy_id'] == 'doctor'
                            for cause in causes))
        changes = {(change['policy_id'], change['change']) for change in data['policy_changes']}
        self.assertIn(('doctor', 'policy_added'), changes)

    # -- traceability ---------------------------------------------------

    def test_cause_names_the_policy_version_and_field(self):
        report = self.analyzer.analyze(self.request([lease('l1')]))
        causes = report.as_dict()['leases'][0]['causes']
        self.assertEqual(len(causes), 1)
        cause = causes[0]
        self.assertEqual(cause['policy_id'], 'degree')
        self.assertEqual(cause['change'], 'policy_version_changed')
        self.assertEqual(cause['before']['policy_version'], 1)
        self.assertEqual(cause['after']['policy_version'], 2)
        self.assertEqual(cause['fields'], ['monthly_cap'])
        self.assertEqual(cause['periods'], ['2026-11'])
        change = report.as_dict()['policy_changes'][0]
        self.assertEqual(change['affected_leases'], ['l1'])
        self.assertEqual(change['amount_changed_leases'], ['l1'])

    def test_unchanged_lease_has_no_false_cause(self):
        # The cap never binds because the rent is too low.
        report = self.analyzer.analyze(self.request([lease('l1', rent='100')]))
        item = report.as_dict()['leases'][0]
        self.assertEqual(item['direction'], 'unchanged')
        self.assertEqual(item['causes'], [])
        # The policy version change is still reported at report level.
        changes = report.as_dict()['policy_changes']
        self.assertEqual([change['change'] for change in changes], ['policy_version_changed'])
        self.assertEqual(changes[0]['amount_changed_leases'], [])

    def test_lowered_income_cap_names_the_candidate_version(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1')],
            candidate_policies=[{**BASELINE, 'version': 2, 'income_cap': '4000'}],
        ))
        causes = report.as_dict()['leases'][0]['causes']
        self.assertEqual([cause['change'] for cause in causes], ['policy_not_applicable'])
        self.assertEqual(causes[0]['fields'], ['income_cap'])
        self.assertEqual(causes[0]['after']['policy_version'], 2)

    def test_policy_change_outside_the_window_is_still_traceable(self):
        old = {**BASELINE, 'policy_id': 'legacy', 'effective_from': '2025-01-01',
               'effective_to': '2025-12-31'}
        report = self.analyzer.analyze(self.request(
            [lease('l1')],
            baseline_policies=[BASELINE, old],
            candidate_policies=[CANDIDATE, {**old, 'version': 2, 'monthly_cap': '100'}],
        ))
        changes = {(change['policy_id'], change['change']) for change in report.as_dict()['policy_changes']}
        self.assertIn(('legacy', 'policy_version_changed'), changes)

    # -- reconciliation -------------------------------------------------

    def test_historical_rows_that_match_are_reconciled(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1'), lease('l2', owner_id='person-2', lease_start='2026-11-16',
                               lease_end='2026-11-30', rent='3000')],
            historical=[
                {'lease_id': 'l1', 'period': '2026-11', 'benefit': '600',
                 'payable': '1800', 'gross': '2400'},
                {'lease_id': 'l2', 'period': '2026-11', 'benefit': '300',
                 'payable': '1200', 'gross': '1500'},
            ],
        ))
        reconciliation = report.as_dict()['reconciliation']
        self.assertEqual(reconciliation['status'], 'reconciled')
        self.assertTrue(reconciliation['balanced'])
        self.assertEqual(reconciliation['matched'], 2)
        self.assertEqual(reconciliation['totals']['benefit']['difference'], '0.00')

    def test_historical_mismatch_is_flagged(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1')],
            historical=[{'lease_id': 'l1', 'period': '2026-11', 'benefit': '500'}],
        ))
        reconciliation = report.as_dict()['reconciliation']
        self.assertEqual(reconciliation['status'], 'mismatch')
        self.assertFalse(reconciliation['balanced'])
        self.assertEqual(reconciliation['problems'][0]['code'], 'historical_amount_mismatch')
        self.assertEqual(reconciliation['problems'][0]['fields']['benefit']['delta'], '100.00')

    def test_tolerance_absorbs_rounding_noise(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1')],
            tolerance='0.01',
            historical=[{'lease_id': 'l1', 'period': '2026-11', 'benefit': '600.01'}],
        ))
        self.assertEqual(report.as_dict()['reconciliation']['status'], 'reconciled')

    def test_missing_history_is_not_treated_as_zero(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1')],
            historical=[],
        ))
        reconciliation = report.as_dict()['reconciliation']
        self.assertEqual(reconciliation['status'], 'not_provided')
        self.assertIsNone(reconciliation['balanced'])
        report = self.analyzer.analyze(self.request(
            [lease('l1'), lease('l2', owner_id='p2')],
            historical=[{'lease_id': 'l1', 'period': '2026-11', 'benefit': '600'}],
        ))
        reconciliation = report.as_dict()['reconciliation']
        self.assertEqual(reconciliation['status'], 'mismatch')
        self.assertEqual(reconciliation['missing_history'], 1)
        self.assertEqual(reconciliation['problems'][0]['code'], 'missing_history')

    def test_history_for_unknown_lease_is_not_compared(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1', household_income=None)],
            historical=[{'lease_id': 'l1', 'period': '2026-11', 'benefit': '0'}],
        ))
        codes = {problem['code'] for problem in report.as_dict()['reconciliation']['problems']}
        self.assertIn('historical_lease_not_comparable', codes)

    def test_history_for_unknown_batch_lease_is_reported(self):
        report = self.analyzer.analyze(self.request(
            [lease('l1')],
            historical=[{'lease_id': 'missing', 'period': '2026-11', 'benefit': '1'}],
        ))
        codes = {problem['code'] for problem in report.as_dict()['reconciliation']['problems']}
        self.assertIn('historical_lease_missing_from_batch', codes)

    # -- determinism and duplicates -------------------------------------

    def test_input_order_does_not_change_the_conclusion(self):
        leases = [
            lease('l1'),
            lease('l2', owner_id='p2', lease_start='2026-11-16'),
            lease('l3', owner_id='p3', rent='900'),
        ]
        policies = [BASELINE, {**BASELINE, 'policy_id': 'doctor', 'monthly_cap': '700'}]
        candidate_policies = [CANDIDATE, {**CANDIDATE, 'policy_id': 'doctor', 'monthly_cap': '650'}]
        reference = self.analyzer.analyze({
            'baseline_policies': policies,
            'candidate_policies': candidate_policies,
            'leases': leases,
            'periods': ['2026-11'],
        }).as_dict()
        randomizer = random.Random(7)
        for _ in range(10):
            shuffled_leases = leases[:]
            shuffled_baseline = policies[:]
            shuffled_candidate = candidate_policies[:]
            randomizer.shuffle(shuffled_leases)
            randomizer.shuffle(shuffled_baseline)
            randomizer.shuffle(shuffled_candidate)
            payload = self.analyzer.analyze({
                'baseline_policies': shuffled_baseline,
                'candidate_policies': shuffled_candidate,
                'leases': shuffled_leases,
                'periods': ['2026-11'],
            }).as_dict()
            self.assertEqual(payload, reference)

    def test_exact_duplicate_leases_are_collapsed(self):
        base = lease('l1')
        report = self.analyzer.analyze(self.request([base, dict(base)]))
        self.assertEqual(len(report.as_dict()['leases']), 1)

    def test_conflicting_duplicate_leases_are_rejected(self):
        with self.assertRaises(PolicyImpactError):
            self.analyzer.analyze(self.request([lease('l1'), lease('l1', rent='9999')]))

    def test_conflicting_duplicate_policies_are_rejected(self):
        with self.assertRaises(PolicyImpactError):
            self.analyzer.analyze({
                'baseline_policies': [BASELINE, {**BASELINE, 'monthly_cap': '100'}],
                'candidate_policies': [CANDIDATE],
                'leases': [lease('l1')],
            })

    def test_overlapping_policy_versions_are_rejected(self):
        with self.assertRaises(PolicyImpactError):
            self.analyzer.analyze({
                'baseline_policies': [BASELINE, {**BASELINE, 'version': 2,
                                                 'effective_from': '2026-06-01'}],
                'candidate_policies': [CANDIDATE],
                'leases': [lease('l1')],
            })

    # -- read-only guarantees ------------------------------------------

    def test_checks_confirm_nothing_was_written(self):
        checks = self.analyzer.analyze(self.request([lease('l1')])).as_dict()['checks']
        self.assertEqual(checks['formal_payments_written'], 0)
        self.assertEqual(checks['historical_settlements_written'], 0)
        self.assertTrue(checks['engine_parity'])
        self.assertTrue(checks['settlement_book_balanced'])

    def test_baseline_matches_the_settlement_engine(self):
        from housing_relief.settlement.engine import SettlementEngine
        request = self.request([
            lease('l1'),
            lease('l2', lease_start='2026-11-16', rent='3000', household_income='8000'),
        ])
        report = self.analyzer.analyze(request)
        engine = SettlementEngine()
        for item in report.as_dict()['leases']:
            start = item['baseline_months'][0]['policies']
            claim = {
                'period': '2026-11',
                'rent': '2400' if item['lease_id'] == 'l1' else '3000',
                'lease_start': '2026-11-01' if item['lease_id'] == 'l1' else '2026-11-16',
                'lease_end': '2026-11-30',
                'household_income': '5000' if item['lease_id'] == 'l1' else '8000',
            }
            result = engine.evaluate(claim, {'policies': [
                {key: policy[key] for key in ('policy_id', 'effective_from', 'effective_to',
                                              'monthly_cap', 'income_cap', 'exclusive_group')}
                for policy in start
            ]})
            self.assertEqual(str(result.value), item['baseline_benefit'])


class PolicyImpactServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'db.sqlite3')
        self.service = PolicyImpactService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def request(self):
        return {
            'baseline_policies': [BASELINE],
            'candidate_policies': [CANDIDATE],
            'leases': [lease('l1')],
            'periods': ['2026-11'],
        }

    def test_repeat_submission_is_idempotent(self):
        first = self.service.submit(self.request(), 'key-1')
        second = self.service.submit(self.request(), 'key-1')
        self.assertEqual(first, second)

    def test_reusing_key_with_different_payload_is_rejected(self):
        self.service.submit(self.request(), 'key-1')
        changed = self.request()
        changed['candidate_policies'] = [{**CANDIDATE, 'monthly_cap': '100'}]
        with self.assertRaises(ConflictError):
            self.service.submit(changed, 'key-1')

    def test_only_the_analysis_snapshot_is_persisted(self):
        self.service.submit(self.request(), 'key-1')
        self.assertEqual(self.database.counts(), {})
        self.assertEqual(self.database.pending_messages(), [])
        report = self.service.submit(self.request(), None)
        snapshot = self.service.history(report['analysis_id'])
        self.assertEqual(snapshot['content']['input_fingerprint'], report['input_fingerprint'])

    def test_reordered_submission_returns_the_same_analysis(self):
        first = self.service.submit(self.request(), 'key-1')
        shuffled = {
            'leases': [lease('l1')],
            'candidate_policies': [CANDIDATE],
            'baseline_policies': [BASELINE],
            'periods': ['2026-11'],
        }
        second = self.service.submit(shuffled, 'key-2')
        self.assertEqual(first['analysis_id'], second['analysis_id'])
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()

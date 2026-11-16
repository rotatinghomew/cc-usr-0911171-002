import decimal
import unittest
from housing_relief.settlement.engine import SettlementEngine

class DomainTests(unittest.TestCase):
    def setUp(self): self.engine=SettlementEngine()

    def item(self,**changes):
        value={'id':'claim-1','rent':'1000','months':2,'subsidy':'300','rent_reduction':'200','demand':1};value.update(changes);return value
    def test_balanced_settlement(self):
        result=self.engine.evaluate(self.item(),{'benefit_cap':'600'});self.assertTrue(result.accepted);self.assertEqual(result.value,decimal.Decimal('500'));self.assertEqual(result.facts['payable'],'1500')
    def test_mutual_exclusion(self):self.assertIn('mutually_exclusive_benefits',self.engine.evaluate(self.item(mutually_exclusive=True),{}).reasons)
    def test_benefit_cap(self):self.assertEqual(self.engine.evaluate(self.item(),{'benefit_cap':'250'}).value,decimal.Decimal('250'))
    def test_zero_floor(self):self.assertEqual(self.engine.evaluate(self.item(rent='100',months=1,subsidy='500',rent_reduction='0'),{}).facts['payable'],'0')
    def test_rank_by_benefit(self):self.assertEqual(self.engine.rank([self.item(id='a',subsidy='100'),self.item(id='b',subsidy='400')],{})[0].key,'b')
    def test_compare_change(self):self.assertEqual(self.engine.compare(self.engine.rank([self.item(subsidy='100')],{}),self.engine.rank([self.item(subsidy='200')],{}))['change_count'],1)
    def test_allocation_capacity(self):
        accepted,rejected=self.engine.allocate([self.item(id='a',demand=7),self.item(id='b',demand=7)],{'capacity':10});self.assertEqual((len(accepted),len(rejected)),(1,1))
    def test_summary(self):self.assertEqual(self.engine.summarize([self.engine.evaluate(self.item(),{})])['accepted'],1)


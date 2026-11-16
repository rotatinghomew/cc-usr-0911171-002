import decimal
import unittest
from housing_relief.accounting.settlement_book import SettlementBook
class SettlementBookTests(unittest.TestCase):
 def setUp(self):self.book=SettlementBook();self.policy={'policy_id':'degree','effective_from':'2026-01-01','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
 def test_full_month(self):
  out=self.book.settle({'period':'2026-11','rent':'2400','lease_start':'2026-11-01','lease_end':'2026-11-30','household_income':'5000'},{'policies':[self.policy]});self.assertEqual(out['benefit'],'600.00');self.assertTrue(out['balanced'])
 def test_proration(self):
  out=self.book.settle({'period':'2026-11','rent':'3000','lease_start':'2026-11-16','lease_end':'2026-11-30','household_income':'5000'},{'policies':[self.policy]});self.assertEqual(out['fraction'],'0.5');self.assertEqual(out['benefit'],'300.00')
 def test_income_cap(self):self.assertEqual(self.book.settle({'period':'2026-11','rent':'2400','household_income':'20000'},{'policies':[self.policy]})['benefit'],'0.00')
 def test_exclusive_group_selects_larger(self):self.assertEqual(self.book.settle({'period':'2026-11','rent':'2400','household_income':'5000'},{'policies':[self.policy,{**self.policy,'policy_id':'doctor','monthly_cap':'900'}]})['policies'],['doctor'])
 def test_recovery_installments(self):self.assertEqual(self.book.recover([{'amount':'1000','status':'paid'}],decimal.Decimal('250'))['installments'],['300','300','150'])

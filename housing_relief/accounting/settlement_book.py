from __future__ import annotations
import datetime as dt
import decimal
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
D=decimal.Decimal
@dataclass(frozen=True)
class PolicyVersion:
    policy_id:str; effective_from:dt.date; effective_to:dt.date|None; monthly_cap:D; income_cap:D; exclusive_group:str
    def active(self,day):return self.effective_from<=day and (self.effective_to is None or day<=self.effective_to)
class SettlementBook:
    def policy(self,row):return PolicyVersion(str(row['policy_id']),dt.date.fromisoformat(str(row['effective_from'])),dt.date.fromisoformat(str(row['effective_to'])) if row.get('effective_to') else None,D(str(row.get('monthly_cap','0'))),D(str(row.get('income_cap','999999999'))),str(row.get('exclusive_group','default')))
    def month_fraction(self,start,end,month):
        next_month=(month.replace(day=28)+dt.timedelta(days=4)).replace(day=1);last=next_month-dt.timedelta(days=1);left=max(start,month.replace(day=1));right=min(end,last)
        return D(max(0,(right-left).days+1))/D(last.day)
    def select_policies(self,claim,policies):
        day=dt.date.fromisoformat(str(claim['period'])+'-01');income=D(str(claim.get('household_income','0')));active=[]
        for row in policies:
            policy=self.policy(row)
            if policy.active(day) and income<=policy.income_cap:active.append(policy)
        by_group={}
        for policy in active:
            old=by_group.get(policy.exclusive_group)
            if old is None or policy.monthly_cap>old.monthly_cap:by_group[policy.exclusive_group]=policy
        return sorted(by_group.values(),key=lambda policy:policy.policy_id)
    def settle(self,claim,context):
        period=dt.date.fromisoformat(str(claim['period'])+'-01');lease_start=dt.date.fromisoformat(str(claim.get('lease_start',period)));lease_end=dt.date.fromisoformat(str(claim.get('lease_end',period.replace(day=28))));rent=D(str(claim.get('rent',claim.get('amount','0'))));selected=self.select_policies(claim,context.get('policies',()));fraction=self.month_fraction(lease_start,lease_end,period);gross=(rent*fraction).quantize(D('0.01'));benefit=min(gross,sum((p.monthly_cap*fraction for p in selected),D('0'))).quantize(D('0.01'));payable=gross-benefit;entries=[('rent_receivable',gross),('policy_benefit',-benefit),('tenant_cash',-payable)]
        return {'gross':str(gross),'benefit':str(benefit),'payable':str(payable),'balanced':sum(v for _,v in entries)==0,'entries':[(n,str(v)) for n,v in entries],'policies':[p.policy_id for p in selected],'fraction':str(fraction)}
    def recover(self,paid,entitled):
        total=sum((D(str(row['amount'])) for row in paid if row.get('status')!='reversed'),D('0'));over=max(D('0'),total-entitled);installments=[];remaining=over
        while remaining>0:part=min(D('300'),remaining);installments.append(str(part));remaining-=part
        return {'paid':str(total),'entitled':str(entitled),'overpayment':str(over),'installments':installments}

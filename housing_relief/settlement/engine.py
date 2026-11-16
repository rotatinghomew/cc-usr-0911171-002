from __future__ import annotations
from ..accounting.settlement_book import SettlementBook
import datetime as dt
import decimal
import math
import statistics
import ast
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

class DomainError(RuntimeError):
    pass

@dataclass(frozen=True)
class AnalysisResult:
    key: str
    accepted: bool
    value: decimal.Decimal
    reasons: tuple[str,...]
    facts: Mapping[str,Any]=field(default_factory=dict)

class SettlementEngineBase:
    algorithm='ledger'
    def normalize(self,rows):
        result=[];seen=set()
        for position,row in enumerate(rows):
            item=dict(row);key=str(item.get('id') or position)
            if key in seen:continue
            seen.add(key);item['_key']=key;item['_position']=position;result.append(item)
        return result
    def rank(self,subjects,context):return sorted([self.evaluate(item,context) for item in self.normalize(subjects)],key=lambda result:(not result.accepted,-result.value,result.key))
    def allocate(self,subjects,context):
        remaining=decimal.Decimal(str(context.get('capacity',100)))-decimal.Decimal(str(context.get('reserved',0)));accepted=[];rejected=[]
        by_key={str(item.get('id')):item for item in self.normalize(subjects)}
        for result in self.rank(subjects,context):
            demand=decimal.Decimal(str(by_key[result.key].get('demand',1)))
            if result.accepted and demand<=remaining:accepted.append(result);remaining-=demand
            else:rejected.append(AnalysisResult(result.key,False,result.value,result.reasons+('allocation_exhausted',),result.facts))
        return accepted,rejected
    def summarize(self,results):
        values=[float(result.value) for result in results];reasons=Counter(reason for result in results for reason in result.reasons)
        return {'count':len(results),'accepted':sum(result.accepted for result in results),'rejected':sum(not result.accepted for result in results),'mean':statistics.fmean(values) if values else 0.0,'median':statistics.median(values) if values else 0.0,'reasons':dict(sorted(reasons.items()))}
    def compare(self,left,right):
        a={item.key:item for item in left};b={item.key:item for item in right};changes=[]
        for key in sorted(set(a)|set(b)):
            old=a.get(key);new=b.get(key)
            if old is None:changes.append({'key':key,'change':'added','accepted':new.accepted})
            elif new is None:changes.append({'key':key,'change':'removed','accepted':old.accepted})
            elif old.accepted!=new.accepted or old.value!=new.value:changes.append({'key':key,'change':'changed','from':str(old.value),'to':str(new.value),'accepted_from':old.accepted,'accepted_to':new.accepted})
        return {'changes':changes,'change_count':len(changes)}

class SettlementEngine(SettlementEngineBase):
    def __init__(self): self.book=SettlementBook()
    def evaluate(self,item,context):
        if item.get('period') and context.get('policies'):
            facts=self.book.settle(item,context); reasons=() if facts['balanced'] else ('journal_not_balanced',); return AnalysisResult(str(item.get('id','unknown')),not reasons,decimal.Decimal(facts['benefit']),reasons,facts)
        key=str(item.get('id','unknown'));reasons=[];rent=decimal.Decimal(str(item.get('rent',item.get('amount',0))));months=decimal.Decimal(str(item.get('months',1)));gross=rent*months
        subsidy=decimal.Decimal(str(item.get('subsidy',0)));reduction=decimal.Decimal(str(item.get('rent_reduction',0)));cap=decimal.Decimal(str(context.get('benefit_cap',gross)))
        if item.get('mutually_exclusive') and subsidy>0 and reduction>0:reasons.append('mutually_exclusive_benefits')
        benefit=min(cap,subsidy+reduction);payable=max(decimal.Decimal('0'),gross-benefit);entries=[('rent_receivable',gross),('benefit_offset',-benefit),('cash_due',-payable)]
        if sum(value for _,value in entries)!=decimal.Decimal('0'):reasons.append('journal_not_balanced')
        accepted=not reasons;return AnalysisResult(key,accepted,benefit,tuple(reasons),{'gross':str(gross),'payable':str(payable),'entries':[(name,str(value)) for name,value in entries]})

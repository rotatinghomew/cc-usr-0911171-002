青年人才住房补贴与租金减免清算中心
测试：python3 -m unittest discover -s tests -v
构建：python3 -m compileall -q housing_relief tests
验收：python3 -m housing_relief --database /tmp/housing_relief.sqlite3 run --fixture fixtures/acceptance.json --output /tmp/housing_relief-output.json
单测：python3 -m unittest tests.test_domain -v

政策调整影响分析（只读，不写支付/历史清算）：
python3 -m housing_relief --database /tmp/housing_relief.sqlite3 impact \
  --fixture fixtures/policy_impact.json --output /tmp/housing_relief-impact.json
fixture 字段：
  baseline_policies / candidate_policies：两套政策版本（policy_id + version）
  leases：同一批租约，逐份使用自身 lease_start/lease_end 与 household_income 重算
  periods：分析月份（YYYY-MM），省略时覆盖租约触及的每个月
  historical：历史清算金额（benefit/payable/gross，可省略 period 表示租约合计）
  tolerance：对账容差（默认 0.00）
  income_band_edges：分层用的收入档位，默认 5000/10000/20000
输出：逐租约增减免与自付变化、direction、policy_changes（含导致变化的政策版本与字段）、
  summary/strata 分层汇总、reconciliation（与原结算口径及历史清算对账）、checks。
资格不明（缺收入/缺租约窗口/状态未定等）列为 unknown 并排除在金额合计之外，不按零收益处理；
租约不在分析期内列为 not_in_period（可解释的零）；互斥政策每组仍只选一项。

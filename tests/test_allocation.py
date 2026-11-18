"""核验成本归属、计价版本、预算风险、复现与修订影响的完整口径。"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from analytics import AnalyticsService

P1, P2 = "p-alpha", "p-beta"
MONTH = {"start": "2026-11-01T00:00:00+08:00",
         "end": "2026-12-01T00:00:00+08:00", "label": "2026-11"}
PUBLISH_AS_OF = "2026-12-03T12:00:00+08:00"
LATER_AS_OF = "2026-12-11T12:00:00+08:00"


def request_event(event_id, entity, product, call_class, items=0,
                  bytes_=None, route="r-a", day=3):
    payload = {"product": product, "call_class": call_class, "items": items}
    if bytes_ is not None:
        payload["bytes"] = bytes_
    if route:
        payload["route_id"] = route
    return {"source": "applog", "event_id": event_id, "revision": 1, "kind": "request",
            "entity": entity,
            "occurred_at": f"2026-11-{day:02d}T10:00:00+08:00",
            "observed_at": f"2026-11-{day:02d}T11:00:00+08:00",
            "payload": payload}


def transfer_event(event_id, entity, account, bytes_, product=None, route="r-a"):
    payload = {"account": account, "route_id": route, "direction": "egress",
               "bytes": bytes_, "period_start": MONTH["start"], "period_end": MONTH["end"]}
    if product:
        payload["product"] = product
    return {"source": "netflow", "event_id": event_id, "revision": 1, "kind": "transfer",
            "entity": entity, "occurred_at": "2026-11-30T23:00:00+08:00",
            "observed_at": "2026-12-01T08:00:00+08:00", "payload": payload}


def charge_event(event_id, account, amount, charge_type, basis, revision=1,
                 route=None, product=None, consumer=None, credit=False,
                 version=None, observed_at=None, period=None):
    span = period or MONTH
    payload = {"account": account, "charge_type": charge_type,
               "allocation_basis": basis, "amount": amount,
               "period_start": span["start"], "period_end": span["end"]}
    if route:
        payload["route_id"] = route
    if product:
        payload["product"] = product
    if consumer:
        payload["consumer_id"] = consumer
    if credit:
        payload["credit"] = True
    if version:
        payload["pricing_version"] = version
    return {"source": "bill", "event_id": event_id, "revision": revision, "kind": "charge",
            "entity": account, "occurred_at": "2026-12-02T10:00:00+08:00",
            "observed_at": observed_at or "2026-12-02T10:00:00+08:00",
            "payload": payload}


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        for entity_id, label in (("team-x", "团队X"), ("team-y", "团队Y")):
            self.service.register(entity_id, label, "g1")
        self.service.link("applog", "caller-x", "team-x", "2026-11-01T00:00:00+08:00")
        self.service.link("applog", "caller-y", "team-y", "2026-11-01T00:00:00+08:00")
        self.service.link("netflow", "caller-x", "team-x", "2026-11-01T00:00:00+08:00")
        self.service.link("netflow", "caller-y", "team-y", "2026-11-01T00:00:00+08:00")
        self.service.register_product(P1, "负责人甲", "acct-a")
        self.service.register_product(P2, "负责人乙", "acct-b")
        self.service.register_pricing(
            version_id="v-tx", product=P1, direction="egress", fee_type="transfer",
            valid_from=MONTH["start"], observed_at="2026-10-20T09:00:00+08:00", rate="0.5")
        self.service.register_pricing(
            version_id="v-rq", product=P1, direction="egress", fee_type="request",
            valid_from=MONTH["start"], observed_at="2026-10-20T09:00:00+08:00", rate="0.02")

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def _seed_calls_and_flows(self):
        # X：1 标准 + 1 重试（重试不增加业务请求数，但产生计费单元与流量）
        self.service.ingest(request_event("r1", "caller-x", P1, "standard", bytes_=600))
        self.service.ingest(request_event("r2", "caller-x", P1, "retry", bytes_=400))
        # Y：1 批量、200 条目
        self.service.ingest(request_event("r3", "caller-y", P1, "batch", items=200, bytes_=3000))
        self.service.ingest(transfer_event("f1", "caller-x", "acct-a", 1_000_000_000, P1))
        self.service.ingest(transfer_event("f2", "caller-y", "acct-a", 3_000_000_000, P1))

    def test_retry_and_batch_not_split_by_request_count(self):
        self._seed_calls_and_flows()
        # 传输池 4.00 元按字节 1:3 分摊，而不是按 2:1 的请求数
        self.service.ingest(charge_event("c1", "acct-a", "4.00", "transfer",
                                         "bytes", route="r-a", version="v-tx"))
        # 请求池 4.04 元按计费单元 2:200 分摊
        self.service.ingest(charge_event("c2", "acct-a", "4.04", "request",
                                         "requests", version="v-rq"))
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        x = result["products"][P1]["consumers"]["team-x"]
        y = result["products"][P1]["consumers"]["team-y"]
        self.assertEqual(x["cost"]["transfer"], "1.00")
        self.assertEqual(y["cost"]["transfer"], "3.00")
        self.assertEqual(x["cost"]["request"], "0.04")
        self.assertEqual(y["cost"]["request"], "4.00")
        self.assertEqual(x["volume"]["demand_requests"], 1)
        self.assertEqual(x["volume"]["retry_requests"], 1)
        self.assertEqual(x["volume"]["billable_units"], 2)
        self.assertEqual(y["volume"]["batch_items"], 200)
        self.assertEqual(y["volume"]["billable_units"], 200)
        for row in result["reconciliation"]:
            self.assertTrue(row["balanced"], row)

    def test_largest_remainder_closes_to_the_cent(self):
        self._seed_calls_and_flows()
        # 1.00 按字节 1:3 => 0.25 / 0.75 整除；换成 1.01 制造尾差
        self.service.ingest(charge_event("c1", "acct-a", "1.01", "transfer",
                                         "bytes", route="r-a"))
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        shares = {c: Decimal(s["cost"]["transfer"])
                  for c, s in result["products"][P1]["consumers"].items()}
        self.assertEqual(sum(shares.values()), Decimal("1.01"))
        self.assertEqual(shares["team-x"], Decimal("0.25"))
        self.assertEqual(shares["team-y"], Decimal("0.76"))

    def test_unattributed_when_no_cost_driver(self):
        # 只有费用，没有任何流量/调用动因
        self.service.ingest(charge_event("c1", "acct-a", "5.00", "transfer",
                                         "bytes", route="r-ghost"))
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        self.assertEqual(result["totals"]["billed"], "5.00")
        self.assertEqual(result["totals"]["allocated"], "0")
        self.assertEqual(len(result["unattributed"]), 1)
        self.assertEqual(result["unattributed"][0]["reason"], "缺少成本动因")
        self.assertTrue(all(row["balanced"] for row in result["reconciliation"]))

    def test_unknown_product_direct_charge_is_unattributed(self):
        self.service.ingest(charge_event("c1", "acct-a", "2.00", "request",
                                         "requests", product="p-ghost"))
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        self.assertEqual(result["unattributed"][0]["reason"], "产品不在目录")

    def test_credits_reduce_current_risk_and_flag_history(self):
        self._seed_calls_and_flows()
        self.service.ingest(charge_event("c1", "acct-a", "4.00", "transfer",
                                         "bytes", route="r-a", version="v-tx"))
        self.service.set_budget(P1, "2026-11", "3.00")
        report = self.service.publish("r-1", PUBLISH_AS_OF, "11月结论",
                                      as_of=PUBLISH_AS_OF, period=MONTH,
                                      elapsed_ratio="1")
        self.assertEqual(report["body"]["allocation"]["totals"]["billed"], "4.00")
        risk_at_publish = report["body"]["risk"]["assessments"]
        breach = [row for row in risk_at_publish
                  if row["product"] == P1 and row["consumer"] is None][0]
        self.assertEqual(breach["level"], "breach")  # 4.00/3.00 = 133%

        # 迟到抵扣：独立调整事件，以负金额冲减 0.20（修订号保留给纠正场景）
        self.service.ingest(charge_event("c1-credit", "acct-a", "0.20", "transfer",
                                         "bytes", route="r-a",
                                         credit=True, version="v-tx",
                                         observed_at=LATER_AS_OF))
        amendment = self.service.record_amendment(
            "a-1", LATER_AS_OF, "credit", MONTH, "云厂商账单抵扣",
            ["bill:c1-credit"], LATER_AS_OF, "1")
        affected = amendment["impact"]["affected_reports"][0]
        self.assertTrue(affected["changed"])
        self.assertEqual(affected["product_deltas"][0]["delta"], "-0.20")
        current = self.service.allocate(MONTH["start"], MONTH["end"], LATER_AS_OF)
        self.assertEqual(current["totals"]["billed"], "3.80")
        self.assertEqual(current["totals"]["credits"], "-0.20")
        # 历史报告本身不可变
        self.assertEqual(
            self.service.get_report("r-1")["body"]["allocation"]["totals"]["billed"],
            "4.00")
        # 旧账单口径仍可逐字段复现
        self.assertEqual(self.service.reproduce("r-1"),
                         report["body"]["allocation"])
        # 抵扣后当前风险敞口随实际成本同步下降（仍超预算，但缺口收窄）
        current_risk = self.service.risk(current, "2026-11", "1")
        row = [r for r in current_risk["assessments"]
               if r["product"] == P1 and r["consumer"] is None][0]
        self.assertEqual(row["level"], "breach")
        self.assertEqual(row["actual"], "3.80")
        self.assertEqual(row["usage_ratio"], "1.266666666666666666666666667")

    def test_bitemporal_reproduction_with_attribution_correction(self):
        self._seed_calls_and_flows()
        self.service.ingest(charge_event("c1", "acct-a", "4.00", "transfer",
                                         "bytes", route="r-a"))
        # 发布时 caller-y 尚未映射，其费用无法按使用方归属
        self.service.connection.execute(
            "DELETE FROM aliases WHERE source='netflow' AND external_id='caller-y'")
        published = self.service.allocate(MONTH["start"], MONTH["end"], PUBLISH_AS_OF)
        self.service.publish("r-2", PUBLISH_AS_OF, "发布时口径",
                             as_of=PUBLISH_AS_OF, period=MONTH)
        # 归属纠正到来
        self.service.link("netflow", "caller-y", "team-y", LATER_AS_OF)
        corrected = self.service.allocate(MONTH["start"], MONTH["end"], LATER_AS_OF)
        self.assertIn("team-y", corrected["products"][P1]["consumers"])
        # as_of 复现仍反映发布时的未映射状态
        reproduced = self.service.reproduce("r-2")
        self.assertEqual(reproduced, published)
        self.assertTrue(any(item["event_id"] == "f2"
                            for item in reproduced["unmatched_events"]))

    def test_pricing_versions_drive_expected_cost_and_tiers(self):
        self._seed_calls_and_flows()
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        # X 传输 1GB × 0.5；Y 3GB × 0.5；请求：X 2 单元、Y 200 单元 × 0.02
        x = result["products"][P1]["consumers"]["team-x"]
        y = result["products"][P1]["consumers"]["team-y"]
        self.assertEqual(x["expected_cost"], "0.54")
        self.assertEqual(y["expected_cost"], "5.50")
        # 阶梯计价：5GB 内 0.5，超出部分 0.4
        self.service.register_pricing(
            version_id="v-tier", product=P2, direction="egress", fee_type="transfer",
            valid_from=MONTH["start"], observed_at="2026-10-20T09:00:00+08:00",
            rate="1.00", tiered=[{"up_to": 5, "rate": "0.50"},
                                  {"up_to": 1000000, "rate": "0.40"}])
        version = self.service.pricing.resolve(P2, "transfer", MONTH["start"], "egress")
        amount = self.service.pricing.expected_amount(version, 7)
        self.assertEqual(amount, Decimal("3.30"))

    def test_future_pricing_version_invisible_to_historical_report(self):
        # 12 月才获知的新费率，11 月口径的期望成本不应使用
        self._seed_calls_and_flows()
        self.service.register_pricing(
            version_id="v-future", product=P1, direction="egress", fee_type="transfer",
            valid_from=MONTH["start"], observed_at=LATER_AS_OF, rate="9.99")
        historical = self.service.allocate(MONTH["start"], MONTH["end"], PUBLISH_AS_OF)
        self.assertEqual(
            historical["products"][P1]["consumers"]["team-x"]["expected_cost"], "0.54")
        current = self.service.allocate(MONTH["start"], MONTH["end"], LATER_AS_OF)
        # 当前口径下 resolve 取观察时间最新的版本 v-future
        self.assertEqual(
            current["products"][P1]["consumers"]["team-x"]["expected_cost"], "10.03")

    def test_variance_separates_volume_and_rate_effects(self):
        # 基期（11月）：2 个业务请求、请求费 4.00 -> 单位成本 2.00/请求
        self.service.ingest(request_event("r1", "caller-x", P1, "standard", day=2))
        self.service.ingest(request_event("r3", "caller-y", P1, "standard", day=2))
        self.service.ingest(charge_event("c1", "acct-a", "4.00", "request",
                                         "requests"))
        baseline = self.service.allocate(MONTH["start"], MONTH["end"])

        # 次期（12月）：请求翻倍到 4 个、费用 12.00 -> 单位成本 3.00（恶化）
        next_month = {"start": "2026-12-01T00:00:00+08:00",
                      "end": "2027-01-01T00:00:00+08:00", "label": "2026-12"}
        for eid, caller, day in (("n1", "caller-x", 2), ("n2", "caller-x", 3),
                                 ("n3", "caller-y", 4), ("n4", "caller-y", 5)):
            event = request_event(eid, caller, P1, "standard", day=day)
            event["occurred_at"] = f"2026-12-{day:02d}T10:00:00+08:00"
            event["observed_at"] = f"2026-12-{day:02d}T11:00:00+08:00"
            self.service.ingest(event)
        self.service.ingest(charge_event("c2", "acct-a", "12.00", "request",
                                         "requests", period=next_month,
                                         observed_at="2027-01-02T10:00:00+08:00"))
        current = self.service.allocate(next_month["start"], next_month["end"])
        variance = self.service.variance(current, baseline)[P1]
        # 量差 = (4-2) × 2.00 = 4.00；价差 = (3.00-2.00) × 4 = 4.00
        self.assertEqual(variance["volume_effect"], "4.00")
        self.assertEqual(variance["rate_effect"], "4.00")
        self.assertEqual(variance["cost_delta"], "8.00")
        self.assertTrue(variance["deterioration"])

    def test_contract_rejections(self):
        bad_batch = request_event("bad", "caller-x", P1, "batch", items=0)
        with self.assertRaises(ValueError):
            self.service.ingest(bad_batch)
        bad_class = request_event("bad2", "caller-x", P1, "weird")
        with self.assertRaises(ValueError):
            self.service.ingest(bad_class)
        bad_credit = charge_event("bad3", "acct-a", "1.00", "request",
                                  "requests", consumer="team-y")  # 有使用方无产品
        with self.assertRaises(ValueError):
            self.service.ingest(bad_credit)

    def test_shared_transfer_on_multi_product_account_is_unattributed(self):
        # 同一账户挂两个产品，共享流量无产品标识：费用与用量都不可臆断归属
        self.service.register_product("p-gamma", "负责人丙", "acct-a")
        self.service.link("netflow", "caller-x", "team-x", "2026-11-01T00:00:00+08:00")
        self.service.ingest(transfer_event("f-shared", "caller-x", "acct-a",
                                           2_000_000_000, product=None))
        self.service.ingest(charge_event("c-shared", "acct-a", "6.00", "transfer",
                                         "bytes", route="r-a"))
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        self.assertTrue(all(
            item["reason"] == "缺少成本动因" for item in result["unattributed"]))
        self.assertEqual(result["totals"]["allocated"], "0")
        self.assertEqual(result["totals"]["unattributed"], "6.00")
        self.assertTrue(all(row["balanced"] for row in result["reconciliation"]))
        # 虚产品不应出现在结果中
        self.assertNotIn("__shared__", result["products"])

    def test_shared_transfer_on_single_product_account_is_attributable(self):
        # acct-a 目录下只有 p-alpha，共享流量可归入该产品并按字节动因分摊
        self.service.ingest(transfer_event("f-shared", "caller-x", "acct-a",
                                           1_000_000_000, product=None))
        self.service.ingest(charge_event("c-shared", "acct-a", "2.00", "transfer",
                                         "bytes", route="r-a"))
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        self.assertEqual(result["unattributed"], [])
        self.assertEqual(
            result["products"][P1]["consumers"]["team-x"]["cost"]["transfer"], "2.00")

    def test_billing_correction_via_higher_revision(self):
        # 归属纠正：同一账单事件的高修订替代旧修订；发布快照不动、当前判断更新
        self._seed_calls_and_flows()
        self.service.ingest(charge_event("c1", "acct-a", "4.00", "transfer",
                                         "bytes", route="r-a"))
        self.service.publish("r-fix", PUBLISH_AS_OF, "发布时口径",
                             as_of=PUBLISH_AS_OF, period=MONTH)
        self.service.ingest(charge_event("c1", "acct-a", "3.20", "transfer",
                                         "bytes", revision=2, route="r-a",
                                         observed_at=LATER_AS_OF))
        current = self.service.allocate(MONTH["start"], MONTH["end"], LATER_AS_OF)
        # 高修订替代：当前总额为 3.20，而非 4.00 或 7.20
        self.assertEqual(current["totals"]["billed"], "3.20")
        historical = self.service.allocate(MONTH["start"], MONTH["end"], PUBLISH_AS_OF)
        self.assertEqual(historical["totals"]["billed"], "4.00")
        self.assertEqual(self.service.reproduce("r-fix")["totals"]["billed"], "4.00")
        amendment = self.service.record_amendment(
            "a-fix", LATER_AS_OF, "attribution", MONTH, "账单金额纠正",
            ["bill:c1@2"], LATER_AS_OF, "1")
        delta = amendment["impact"]["affected_reports"][0]["product_deltas"][0]
        self.assertEqual(delta["published_total"], "4.00")
        self.assertEqual(delta["current_total"], "3.20")
        self.assertEqual(delta["delta"], "-0.80")

    def test_budget_levels_and_forecast(self):
        self._seed_calls_and_flows()
        self.service.ingest(charge_event("c1", "acct-a", "4.00", "transfer",
                                         "bytes", route="r-a"))
        self.service.set_budget(P1, "2026-11", "3.20")  # 4.00/3.20 = 125%
        result = self.service.allocate(MONTH["start"], MONTH["end"])
        risk = self.service.risk(result, "2026-11", elapsed_ratio="0.5")
        product_row = [r for r in risk["assessments"]
                       if r["product"] == P1 and r["consumer"] is None][0]
        self.assertEqual(product_row["level"], "breach")
        # 半月已花 4.00 => 外推整月 8.00
        self.assertEqual(product_row["forecast"], "8.0")
        self.service.set_budget(P1, "2026-11", "9.00")  # 预测 8.0/9.0 ≈ 88.9%，预警区间
        risk2 = self.service.risk(result, "2026-11", elapsed_ratio="0.5")
        product_row2 = [r for r in risk2["assessments"]
                        if r["product"] == P1 and r["consumer"] is None][0]
        self.assertEqual(product_row2["level"], "watch")
        self.service.set_budget(P1, "2026-11", "20.00")  # 40%，安全
        risk3 = self.service.risk(result, "2026-11", elapsed_ratio="0.5")
        product_row3 = [r for r in risk3["assessments"]
                        if r["product"] == P1 and r["consumer"] is None][0]
        self.assertEqual(product_row3["level"], "ok")


if __name__ == "__main__":
    unittest.main()

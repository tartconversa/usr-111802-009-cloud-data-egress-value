"""本地演练入口：导入样本并跑通成本归属、报告复现与修订影响。"""
import argparse
import json
from pathlib import Path
from .service import AnalyticsService

PERIOD = {"start": "2026-11-01T00:00:00+08:00",
          "end": "2026-12-01T00:00:00+08:00", "label": "2026-11"}
PUBLISH_AS_OF = "2026-12-03T12:00:00+08:00"
CURRENT_AS_OF = "2026-12-11T12:00:00+08:00"

def main():
    parser = argparse.ArgumentParser(description="运营分析服务")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", default="fixtures/sample.json")
    args = parser.parse_args()
    sample = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    service = AnalyticsService(args.database)
    try:
        for subject in sample["entities"]:
            service.register(**subject)
        for alias in sample["aliases"]:
            service.link(**alias)
        for product in sample.get("products", []):
            service.register_product(**product)
        for pricing in sample.get("pricing", []):
            service.register_pricing(**pricing)
        for budget in sample.get("budgets", []):
            service.set_budget(**budget)
        for event in sample["events"]:
            service.ingest(event)

        print("== 发布时点（旧账单口径）的归属与风险 ==")
        published = service.allocate(PERIOD["start"], PERIOD["end"], PUBLISH_AS_OF, PERIOD["label"])
        print(json.dumps(published, ensure_ascii=False, sort_keys=True, indent=2))
        report = service.publish("report-2026-11", "2026-12-03T12:00:00+08:00",
                                 "2026年11月经营结论",
                                 as_of=PUBLISH_AS_OF, period=PERIOD, elapsed_ratio="1")
        print("报告摘要:", report["digest"])

        print("== 按旧账单复现历史结论 ==")
        reproduced = service.reproduce("report-2026-11")
        print("复现一致:", reproduced == report["body"]["allocation"])

        print("== 抵扣/纠正到达后的当前判断与受影响历史结论 ==")
        amendment = service.record_amendment(
            "amend-2026-11-1", "2026-12-11T12:00:00+08:00", "credit", PERIOD,
            "账单抵扣与链路费用纠正", ["bill:bill-6", "bill:bill-5"], CURRENT_AS_OF, "1")
        print(json.dumps(amendment["impact"]["affected_reports"], ensure_ascii=False, indent=2))
        print(json.dumps(amendment["impact"]["risk"]["assessments"], ensure_ascii=False, indent=2))
    finally:
        service.close()

if __name__ == "__main__":
    main()

"""协调来源事件、主体目录、成本归属、计价版本、预算风险与报告发布。"""
import json
import sqlite3
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import ProductDirectory
from .pricing import PricingBook
from .allocation import CostAllocator
from .budget import BudgetBook
from .amendments import AmendmentBook
from .contracts import validate_event

class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(
            self.connection, self.config["event_kinds"],
            payload_validator=lambda kind, payload: validate_event(kind, payload, self.config))
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = ProductDirectory(self.connection)
        self.pricing = PricingBook(self.connection)
        self.allocator = CostAllocator(
            self.store, self.entities, self.domain, self.pricing, self.config)
        self.budgets = BudgetBook(self.connection, self.config["risk_thresholds"])
        self.amendments = AmendmentBook(
            self.connection, self.allocator, self.budgets, self.decisions)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 基础目录 ---------------------------------------------------------
    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id,label,group_id)

    def link(self, source, external_id, entity_id, linked_at=None):
        with self.connection:
            return self.entities.link(source, external_id, entity_id, linked_at)

    def register_product(self, product, owner, account):
        with self.connection:
            return self.domain.register(product, owner, account)

    def register_pricing(self, **kwargs):
        with self.connection:
            return self.pricing.register(**kwargs)

    def set_budget(self, product, period, amount, consumer_id=None):
        with self.connection:
            return self.budgets.set_budget(product, period, amount, consumer_id)

    # ---- 事件与通用摘要 ---------------------------------------------------
    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    # ---- 成本归属与风险 ---------------------------------------------------
    def allocate(self, period_start, period_end, as_of=None, period_label=None):
        result = self.allocator.allocate(period_start, period_end, as_of)
        result["period"]["label"] = period_label or period_start[:7]
        return result

    def risk(self, allocation, period_label, elapsed_ratio=None):
        return self.budgets.assess(allocation, period_label, elapsed_ratio)

    def variance(self, current, baseline):
        return self.allocator.variance(current, baseline)

    def reproduce(self, report_id):
        """按发布报告记录的 as_of 重新计算，应与报告快照逐字段一致。"""
        report = self.decisions.get(report_id)
        if report is None:
            raise ValueError("报告不存在")
        period = report["body"]["allocation"]["period"]
        return self.allocate(period["start"], period["end"],
                             report["body"]["allocation"].get("as_of"),
                             period.get("label"))

    def record_amendment(self, amendment_id, created_at, kind, period, reason,
                         sources, current_as_of, elapsed_ratio=None):
        with self.connection:
            return self.amendments.record(
                amendment_id, created_at, kind, period, reason, sources,
                current_as_of, elapsed_ratio)

    # ---- 报告发布（不可变快照） -------------------------------------------
    def publish(self, report_id, created_at, title, as_of=None,
                period=None, elapsed_ratio=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        if period is not None:
            allocation = self.allocate(period["start"], period["end"], as_of,
                                       period.get("label"))
            body["allocation"] = allocation
            body["risk"] = self.budgets.assess(
                allocation, allocation["period"]["label"], elapsed_ratio)
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    def get_report(self, report_id):
        return self.decisions.get(report_id)

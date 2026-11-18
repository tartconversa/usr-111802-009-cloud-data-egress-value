"""归属修订与历史结论影响追溯。

月报一旦发布即为不可变快照；迟到的账单抵扣或归属纠正不修改旧报告，
而是登记修订并重新计算当前风险，逐份标注受影响的历史结论及其偏差。
"""
import hashlib
import json
from decimal import Decimal

from .store import timestamp


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AmendmentBook:
    def __init__(self, connection, allocator, budgets, decisions):
        self.connection = connection
        self.allocator = allocator
        self.budgets = budgets
        self.decisions = decisions
        connection.execute("""CREATE TABLE IF NOT EXISTS amendments (
            amendment_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
            kind TEXT NOT NULL, period TEXT NOT NULL, reason TEXT NOT NULL,
            sources TEXT NOT NULL, impact TEXT NOT NULL, digest TEXT NOT NULL)""")

    def record(self, amendment_id, created_at, kind, period, reason, sources,
               current_as_of, elapsed_ratio=None):
        """登记修订并产出当前风险与受影响历史结论。"""
        if kind not in ("credit", "attribution", "pricing", "other"):
            raise ValueError("未知修订类型")
        if not isinstance(sources, list) or not sources:
            raise ValueError("修订必须关联来源事件")
        current = self.allocator.allocate(period["start"], period["end"], current_as_of)
        risk = self.budgets.assess(current, period["label"], elapsed_ratio)
        affected = self._impact(period)
        impact = {"current_allocation_digest": _digest(current),
                  "current_totals": current["totals"],
                  "risk": risk, "affected_reports": affected}
        record = (amendment_id, timestamp(created_at), kind, period["label"], reason,
                  json.dumps(sources, ensure_ascii=False),
                  json.dumps(impact, ensure_ascii=False, sort_keys=True),
                  _digest(impact))
        prior = self.connection.execute(
            "SELECT 1 FROM amendments WHERE amendment_id=?", (amendment_id,)).fetchone()
        if prior:
            raise ValueError("修订编号已存在")
        self.connection.execute(
            "INSERT INTO amendments VALUES (?,?,?,?,?,?,?,?)", record)
        return self.get(amendment_id)

    def _impact(self, period):
        """重算期间归属，与每份历史月报的快照口径逐产品比对。"""
        latest = self.allocator.allocate(period["start"], period["end"])
        affected = []
        for report in self.decisions.list_reports():
            body = report["body"]
            snapshot = body.get("allocation")
            if not snapshot or snapshot["period"]["label"] != period["label"]:
                continue
            deltas = []
            for product, row in latest["products"].items():
                old = snapshot.get("products", {}).get(product)
                old_total = Decimal(old["cost"]["total"]) if old else Decimal("0")
                new_total = Decimal(row["cost"]["total"])
                if old is None or old_total != new_total:
                    deltas.append({
                        "product": product,
                        "published_total": None if old is None else str(old_total),
                        "current_total": str(new_total),
                        "delta": str(new_total - old_total),
                    })
            published_unattributed = sum_decimals(snapshot.get("unattributed"))
            current_unattributed = sum_decimals(latest["unattributed"])
            affected.append({
                "report_id": report["report_id"],
                "published_at": report["created_at"],
                "changed": bool(deltas) or published_unattributed != current_unattributed,
                "product_deltas": deltas,
                "unattributed_published": str(published_unattributed),
                "unattributed_current": str(current_unattributed),
                "digest_published": report["digest"],
            })
        return affected

    def get(self, amendment_id):
        row = self.connection.execute(
            "SELECT * FROM amendments WHERE amendment_id=?", (amendment_id,)).fetchone()
        if row is None:
            return None
        return dict(row) | {"sources": json.loads(row["sources"]),
                            "impact": json.loads(row["impact"])}

    def list_amendments(self):
        return [self.get(row[0]) for row in self.connection.execute(
            "SELECT amendment_id FROM amendments ORDER BY created_at").fetchall()]


def sum_decimals(lines):
    return sum((Decimal(line["amount"]) for line in lines), Decimal("0"))

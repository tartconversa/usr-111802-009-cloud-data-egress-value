"""预算登记与预算风险判断。

预算按产品登记，可选择细化到使用方；判断口径取最细匹配：
产品×使用方有预算则用之，否则回落到产品预算。抵扣降低当前风险敞口，
预测按截至当前的实际成本线性外推到整月。
"""
from decimal import Decimal

class BudgetBook:
    def __init__(self, connection, thresholds):
        self.connection = connection
        self.watch = Decimal(str(thresholds["watch"]))
        self.breach = Decimal(str(thresholds["breach"]))
        connection.execute("""CREATE TABLE IF NOT EXISTS budgets (
            product TEXT NOT NULL, consumer_id TEXT, period TEXT NOT NULL,
            amount TEXT NOT NULL, PRIMARY KEY(product, consumer_id, period))""")

    def set_budget(self, product, period, amount, consumer_id=None):
        value = Decimal(str(amount))
        if not value.is_finite() or value <= 0:
            raise ValueError("预算必须为正的有限金额")
        # consumer_id 可空，不能依赖 ON CONFLICT（NULL 不参与唯一性匹配），先删后插。
        self.connection.execute(
            "DELETE FROM budgets WHERE product=? AND period=? AND consumer_id IS ?",
            (product, period, consumer_id))
        self.connection.execute(
            "INSERT INTO budgets VALUES(?,?,?,?)",
            (product, consumer_id, period, str(value)))

    def get_budget(self, product, period, consumer_id=None):
        row = self.connection.execute(
            "SELECT amount FROM budgets WHERE product=? AND period=? AND consumer_id IS ?",
            (product, period, consumer_id)).fetchone()
        if row is None and consumer_id is not None:
            row = self.connection.execute(
                "SELECT amount FROM budgets WHERE product=? AND period=? AND consumer_id IS NULL",
                (product, period)).fetchone()
        return Decimal(row[0]) if row else None

    def assess(self, allocation, period, elapsed_ratio=None):
        """按归属结果评估风险；elapsed_ratio 为期间已过比例（0,1]，用于线性预测。"""
        rows, unbudgeted = [], []
        for product, entry in allocation["products"].items():
            for consumer, slice_ in entry["consumers"].items():
                consumer_key = None if consumer == "__PRODUCT__" else consumer
                actual = Decimal(slice_["cost"]["total"])
                budget = self.get_budget(product, period, consumer_key)
                rows.append(self._row(product, consumer_key, actual, budget, elapsed_ratio))
                if budget is None and self.get_budget(product, period) is None:
                    unbudgeted.append({"product": product, "consumer": consumer})
            actual = Decimal(entry["cost"]["total"])
            budget = self.get_budget(product, period)
            rows.append(self._row(product, None, actual, budget, elapsed_ratio))
        return {"period": period, "as_of": allocation.get("as_of"),
                "thresholds": {"watch": str(self.watch), "breach": str(self.breach)},
                "assessments": rows, "unbudgeted": unbudgeted}

    def _row(self, product, consumer, actual, budget, elapsed_ratio):
        row = {"product": product, "consumer": consumer,
               "actual": str(actual), "budget": None, "usage_ratio": None,
               "forecast": None, "forecast_ratio": None, "level": "unbudgeted"}
        if budget is None:
            return row
        ratio = actual / budget
        forecast = actual / Decimal(str(elapsed_ratio)) if elapsed_ratio else None
        forecast_ratio = forecast / budget if forecast is not None else None
        reference = forecast_ratio if forecast_ratio is not None else ratio
        if reference >= self.breach:
            level = "breach"
        elif reference >= self.watch:
            level = "watch"
        else:
            level = "ok"
        row.update({"budget": str(budget), "usage_ratio": str(ratio),
                    "forecast": str(forecast) if forecast is not None else None,
                    "forecast_ratio": str(forecast_ratio)
                    if forecast_ratio is not None else None, "level": level})
        return row

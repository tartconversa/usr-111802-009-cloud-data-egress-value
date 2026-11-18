"""跨云传输与调用费用的归属引擎。

归属原则：
- 费用先分直接归属（账单已指明产品/使用方）与共享池（账户或链路汇总费用）。
- 共享池按成本动因分摊：传输费按字节、请求费按可计费单元
  （标准调用=1、重试调用=1、批量调用=条目数），绝不按请求数平摊。
- 重试调用真实产生流量与费用，单独计数但同样参与字节/单元分摊；
  批量调用按条目数形成权重，避免被当作一次普通请求。
- 找不到成本动因或主体无法映射的费用进入未归属清单，不做静默吞掉。
- 全程 Decimal 计算，最大余数法按币种最小单位（分）分配，账户级对账闭环。
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR

from .contracts import validate_event

QUANTUM = Decimal("0.01")
_ZERO = Decimal("0")


def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def quantize_money(amount):
    return amount.quantize(QUANTUM)


def largest_remainder(amount, weights):
    """按整数权重把金额分到各键，含负值（抵扣）；返回 (分摊, 残余)。"""
    clean = {key: Decimal(value) for key, value in weights.items() if Decimal(value) > 0}
    total = sum(clean.values(), _ZERO)
    if not clean or total <= 0:
        return {}, amount
    sign = -1 if amount < 0 else 1
    base = abs(amount)
    exact = {key: base * weight / total for key, weight in clean.items()}
    units = {key: (value / QUANTUM).to_integral_value(rounding=ROUND_FLOOR)
             for key, value in exact.items()}
    allocated_units = sum(units.values(), _ZERO)
    leftover = int((base / QUANTUM - allocated_units).to_integral_value())
    ranked = sorted(clean, key=lambda key: (-(exact[key] - units[key] * QUANTUM), str(key)))
    for index in range(leftover):
        units[ranked[index]] += 1
    shares = {key: sign * units[key] * QUANTUM for key in units if units[key] != 0}
    residual = amount - sum(shares.values(), _ZERO)
    return shares, residual


def _new_volume():
    return {
        "calls": {"standard": 0, "retry": 0, "batch": 0},
        "demand_requests": 0, "retry_requests": 0, "batch_items": 0,
        "billable_units": 0, "bytes": 0, "request_payload_bytes": 0,
    }


class CostAllocator:
    def __init__(self, store, entities, products, pricing, config):
        self.store = store
        self.entities = entities
        self.products = products
        self.pricing = pricing
        self.config = config
        self.bytes_per_gb = Decimal(config["bytes_per_gb"])

    # ---- 事件装载与口径校验 ----------------------------------------------
    def _load(self, period_start, period_end, as_of):
        start, end = _parse(period_start), _parse(period_end)
        requests, transfers, charges = [], [], []
        unmatched = []
        for event in self.store.current(as_of):
            kind = event["kind"]
            normalized = validate_event(kind, event["payload"], self.config)
            if kind == "request" or kind == "transfer":
                # 调用与流量按实际发生时间归属期间。
                if not (start <= _parse(event["occurred_at"]) < end):
                    continue
            else:
                # 账单在次月才到达，归属期间由费用载荷中的业务期间决定。
                if not (_parse(normalized["period_start"]) < end and
                        _parse(normalized["period_end"]) > start):
                    continue
            if kind == "request":
                consumer = self.entities.resolve(event["source"], event["entity"], as_of)
                if consumer is None:
                    unmatched.append(self._ref(event, "主体未映射"))
                    continue
                requests.append(normalized | {"consumer": consumer,
                                              "occurred_at": event["occurred_at"]})
            elif kind == "transfer":
                consumer = self.entities.resolve(event["source"], event["entity"], as_of)
                if consumer is None:
                    unmatched.append(self._ref(event, "主体未映射"))
                    continue
                transfers.append(normalized | {"consumer": consumer,
                                               "occurred_at": event["occurred_at"]})
            else:
                charges.append(normalized | {"source": event["source"],
                                             "event_id": event["event_id"]})
        return requests, transfers, charges, unmatched

    @staticmethod
    def _ref(event, reason):
        return {"source": event["source"], "event_id": event["event_id"], "reason": reason}

    # ---- 主入口 -----------------------------------------------------------
    def allocate(self, period_start, period_end, as_of=None):
        requests, transfers, charges, unmatched = self._load(period_start, period_end, as_of)
        volume = defaultdict(lambda: defaultdict(_new_volume))
        self._accumulate_requests(requests, volume)
        self._accumulate_transfers(transfers, volume)

        ledger = []
        unattributed = []
        pools = []
        for charge in charges:
            charge["amount"] = quantize_money(charge["amount"])
            if charge["product"] is not None:
                self._apply_direct(charge, ledger, unattributed)
            else:
                self._apply_pool(charge, requests, transfers, ledger, unattributed, pools)

        expected, gaps = self._expected_costs(requests, transfers, as_of)
        products = self._compose_products(volume, ledger, expected)
        reconciliation = self._reconcile(charges, ledger, unattributed)
        totals = {
            "billed": str(sum((c["amount"] for c in charges), _ZERO)),
            "allocated": str(sum((Decimal(line["amount"]) for line in ledger), _ZERO)),
            "unattributed": str(sum((Decimal(line["amount"]) for line in unattributed), _ZERO)),
            "credits": str(sum((c["amount"] for c in charges if c["credit"]), _ZERO)),
            "expected": str(sum(expected.values(), _ZERO)) if expected else None,
        }
        return {
            "period": {"start": period_start, "end": period_end},
            "as_of": as_of, "currency": self.config["currency"],
            "totals": totals, "products": products,
            "unattributed": unattributed, "unmatched_events": unmatched,
            "pricing_gaps": gaps, "pools": pools,
            "reconciliation": reconciliation,
        }

    # ---- 用量归集 ---------------------------------------------------------
    def _accumulate_requests(self, requests, volume):
        for request in requests:
            bucket = volume[request["product"]][request["consumer"]]
            bucket["calls"][request["call_class"]] += 1
            if request["call_class"] == "retry":
                bucket["retry_requests"] += 1
                bucket["billable_units"] += 1
            elif request["call_class"] == "batch":
                bucket["batch_items"] += request["items"]
                bucket["billable_units"] += request["items"]
                bucket["demand_requests"] += 1
            else:
                bucket["billable_units"] += 1
                bucket["demand_requests"] += 1
            if request["bytes"] is not None:
                bucket["request_payload_bytes"] += request["bytes"]

    def _accumulate_transfers(self, transfers, volume):
        for transfer in transfers:
            product = transfer["product"]
            if product is None:
                # 与费用归属同一判定：仅单产品账户的共享流量可归入该产品
                candidates = self.products.account_products(transfer["account"])
                if len(candidates) != 1:
                    continue
                product = candidates[0]
            volume[product][transfer["consumer"]]["bytes"] += transfer["bytes"]

    # ---- 费用归属 ---------------------------------------------------------
    def _apply_direct(self, charge, ledger, unattributed):
        product = charge["product"]
        if self.products.get(product) is None:
            unattributed.append(self._unattributed(charge, "产品不在目录"))
            return
        consumer = charge["consumer_id"]
        if consumer is not None and self.entities.connection.execute(
                "SELECT 1 FROM entities WHERE entity_id=?", (consumer,)).fetchone() is None:
            unattributed.append(self._unattributed(charge, "使用方主体不存在"))
            return
        ledger.append(self._line(charge, product, consumer))

    def _apply_pool(self, charge, requests, transfers, ledger, unattributed, pools):
        basis = charge["allocation_basis"]
        weights = self._pool_weights(charge, basis, requests, transfers)
        shares, residual = largest_remainder(charge["amount"], weights)
        for (product, consumer), amount in sorted(shares.items()):
            ledger.append(self._line(charge, product, consumer, amount))
        pool_info = {
            "account": charge["account"], "route_id": charge["route_id"],
            "charge_type": charge["charge_type"], "allocation_basis": basis,
            "amount": str(charge["amount"]), "weight_total": str(sum(weights.values(), _ZERO)),
            "weights": {f"{product}|{consumer}": str(weight)
                        for (product, consumer), weight in sorted(weights.items())},
        }
        pools.append(pool_info)
        if residual != 0 or not shares:
            reason = "缺少成本动因" if not shares else "分摊尾差"
            unattributed.append(self._unattributed(charge, reason, residual if shares else None))

    def _pool_weights(self, charge, basis, requests, transfers):
        weights = defaultdict(Decimal)
        if basis == "bytes":
            for transfer in transfers:
                if not self._scope_match(charge, transfer["account"], transfer.get("route_id"),
                                         transfer.get("product")):
                    continue
                product = transfer["product"]
                if product is None:
                    # 链路流量未指明产品：仅当账户目录下恰好一个产品时才可切分，
                    # 多产品账户无可靠动因，该部分不产生权重并落入未归属。
                    candidates = self.products.account_products(charge["account"])
                    if len(candidates) != 1:
                        continue
                    product = candidates[0]
                weights[(product, transfer["consumer"])] += transfer["bytes"]
        else:
            for request in requests:
                account = self.products.account(request["product"])
                if not self._scope_match(charge, account, request.get("route_id"),
                                         request["product"]):
                    continue
                weights[(request["product"], request["consumer"])] += self._request_units(request)
        return dict(weights)

    def _scope_match(self, charge, account, route_id, product):
        if account != charge["account"]:
            return False
        if charge["route_id"] is not None and route_id != charge["route_id"]:
            return False
        if charge["product"] is not None and product != charge["product"]:
            return False
        return True

    @staticmethod
    def _request_units(request):
        if request["call_class"] == "batch":
            return Decimal(request["items"])
        return Decimal(1)

    def _line(self, charge, product, consumer, amount=None):
        return {
            "product": product, "consumer": consumer,
            "account": charge["account"], "route_id": charge["route_id"],
            "charge_type": charge["charge_type"], "allocation_basis": charge["allocation_basis"],
            "pricing_version": charge["pricing_version"], "credit": charge["credit"],
            "amount": str(amount if amount is not None else charge["amount"]),
            "source_event": charge["event_id"],
        }

    def _unattributed(self, charge, reason, amount=None):
        return {
            "account": charge["account"], "route_id": charge["route_id"],
            "product": charge["product"], "charge_type": charge["charge_type"],
            "allocation_basis": charge["allocation_basis"], "credit": charge["credit"],
            "amount": str(charge["amount"] if amount is None else amount),
            "reason": reason, "source_event": charge["event_id"],
        }

    # ---- 计价版本期望成本 -------------------------------------------------
    def _expected_costs(self, requests, transfers, as_of):
        expected = defaultdict(Decimal)
        gaps = []
        for request in requests:
            version = self.pricing.resolve(request["product"], "request",
                                           request["occurred_at"], as_of=as_of)
            if version is None:
                gaps.append({"product": request["product"], "fee_type": "request",
                             "usage_time": request["occurred_at"]})
                continue
            expected[(request["product"], request["consumer"])] += self.pricing.expected_amount(
                version, self._request_units(request))
        for transfer in transfers:
            if transfer["product"] is None:
                continue
            version = self.pricing.resolve(transfer["product"], "transfer",
                                           transfer["occurred_at"],
                                           direction=transfer["direction"], as_of=as_of)
            if version is None:
                gaps.append({"product": transfer["product"], "fee_type": "transfer",
                             "usage_time": transfer["occurred_at"],
                             "direction": transfer["direction"]})
                continue
            gb = Decimal(transfer["bytes"]) / self.bytes_per_gb
            expected[(transfer["product"], transfer["consumer"])] += \
                self.pricing.expected_amount(version, gb)
        return expected, gaps

    # ---- 产品口径组装 -----------------------------------------------------
    def _compose_products(self, volume, ledger, expected):
        cost = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
        credits = defaultdict(lambda: defaultdict(Decimal))
        for line in ledger:
            cost[line["product"]][line["consumer"]][line["charge_type"]] += Decimal(line["amount"])
            cost[line["product"]]["__ALL__"][line["charge_type"]] += Decimal(line["amount"])
            if line["credit"]:
                credits[line["product"]][line["consumer"]] += Decimal(line["amount"])
                credits[line["product"]]["__ALL__"] += Decimal(line["amount"])
        result = {}
        for product in sorted(set(volume) | set(cost)):
            entry = {"owner": None, "account": None, "consumers": {}}
            directory = self.products.get(product)
            if directory:
                entry["owner"], entry["account"] = directory["owner"], directory["account"]
            consumers = set(volume.get(product, {})) | {
                consumer for consumer in cost.get(product, {}) if consumer != "__ALL__"}
            for consumer in sorted(consumers, key=lambda value: value or ""):
                entry["consumers"][consumer or "__PRODUCT__"] = self._slice(
                    volume.get(product, {}).get(consumer, _new_volume()),
                    cost.get(product, {}).get(consumer, {}),
                    expected.get((product, consumer)),
                    credits.get(product, {}).get(consumer, _ZERO))
            entry.update(self._slice(
                self._merge_volume(volume.get(product)),
                cost.get(product, {}).get("__ALL__", {}),
                sum((v for (p, c), v in expected.items() if p == product), _ZERO) or None,
                credits.get(product, {}).get("__ALL__", _ZERO)))
            result[product] = entry
        return result

    def _merge_volume(self, product_volume):
        merged = _new_volume()
        if not product_volume:
            return merged
        for bucket in product_volume.values():
            for key in ("demand_requests", "retry_requests", "batch_items",
                        "billable_units", "bytes", "request_payload_bytes"):
                merged[key] += bucket[key]
            for call_class, count in bucket["calls"].items():
                merged["calls"][call_class] += count
        return merged

    def _slice(self, volume_bucket, cost_by_type, expected, credit_total):
        transfer_cost = cost_by_type.get("transfer", _ZERO)
        request_cost = cost_by_type.get("request", _ZERO)
        total = transfer_cost + request_cost
        gb = Decimal(volume_bucket["bytes"]) / self.bytes_per_gb
        billable = Decimal(volume_bucket["billable_units"])
        demand = Decimal(volume_bucket["demand_requests"])
        unit = {
            "per_demand_request": str(total / demand) if demand else None,
            "per_billable_unit": str(total / billable) if billable else None,
            "transfer_per_gb": str(transfer_cost / gb) if gb else None,
        }
        return {
            "volume": volume_bucket | {"gb": str(gb)},
            "cost": {"total": str(total), "transfer": str(transfer_cost),
                     "request": str(request_cost),
                     "credits": str(credit_total)},
            "expected_cost": str(expected) if expected is not None else None,
            "unit_cost": unit,
        }

    # ---- 对账与差异分解 ---------------------------------------------------
    def _reconcile(self, charges, ledger, unattributed):
        billed = defaultdict(Decimal)
        for charge in charges:
            billed[charge["account"]] += charge["amount"]
        allocated = defaultdict(Decimal)
        for line in ledger:
            allocated[line["account"]] += Decimal(line["amount"])
        unclaimed = defaultdict(Decimal)
        for line in unattributed:
            unclaimed[line["account"]] += Decimal(line["amount"])
        rows = []
        for account in sorted(billed):
            balanced = billed[account] == allocated[account] + unclaimed[account]
            rows.append({"account": account, "billed": str(billed[account]),
                         "allocated": str(allocated[account]),
                         "unattributed": str(unclaimed[account]), "balanced": balanced})
        return rows

    def variance(self, current, baseline):
        """量差/价差分解：解释费用上涨来自服务量还是单位成本恶化。"""
        result = {}
        for product, row in current["products"].items():
            base = baseline["products"].get(product)
            if base is None:
                result[product] = {"status": "new_product"}
                continue
            cur_cost = Decimal(row["cost"]["total"])
            base_cost = Decimal(base["cost"]["total"])
            cur_units = Decimal(row["volume"]["demand_requests"])
            base_units = Decimal(base["volume"]["demand_requests"])
            cur_rate = Decimal(row["unit_cost"]["per_demand_request"]) \
                if row["unit_cost"]["per_demand_request"] else _ZERO
            base_rate = Decimal(base["unit_cost"]["per_demand_request"]) \
                if base["unit_cost"]["per_demand_request"] else _ZERO
            volume_effect = (cur_units - base_units) * base_rate
            rate_effect = (cur_rate - base_rate) * cur_units
            result[product] = {
                "status": "compared",
                "cost_delta": str(cur_cost - base_cost),
                "volume_effect": str(volume_effect),
                "rate_effect": str(rate_effect),
                "explained": str(volume_effect + rate_effect),
                "unit_rate_current": str(cur_rate), "unit_rate_baseline": str(base_rate),
                "deterioration": rate_effect > 0,
            }
        return result

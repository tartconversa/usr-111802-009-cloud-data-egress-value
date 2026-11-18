"""request / transfer / charge 三类事件的载荷业务契约。

事件的信封契约（来源、事件号、修订、时间）由 store.EventStore 负责；
本模块只校验承载成本分析口径的载荷字段。所有金额与数量都要求有限、
非 NaN，并以字符串或数字给出，金额按币种最小单位由调用方量化。
"""
from decimal import Decimal, InvalidOperation

def _require(payload, key, kind):
    value = payload.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) or value == "":
        raise ValueError(f"字段 {key} 缺失或类型无效")
    return value

def _text(payload, key):
    value = _require(payload, key, str)
    if not value.strip():
        raise ValueError(f"字段 {key} 不能为空")
    return value

def _non_negative(payload, key):
    raw = payload.get(key)
    if isinstance(raw, bool):
        raise ValueError(f"字段 {key} 无效")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"字段 {key} 无效") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"字段 {key} 必须为非负有限数")
    return value

def _optional_text(payload, key):
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"字段 {key} 必须为非空字符串")
    return value

def validate_request(payload, call_classes):
    """调用事件：调用类别决定计费单元，重试与批量不能按请求数平摊。"""
    product = _text(payload, "product")
    call_class = _text(payload, "call_class")
    if call_class not in call_classes:
        raise ValueError("未知调用类别")
    items = _non_negative(payload, "items")
    if call_class != "batch" and items != 0:
        raise ValueError("仅批量调用允许携带条目数")
    if call_class == "batch" and items == 0:
        raise ValueError("批量调用必须携带正条目数")
    if items != items.to_integral_value():
        raise ValueError("批量条目数必须为整数")
    return {
        "product": product,
        "call_class": call_class,
        "items": int(items),
        "bytes": int(_non_negative(payload, "bytes")) if "bytes" in payload else None,
        "route_id": _optional_text(payload, "route_id"),
    }

def validate_transfer(payload, directions):
    """传输量事件：共享链路在一段期间内按字节记录真实流量。"""
    product = _optional_text(payload, "product")
    account = _text(payload, "account")
    direction = _text(payload, "direction")
    if direction not in directions:
        raise ValueError("未知传输方向")
    period_start = _text(payload, "period_start")
    period_end = _text(payload, "period_end")
    if period_end < period_start:
        raise ValueError("传输期间结束时间早于开始时间")
    return {
        "product": product,
        "account": account,
        "route_id": _optional_text(payload, "route_id"),
        "direction": direction,
        "bytes": int(_non_negative(payload, "bytes")),
        "period_start": period_start,
        "period_end": period_end,
    }

def validate_charge(payload, charge_types, allocation_bases, default_bases):
    """账单费用事件：账户汇总、计价版本化；抵扣以负金额表达。"""
    account = _text(payload, "account")
    charge_type = _text(payload, "charge_type")
    if charge_type not in charge_types:
        raise ValueError("未知费用类型")
    amount = _non_negative(payload, "amount")
    credit = payload.get("credit", False)
    if not isinstance(credit, bool):
        raise ValueError("抵扣标记无效")
    signed = -amount if credit else amount
    basis = payload.get("allocation_basis")
    if basis is None:
        basis = default_bases[charge_type]
    if basis not in allocation_bases:
        raise ValueError("未知分摊基准")
    period_start = _text(payload, "period_start")
    period_end = _text(payload, "period_end")
    if period_end < period_start:
        raise ValueError("账单期间结束时间早于开始时间")
    result = {
        "account": account,
        "route_id": _optional_text(payload, "route_id"),
        "product": _optional_text(payload, "product"),
        "consumer_id": _optional_text(payload, "consumer_id"),
        "charge_type": charge_type,
        "amount": signed,
        "credit": credit,
        "allocation_basis": basis,
        "pricing_version": _optional_text(payload, "pricing_version"),
        "period_start": period_start,
        "period_end": period_end,
    }
    if result["product"] is None and result["consumer_id"] is not None:
        raise ValueError("直接归属到使用方的费用必须同时给出产品")
    return result

def validate_event(kind, payload, config):
    if kind == "request":
        return validate_request(payload, config["call_classes"])
    if kind == "transfer":
        return validate_transfer(payload, config["directions"])
    if kind == "charge":
        return validate_charge(payload, config["charge_types"],
                               config["allocation_bases"], config["charge_basis_default"])
    raise ValueError("未知事件类型")

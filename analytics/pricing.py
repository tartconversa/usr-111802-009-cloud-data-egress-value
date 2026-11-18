"""版本化计价表：业务生效期与获知时间双时间管理。

rate 的单位随 fee_type 区分：transfer 为每 GB 的币种金额，
request 为每个可计费单元（标准/重试各一、批量按条目数）的金额。
tiered 阶梯按累计用量分段计价；observed_at 之后才可见，
因此历史月报可按当时已知的计价版本复现。
"""
from decimal import Decimal

def _money(value, name):
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name}无效") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name}必须为非负有限数")
    return parsed

class PricingBook:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS pricing_versions (
            version_id TEXT NOT NULL, product TEXT NOT NULL,
            cloud TEXT, direction TEXT NOT NULL, fee_type TEXT NOT NULL,
            valid_from TEXT NOT NULL, valid_to TEXT, rate TEXT NOT NULL,
            tiered TEXT NOT NULL, observed_at TEXT NOT NULL,
            PRIMARY KEY (version_id))""")
        connection.execute("""CREATE INDEX IF NOT EXISTS pricing_lookup
            ON pricing_versions(product, fee_type, direction, valid_from)""")

    def register(self, version_id, product, direction, fee_type, valid_from,
                 observed_at, rate="0", valid_to=None, cloud=None, tiered=None):
        if not all(isinstance(v, str) and v.strip()
                   for v in (version_id, product, direction, fee_type, valid_from, observed_at)):
            raise ValueError("计价版本信息不能为空")
        if valid_to is not None and valid_to < valid_from:
            raise ValueError("计价生效结束时间早于开始时间")
        rate_value = _money(rate, "费率")
        if tiered is None:
            tiers = []
        else:
            if not isinstance(tiered, list) or not tiered:
                raise ValueError("阶梯计价必须为非空列表")
            tiers = []
            last = -1
            for tier in tiered:
                up_to = int(tier["up_to"])
                tier_rate = _money(tier["rate"], "阶梯费率")
                if up_to <= last:
                    raise ValueError("阶梯上界必须严格递增")
                tiers.append([up_to, str(tier_rate)])
                last = up_to
        import json
        payload = json.dumps(tiers, ensure_ascii=False)
        if self.connection.execute("SELECT 1 FROM pricing_versions WHERE version_id=?",
                                   (version_id,)).fetchone():
            raise ValueError("计价版本编号已存在")
        self.connection.execute("""INSERT INTO pricing_versions
            (version_id,product,cloud,direction,fee_type,valid_from,valid_to,rate,tiered,observed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (version_id, product, cloud, direction, fee_type, valid_from, valid_to,
             str(rate_value), payload, observed_at))
        return self.get(version_id)

    def get(self, version_id):
        row = self.connection.execute(
            "SELECT * FROM pricing_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            return None
        import json
        return dict(row) | {"tiered": json.loads(row["tiered"]),
                            "rate": Decimal(row["rate"])}

    def versions(self, product, fee_type, direction=None, as_of=None):
        """返回 as_of 时点已获知、且覆盖该业务时点的计价版本（按生效期倒序）。"""
        clauses = ["product=?", "fee_type=?"]
        params = [product, fee_type]
        if direction is not None:
            clauses.append("direction=?")
            params.append(direction)
        if as_of is not None:
            clauses.append("observed_at<=?")
            params.append(as_of)
        rows = self.connection.execute(
            "SELECT * FROM pricing_versions WHERE " + " AND ".join(clauses) +
            " ORDER BY valid_from DESC, observed_at DESC", params).fetchall()
        import json
        result = []
        for row in rows:
            result.append(dict(row) | {"tiered": json.loads(row["tiered"]),
                                       "rate": Decimal(row["rate"])})
        return result

    def resolve(self, product, fee_type, usage_time, direction=None, as_of=None):
        """按业务发生时间选生效版本；同点多版本取观察时间最新者。"""
        for version in self.versions(product, fee_type, direction, as_of):
            if version["valid_from"] <= usage_time and (
                    version["valid_to"] is None or usage_time < version["valid_to"]):
                return version
        return None

    def expected_amount(self, version, usage_units):
        """按版本计算期望金额。units：传输费为 GB 数，请求费为可计费单元数。"""
        units = Decimal(str(usage_units))
        if units < 0:
            raise ValueError("用量不能为负")
        tiers = version["tiered"]
        if not tiers:
            return units * version["rate"]
        total = Decimal("0")
        lower = Decimal("0")
        remaining = units
        for up_to, tier_rate in tiers:
            width = Decimal(up_to) - lower
            used = min(remaining, width)
            total += used * Decimal(tier_rate)
            remaining -= used
            lower = Decimal(up_to)
            if remaining <= 0:
                break
        if remaining > 0:
            total += remaining * Decimal(tiers[-1][1])
        return total

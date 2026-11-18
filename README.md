# 跨云数据产品数据服务

服务登记多来源事件、业务主体及现有摘要，并保存发布报告的原始内容。在事件溯源底座之上提供跨云传输费/调用费的成本归属、计价版本、单位成本、预算风险与修订影响分析。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json

## 模块职责

- `store.py`：带来源、修订号与双时间的原始事件存储；同来源同事件的高修订替代低修订。
- `contracts.py`：request/transfer/charge 三类事件的载荷业务契约，入库时强制校验。
- `identity.py`：外部身份到分析主体的映射，别名带 `linked_at` 以支持归属纠正的时点复现。
- `domain.py`：产品、负责人与云账户目录。
- `pricing.py`：双时间版本化计价表（生效期 + 获知时间），含阶梯计价与期望成本计算。
- `allocation.py`：成本归属引擎。传输费按字节、请求费按可计费单元（重试=1、批量按条目数）分摊共享池；最大余数法按分分配，账户级对账闭环；输出产品×使用方的单位成本、未归属费用、期望成本差异与量差/价差分解。
- `budget.py`：预算登记与 ok/watch/breach 风险判断，支持按期间进度线性外推。
- `amendments.py`：抵扣与归属纠正的修订登记；重算当前风险并标注受影响的历史报告及偏差。
- `decisions.py`：不可变报告发布（正文哈希），报告内嵌归属与风险快照。
- `metrics.py` / `service.py`：原有通用事件摘要与统一协调入口。

## 关键口径

重试与批量调用不按请求数平摊费用；共享链路费用必须找到字节或计费单元动因，否则进入未归属而非静默吞掉。月报发布后可按旧账单 `as_of` 逐字段复现；抵扣（独立负向事件）与纠正（同事件高修订）到达时只更新当前判断，并在修订记录中指出受影响的历史结论。详见 `docs/domain.md`。

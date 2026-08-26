# 第二轮修复清单（for Trae）

> 项目：LihuQuantify
> 背景：第一轮六项修复已全部落地（移动止盈接入、MA10 收盘判定、配置接线、轮次统计、网格+验证段、市场状态过滤），全流程（数据→策略→风控→回测→模拟盘→巡检→报告）已构建完毕。
> 本轮问题来源：对优化后代码的二次复查。**硬约束（不得修改）**：铁律数值——成本 -8% 强制止损、单票 ≤25%、同板块 ≤40%、同票月内 ≤3 次、连亏 3 笔停手 30 天、T+1、绝不向下补仓、买入单+止损条件单必须同时挂。禁止未来函数。保持事件驱动引擎架构。输出保留免责声明。

---

## 修复 A（最重要·统计纪律）：为"市场状态过滤"建立独立 holdout 验证

**现状**：`config/settings.yaml` 注释自述——训练段（50 只×2 年）最优组在验证段（2025-12~2026-08）为负（`outputs/grid_search_validation.csv` 三行全负），随后**用同一段验证数据**的分段统计（震荡段胜率 41.9%）推导出"仅上涨段开新仓"的过滤规则。验证段已被用来调规则，不再是干净的 out-of-sample；目前没有任何一段从未被看过的数据证明"市场过滤"本身有效。

**改法（不改代码，改流程与脚本）**：

1. 新增 `scripts/validate_market_filter.py`：
   - 选一个**从未参与过任何决策**的 holdout 段（例如 2023-01-01 ~ 2024-12-31 中未被网格训练/验证用过的一段），只跑"市场过滤 ON vs OFF"两组对照，**只输出结论，不据此调参**；
   - 若 holdout 段数据不足，用未来 1~3 个月的模拟盘运行（paper）作为 live validation，同样只记录不调参。
2. 稳健性检验（同一脚本）：市场过滤的阈值做敏感性——20 日涨幅阈值 3% 改为 2%/4%、状态窗口 20 日改为 60 日，输出各变体的收益/回撤/空仓天数对比。**若结论随阈值小幅变化而剧变 → 该规则脆弱，不能当铁律，降级为"参考信号"**。
3. 建立决策日志 `docs/决策日志.md`：每行记录"日期 / 基于哪段数据 / 改了哪条规则 / 该段数据此后永久失去验收资格"。每次改规则前先翻日志。

**验收**：
- holdout 验证产出结论（过滤有效 / 无效 / 脆弱）；
- 决策日志入库并坚持使用；
- 若过滤被判脆弱：将 `market_filter` 默认改为 false，或以"过滤仅作降仓信号（仓位减半）"替代"完全禁止开仓"。

---

## 修复 B：模拟盘状态持久化（进程重启不丢账户）

**现状**：`src/lihu_quantify/execution/paper_trade.py` 的 `cash/positions/trades` 全在内存；`monitor/scheduler.py` 每次 `scan()` 新建 OMS，`stop_registry` 从持仓重建且只按"成本-8%"重建（丢失原始止损价与 MA10 信息）。进程重启 → 本金、持仓、止损登记全部归零，模拟盘连续验证的语义被破坏。

**改法**：

1. `PaperBroker` 增加持久化：
   - 状态文件 `data/paper_state.json`：`{cash, init_capital, positions:{ts_code:{volume, available, today_bought, cost}}, trades:[...], trade_day}`；
   - 每次 `buy/sell/on_new_day` 后原子写入（先写临时文件再替换）；构造函数支持 `state_file` 路径，启动时加载恢复；
   - `query_positions/query_asset` 逻辑不变（状态来自持久化后的内存镜像）。
2. OMS 止损登记持久化：`data/stop_registry.json`，字段含原始 `stop_price`、`reason`、`buy_order_id`；`rebuild_stops_from_positions()` 优先从持久化文件恢复，文件缺失才回退"成本-8%"重建。
3. `DailyScanner.scan()` 复用同一 broker 实例时不再重建 OMS（OMS 作为 scanner 属性持有，stop_registry 跨日保留）。

**验收**：
- 单元测试：买入→序列化→新建 broker 加载→持仓/现金/止损登记完全一致；
- 模拟运行两天（手动调用两次 scan 或直接调 buy/sell）后重启进程，账户状态不变。

---

## 修复 C：板块集中度 ≤40% 铁律真正生效（目前空转）

**现状**：`backtest/engine.py` 的 `sector_by_code=None`；`monitor/scheduler.py` 的 `CheckContext(sector="")`；`risk/checklist.py` `_check_sector` 对未知板块直接放行。**"同板块 ≤40%"铁律在回测与实盘链路均未生效。** Tushare `stock_basic` 自带 `industry` 字段（`_universe` 已拉取该接口）。

**改法**：

1. 在 `DailyScanner` 与 `run_backtest.py`、`scripts/grid_search.py` 中，从 `stock_basic` 构建 `sector_by_code = {ts_code: industry}`（industry 为空/NaN 的用"未分类"占位，不因未知放行，按同一板块累计）；
2. `engine.run(sector_by_code=...)` 传入；`CheckContext(sector=sector_by_code.get(code, ""))` 传入；
3. `checklist._check_sector` 保持现有逻辑（有 sector 即强制校验 ≤40%）。

**验收**：
- 回测中构造两只同 industry 的持仓 + 第三只同板块信号 → Checklist 第 2 项拒绝；
- 巡检链路同样校验。

---

## 修复 D：止损口径统一（回测 / 模拟盘 / 实盘三种口径对齐）

**现状（三处不一致）**：
- 回测：收盘触发 → 次日开盘成交（`engine.py` 撮合）；
- 模拟盘：`scheduler.py _check_stops_with_alert` 用当日收盘价**即时成交**；
- 实盘（QMT 条件单，未接）：盘中触发即时成交；
- 且模拟盘/OMS **只有价格止损，无 MA10 破位检查**（回测有 `ma_break`，且 MA10 动态；模拟盘止损价是信号日一次性固定值）。

**改法**：

1. 明确三种口径并写入注释/文档：回测=次日开盘、模拟盘=次日开盘（改为与回测一致）、实盘=盘中条件单。模拟盘的 `_check_stops_with_alert` 改为：当日收盘判断触发 → 记入待执行队列 → 次日（下一次 scan 的开盘价，Tushare 无盘中数据时用次日 open 字段）执行卖出；
2. 模拟盘每日增加 **MA10 破位检查**：收盘价 < MA10 → 触发 `ma_break` 离场（与 `stop_loss.py` 的判定一致，用当日最新 daily 数据计算 MA10）；
3. OMS 的止损登记保存时记录 `stop_price` 的构成（成本-8% 与 MA10 取先到先走），重建时恢复完整语义。

**验收**：
- 单元测试：构造"收盘触发止损"场景，模拟盘应在**次日**成交而非当日；
- 模拟盘出现收盘破 MA10 的持仓时产生离场动作；
- 文档注明三种口径差异，复盘时按口径对齐解释偏差。

---

## 修复 E：巡检执行价统一用最新交易日收盘价

**现状**：`monitor/scheduler.py` 第 168 行 `price = sig.suggested_price or last_bar["close"]`——freshness=3 时信号可能是 2~3 天前产生的，买入价与 Checklist 检查价用的是**信号日旧价**。

**改法**：执行价与 `CheckContext.current_price` 一律用 `last_bar["close"]`（最新交易日收盘），`sig.suggested_price` 仅作信号记录展示。

**验收**：构造"信号产生于 2 天前"的场景，买入价等于最新收盘价。

---

## 修复 F：连亏 3 笔停手状态机接线（铁律至今未落地）

**现状**：`halted_until` 全仓库只有初始化（None）与读取（`risk/frequency.py`、`risk/checklist.py`），**从未被赋值**。回测与模拟盘都没有"连亏 3 笔停手一个月"。

**改法**：

1. 回测：`backtest/portfolio.py` 在每笔卖出 `apply_fill` 后统计该 ts_code 的连续亏损笔数（复用 `consecutive_losses`），达到 `halt_after_consec_losses=3` → `self.halted_until = 卖出日 + timedelta(days=halt_days)`；
2. 模拟盘：`PaperBroker` 同样在 sell 时更新 `halted_until`（持久化进 state 文件），`DailyScanner.scan()` 开仓前检查（Checklist `_check_frequency` 已读该字段，设上即自动生效）；
3. 注意语义：连亏按"同一只票"统计（铁律原文"连亏 3 笔，停手一个月"），停手范围按你们之前的约定——建议停手该票一个月（最小语义），如需全局停手在 settings 加开关。

**验收**：构造同票连亏 3 笔 → 回测与模拟盘均拒绝第 4 笔买入并显示"停手至 xx"；第 30 天后方可再次开仓。

---

## 修复 G：每日报告"铁律自检"动态化

**现状**：`outputs/reports/2026-08-25.md` 中"铁律自检"为静态 `[x]` 模板文本。

**改法**：`monitor/report.py` 的自检段改为运行时真实校验并输出：
- OMS 止损登记与持仓一一对应（每个持仓都有 stop 记录，且 volume 一致）；
- 单票市值占比 ≤25%（逐仓计算）；
- 当日无向下补仓行为（检查 buy 记录对已有亏损持仓）；
- 连亏停手状态（halted_until）；
- 任一不满足输出 `[ ]` 并触发告警。

**验收**：人为删除一个止损登记后重跑 report，自检段出现 `[ ]` 项并触发告警。

---

## 修复 H：小修合集

1. `backtest/metrics.py _pair_rounds`：轮次盈亏未扣**买入侧佣金**（只扣了卖出侧佣金+印花税）——`consume` 部分按比例加扣买入单的 `commission`；
2. `monitor/scheduler.py`：注册 `monthly_review` 任务（cron 来自 `settings.scheduler.monthly_review_cron`，月末生成月度复盘报告，填充 `docs/月度复盘模板.md` 字段）；
3. 报告输出路径统一：`_ROOT / "outputs"`（当前历史报告出现在 `src/outputs/`，为旧路径产物，迁移并固定路径）；
4. `_universe` 补充硬过滤：接上 `universe.min_list_days`（上市≥60 日）与 `min_avg_amount_20d`（近 20 日日均成交额≥1 亿）——目前巡检池只做了前缀/ST 过滤。

---

## 修复 I：网格扩边 + 稳健性报告（统计优化，不急于执行）

**现状**：最优 `close_to_ma5_max_dev=0.05` 落在网格**边界**上（0.03→0.05 收益跳升）；chase 维度 0.05→0.08 收益从 0.188 跳到 0.502，**面不平滑**——单点最优不可靠。

**改法**（在修复 A 的 holdout 结论出来后再做）：
1. 网格扩展 `ma5_dev` 至 {0.03, 0.05, 0.08, 0.10}、freshness {2,3,5}、chase {0.06,0.08,0.10}；
2. 报告不只报单点最优，输出"稳健区域"（相邻组合均正的连续区域）与"参数平坦度"（最优±邻格的收益衰减曲线）；
3. 训练段/验证段/ holdout 三段划分严格固定，任何一段只允许使用一次结论。

---

## 修复 J：股票池扩展路线图（下一步，非本轮必做）

**现状**：训练池=巡检池=`stock_basic` 按代码排序 `head(50)`（深市老牌股为主，与策略原型的"短线强势股/涨停回马枪"场景错配）。优点：训练与实盘池一致（已做对）；缺点：池本身有偏。

**路线图**：
1. 先按**成交额分层抽样**扩到 200~300 只（主板 + 剔除 ST/次新，20 日日均成交额 ≥1 亿）；
2. **训练、验证、巡检三处同步换池**，网格重跑（修复 I 的扩展网格）；
3. 对比新旧池的回测结论，若换池后稳健区域消失 → 说明原结论是池子偏差，回到策略逻辑层面再迭代。

---

## 实施顺序建议

**A（holdout 验证）→ B（持久化）→ C（板块映射）→ F（连亏停手）→ D（口径统一）→ E（执行价）→ G（报告动态化）→ H（小修）**
（I、J 等 A 的结论出来后再启动，避免在未验证的地基上继续调参。）

## 回归基线（不可破坏，同第一轮）

- 九转序列 / MACD 背离 / 蜡烛图预警 / Checklist 长电案例 四个黄金用例；
- T+1、不向下补仓、-8% 强制止损行为不变；
- 第一轮修复的移动止盈、MA10 收盘判定、信号 1 日有效必须保持。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

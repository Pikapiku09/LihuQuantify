# 第三轮修复清单（for Trae）

> 项目：LihuQuantify
> 背景：第二轮修复已全部完成——holdout 独立验证（`outputs/market_filter_holdout.csv`）、决策日志（`docs/决策日志.md`）、分层 200 池三处同步（`data/pool.py` + `pool_mode=strat`）、市场过滤 block/reduce 两档、扩边网格（`scripts/grid_search_v2.py`）、状态持久化（`data/paper_state.json`）、待执行止损队列、板块映射、月度复盘任务、前端监控看板（`web/`）。
> 本轮问题来源：对第二轮成果的第三次复查（重点：统计口径一致性 + 看板展示正确性）。
> **硬约束（不得修改）**：铁律数值——成本 -8% 强制止损、单票 ≤25%、同板块 ≤40%、同票月内 ≤3 次、连亏 3 笔停手 30 天、T+1、绝不向下补仓、买入单+止损条件单必须同时挂。禁止未来函数。保持事件驱动引擎架构。输出保留免责声明。
> **统计纪律（第二轮已确立，必须延续）**：任何一段数据一旦用于调规则，永久失去验收资格；改规则前查 `docs/决策日志.md`；网格最优格只是"训练段参考"，不直接用于实盘。

---

## 修复 A（最高优先）：回测侧"连亏 3 笔停手"接线 + grid v2 重跑对齐

**现状**：`src/lihu_quantify/backtest/portfolio.py` 的 `halted_until` 只有初始化与快照输出，**从未赋值**；`consecutive_losses()` 存在但无人调用。PaperBroker（模拟盘/实盘路径）已实现 `halt_map`，但回测没有——**回测结果不含这条铁律，系统性偏乐观，且与实盘口径不一致**。grid v2 的全部数字都是这个口径下跑出来的。

**改法**：

1. `portfolio.py` 的 `apply_fill()` 卖出分支末尾追加：

```python
# 修复F(第二轮)补漏：连亏3笔 → 停手30天（与 PaperBroker.halt_map 同语义）
if self.consecutive_losses(code) >= 3:
    from datetime import timedelta
    self.halted_until = fill.fill_date + timedelta(days=30)
    logger.warning(f"[铁律F] {code} 连亏3笔，停手至 {self.halted_until}")
```

2. 确认引擎侧已生效：`ChecklistGate._check_frequency` 已读 `account.halted_until`（无需改）；只需确认 `engine.run` 在每次 Checklist 检查前用的是最新 `portfolio.to_snapshot(...)`（当前已是）。
3. **重跑** `scripts/grid_search_v2.py` 与 `scripts/validate_market_filter.py`，生成新结果文件（建议输出名加 `_halt` 后缀），并更新 `outputs/` 中的稳健性报告。

**验收**：
- 单元测试：构造同票连亏 3 笔 → 第 4 笔信号被 Checklist 拒绝并显示"停手至 xx"；
- 新旧 grid 结果对比记录在决策日志（预期：含停手后收益略降、更接近实盘口径）。

---

## 修复 B：股票池幸存者偏差处理（strat200 数字打折扣）

**现状**：`data/pool.py` 用**当前** `stock_basic(list_status="L")` + **最近 20 日**成交额建池。用它回测 2024-2026 时，期间退市/被 ST/失去流动性的股票不在池内 → 历史收益被系统性高估（strat200 训练段 244%~460% 与此有关）。同时"稳健点 36/36 全正"是因全体为正的牛市段+偏差所致，稳健性指标失去鉴别力。

**改法（三选一，按可行性）**：

1. **历史快照建池（推荐，工作量中等）**：池子按回测起点重建——用 `stock_basic` 的 `list_date` 过滤出回测起点前已上市的股票；再用回测起点前后 20 日的 `daily(trade_date=...)` 全市场快照算成交额做流动性过滤（排除当时就无流动性的票）；无法可靠获取历史退市名单时，用"当前仍上市"近似并**在报告中声明**。
2. **保守近似（工作量最小）**：保持现池，但 `grid_v2_*_robustness.md` 顶部增加"幸存者偏差声明"，并把展示收益按经验折扣（如 ×0.6）附注"偏差调整后参考值"。
3. **新数据源（长期）**：接入 Tushare 指数成分历史（`index_weight`）或退市股表（如有权限），彻底消除偏差。

**验收**：
- 报告/文档中出现"池子构建日期 + 幸存者偏差声明"；
- 决策日志记录本项处理方式；
- 后续所有基于该池的回测数字旁标注口径。

---

## 修复 C：看板误导性数字修复 + 回测结果持久化 + 权益曲线

**现状（三处）**：

1. `web/server.py` 第 168 行：`"total_asset": state.get("init_capital", 100000)`——看板"总资产"永远显示初始本金 10 万，与实际权益无关；
2. 仪表盘三张"最优回测收益/卡玛/胜率"卡片来自 `outputs/grid_search_results.csv`（**第一轮旧 50 池、无市场过滤的 in-sample 最优格**），与当前实盘配置（strat200 + 市场过滤）不是一回事；
3. 回测结果从未持久化 → ECharts 已引入（CDN）但**没有任何图**（`echarts.init` 不存在），回测页只有数字卡片。

**改法**：

1. `PaperBroker._save_state()` 的 state 中追加 `"asset": {"cash":..., "total_asset":..., "market_value":...}`（调 `query_asset()`），看板 `account.total_asset` 改读该字段；
2. `run_full_backtest.py`（及 grid 脚本）把最终结果落盘：`outputs/backtest_result.json`——`{equity_curve: [{date, equity}], metrics: {...}, config_hash, 市场过滤模式, 池信息}`；回测页 `/api/equity` 读它；
3. 看板"绩效"区改展示：**当前实盘配置**（strat200 + market_filter 实际模式）在验证段/holdout 段的表现；网格"最优格"一律加前缀"训练段参考"并弱化展示（次级位置）；
4. 前端 `loadBacktest()` 用 `/api/equity` 画权益曲线（此时启用 ECharts），并叠加最大回撤区间阴影；
5. 每张网格卡片的 `robust_points` 改为后端从 JSON（见修复 H.2）读取，不再用正则抓 MD。

**验收**：
- 看板"总资产"等于 cash+持仓市值（非 10 万）；
- 回测页出现真实权益曲线图；
- 网格最优格明确标注"训练段参考"，不在仪表盘头条位置出现。

---

## 修复 D：月度复盘 cron 月末判断（防重复触发）

**现状**：`monitor/scheduler.py` 第 481-487 行用 `day="28-31"` 近似月末，28/29/30/31 日每天 16:00 都会触发——一个月生成多份月度报告，2 月还会缺最后一天。

**改法**：任务函数内加判断（两种任选其一）：

```python
def monthly_review_job():
    # 仅当"今天是本月最后一个交易日"时执行
    latest = scanner.store.get_latest_trade_date()
    if latest is None:
        return
    if latest.month != date.today().month or (latest + timedelta(days=1)).month == latest.month and latest.day >= 28:
        # 今天不是本月最后交易日（下月还有本月数据），跳过
        ...
```

或更简单：生成前检查 `outputs/reports/monthly_YYYYMM.md` 已存在即跳过（幂等）。

**验收**：连跑 3 天（28/29/30 日）只生成一份月度报告；月末最后交易日生成的是最终版。

---

## 修复 E：回测侧板块 40% 铁律接线确认与补齐

**现状**：巡检侧 `sector_map` 已接入 `CheckContext(sector=...)` ✓；但 `run_full_backtest.py` / `scripts/grid_search_v2.py` 是否把 `sector_by_code` 传入 `engine.run()` **未确认**——若没传，回测里 `_check_sector` 对未知板块直接放行，训练口径与实盘不一致。

**改法**：

1. 在 `run_full_backtest.py` 与 `grid_search_v2.py` 中，从 `stock_basic` 构建 `{ts_code: industry}`（industry 空/NaN → "未分类"，不因未知放行），传入 `engine.run(sector_by_code=...)`；
2. 验证 Checklist `_check_sector` 在 sector 为"未分类"时是否按同板块累计（当前逻辑：sector 非空即参与累计，符合预期，确认即可）。

**验收**：构造两只同 industry 持仓 + 第三只同板块信号的回测场景 → 第三笔被 Checklist 拒绝；grid 重跑结果与板块未接线时可比对（预期收益略降或交易略少）。

---

## 修复 F：PaperBroker 连亏判定扣除费用

**现状**：`paper_trade.py` `_on_sell_halt_check` 用 `卖出价 < 最近买入价` 判亏，未扣双边费用（约 0.04%），边界微亏会被漏判，导致"连亏 3 笔停手"偶发漏触发。

**改法**：判定改为真实盈亏：

```python
pnl = (sell_price - last_buy_price) * vol - (commission + stamp_tax)   # 需在 trades 里带出费用
if pnl < 0:
    consec += 1
```

（卖出记录已存 commission/stamp_tax；买入记录存 commission；按轮次取对应费用。）

**验收**：构造"卖出价=买入价×0.9995"的边界场景 → 判定为亏损并计数。

---

## 修复 G：市场过滤结论保守化（holdout 样本小 + 变体脆弱）

**现状**：`outputs/market_filter_holdout.csv` 显示 ON-3%-20d（16 笔）+7.2% vs OFF（78 笔）-7.5%——方向有效，但 ON 变体样本仅 13~16 笔；阈值 2% 近乎无效（+0.5%）、窗口 60 日直接为负（-8.1%）。规则脆弱。

**改法**：

1. `config/settings.yaml` 将 `market_filter_mode` 默认设为 `reduce`（半仓参与）而非 `block`，注释写明 holdout 证据等级与样本量；
2. `docs/决策日志.md` 补一行："市场过滤 ON-3%-20d 证据等级：中等偏弱（16 笔样本，变体脆弱），采用 reduce 起步；积累 ≥100 笔纸面交易后再评估是否升级 block"；
3. 后续每月在月度复盘里追加"过滤命中统计"（本月被过滤拦下/减半的信号数及其假设收益），用实盘前的纸面数据持续验证。

**验收**：settings 默认 reduce；决策日志有证据等级记录；月度复盘模板含过滤命中统计栏目。

---

## 修复 H：前端小项

1. **XSS 防护**：`web/static/index.html` 的 `mdToHtml` 直接把报告内容塞 `innerHTML` 且无转义（报告含 Tushare 新闻标题等外部数据）。改法：渲染前对原始 md 先转义 `<`、`>`、`&`（或引入 marked + DOMPurify，均本地引入不打 CDN 也行）；
2. **robust_points 结构化**：`_count_robust_points` 用正则从 MD 抓第一个 `N/N`，脆弱。改法：`analyze_grid.py` 输出稳健点数到 `grid_*_summary.json`，后端读 JSON；
3. **自动刷新**：看板仅在切换页面时加载。改法：仪表盘页 `setInterval(loadDashboard, 60000)`（60 秒轮询），页面隐藏时暂停；
4. **ECharts 依赖**：当前引 CDN 但未使用。改法：修复 C.3 的权益曲线落地后正式启用；若暂不画图则移除引用（离线环境不依赖 CDN）。

**验收**：注入 `<script>` 的恶意报告内容渲染后仅显示文本；断网打开看板无 CDN 报错；仪表盘 60 秒自动刷新。

---

## 修复 I：边界最优声明与文档补全

**现状**：grid v2 最优仍在边界（ma5=0.1 为网格最大值且收益最高），稳健性报告已声明"不据此选参"（做得好），但 USER_GUIDE/ARCHITECTURE 未提"边界最优=真实最优可能在网格外"的说明。

**改法**：`docs/USER_GUIDE.md` 增加一节"参数选择纪律"：① 最优在边界 → 不选，扩网格再观察；② 稳健区域 > 单点最优；③ 每段数据一次验收权（引 `docs/决策日志.md`）。

**验收**：USER_GUIDE 含上述三条款。

---

## 实施顺序建议

**A（连亏停手+重跑）→ C（看板+持久化）→ D（cron）→ E（板块回测接线）→ F（费用判定）→ B（池子偏差处理）→ G（过滤保守化）→ H（前端小项）→ I（文档）**

A 完成并重跑 grid 后，C 的展示数字才有正确口径；B/G 是长期统计纪律，不阻塞模拟盘上线。

## 回归基线（不可破坏，累计三轮）

- 九转序列 / MACD 背离 / 蜡烛图预警 / Checklist 长电案例 四个黄金用例；
- T+1、不向下补仓、-8% 强制止损、移动止盈（高水位-3%）、MA10 收盘判定、信号 1 日有效；
- 状态持久化原子写、待执行止损"次日开盘执行"口径；
- 决策日志纪律与"已消费段不选参"声明。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

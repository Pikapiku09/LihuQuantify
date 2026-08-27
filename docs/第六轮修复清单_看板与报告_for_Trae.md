# 第六轮修复清单：看板与报告（for Trae）

> 项目：LihuQuantify
> 背景：系统已进入 NAS 稳定运行阶段，用户反馈四个展示层/纸面止盈缺口。均为"展示层 + 纸面止盈"问题，**不涉及策略与风控逻辑**。
> 硬约束：策略/风控逻辑冻结；三口径（回测/模拟盘/实盘）不变；铁律数值不动；100 笔纸面样本期间不改参数。

---

## 修复 1：`.md` 巡检报告升级为 rich 版（当前太薄）

**现状**：`monitor/report.py` 的 `daily_report()` 仍是老薄版（账户概览/持仓/信号/告警/铁律自检），round-5 只升级了 HTML 邮件和 `last_scan.json`，`.md` 报告没同步。rich 数据已在 `_build_daily_summary`（scheduler.py 第 60-175 行）产出。

**改法**：
1. 在 `scheduler._scan_impl` 中，把 `_build_daily_summary(...)` 的调用**提前到报告生成之前**（当前在第 539 行，报告生成在第 510 行——调整顺序，或把 rich summary 传入 `ReportGenerator.daily_report`）；
2. `report.py` 的 `daily_report()` 增加渲染模块（与 `daily_report.py` 的字段同源）：
   - 一、账户总览（总资产/现金/持仓市值/今日盈亏/累计盈亏及收益率）
   - 二、当前持仓（表：代码/名称/持股/成本/现价/市值/浮动盈亏/盈亏%/占比/止损线）
   - 三、今日操作（买入成交 / 卖出记录含实现盈亏与原因 / 被拒信号）
   - 四、盈亏分析（今日已实现 / 浮动 / 累计 / 持仓盈亏分布）
   - 五、市场与风险（市场状态、过滤模式、待执行止损、停手票、告警）
3. 生成后写 `outputs/reports/{date}.md`（路径不变，看板报告页自动可见）。

**验收**：打开看板"巡检报告"页，报告含持仓明细（名称/现价/浮盈亏）与今日买卖盈亏，不再是只有风控拦截记录。

---

## 修复 2：看板持仓表补"名称/现价/市值/浮盈亏/盈亏%"

**现状**：`web/server.py` `/api/dashboard`（第 141-150 行）只读 `paper_state.json` 的持仓（volume/cost）+ `stop_registry`（stop_price），缺名称/现价/市值/盈亏。而 rich 数据已在 `data/last_scan.json` 的 `summary.positions`（每项含 `name/price/market_value/float_pnl/float_pnl_pct/weight/stop_price`）。

**改法**：
1. `web/server.py` 增加读取 `data/last_scan.json`，取 `summary.positions`；
2. `/api/dashboard` 的 positions 列表合并：以 paper_state 持仓的 volume 为准，last_scan 提供 `name/price/market_value/float_pnl/float_pnl_pct/weight/stop_price`（找不到 last_scan 时回退现有字段）；
3. `web/static/index.html` 持仓表列改为：**代码/名称/持股/成本/现价/市值/浮动盈亏/盈亏%/止损线/状态**，浮盈亏/盈亏% 红涨绿跌着色。

**验收**：看板持仓表每行显示股票名称、现价、市值、浮动盈亏与盈亏%。

---

## 修复 3：首页改为真实监控指标（累计收益率/今日盈亏/浮动盈亏）

**现状**：首页指标卡当前是"总资产/持仓数/**最优回测收益/卡玛/胜率**/停手票"，后三个是回测 in-sample 预测值，非真实交易。真实数据可算：`paper_state.asset.total_asset` vs `init_capital` → 累计收益率；`last_scan.summary.prev_total_asset` → 今日盈亏；`summary.positions.float_pnl` 之和 → 浮动盈亏。

**改法**：
1. `web/server.py` `/api/dashboard` 返回新增 `live` 块：
   - `cumulative_return` = (total_asset − init_capital) / init_capital
   - `day_pnl` = total_asset − prev_total_asset（prev 为空则不展示）
   - `floating_pnl` = Σ positions.float_pnl
   - `realized_today` = Σ sells_today.pnl（last_scan 提供）
2. `index.html` 首页指标卡替换为：**总资产 / 累计收益率（红绿）/ 今日盈亏（红绿）/ 浮动盈亏（红绿）/ 持仓数 / 停手票**；
3. 回测"最优收益/卡玛/胜率/回撤/盈亏比"全部移到"回测分析/网格搜索"页，并保留"训练段参考·含幸存者偏差"标签（后端 `grid_training_reference`/`backtest` 已就绪，前端对接即可）。

**验收**：首页显示真实累计收益率、今日盈亏、浮动盈亏，不再有回测预测值占头条。

---

## 修复 4：纸面路径补"移动止盈" + 交易记录存 pnl/reason（解决"几分钱差价"）

**现状（三处缺口）**：
1. 纸面唯一卖出路径是"收盘破 MA10 → 次日开盘卖"，入场价离 MA10 仅 1~2%，震荡市里"刚金叉→回踩 MA10 离场"，差价几分钱，扣费后常为负；
2. **纸面路径缺 trailing 移动止盈**——回测有（高水位回撤 3% 离场），但 `PaperBroker` 无 high_water_mark，`scheduler._check_stops_with_alert` 无 trailing 判定；
3. `PaperBroker.buy/sell` 的交易记录**没存 `pnl` 和 `reason`**，看板"近十笔交易"的盈亏列/原因列是空的（前端已有这两列，只等数据）。

**改法**：

1. `paper_trade.py`：
   - `PaperBroker` 增加 `self.high_water_mark: dict[str, float]`（每只持仓），并持久化进 `paper_state.json`（`_save_state`/`_load_state` 加字段）；
   - `sell(ts_code, price, volume, reason="")`：记录 `"reason": reason`，并计算 `"pnl": (price - pos.cost) * volume - commission - stamp_tax`（在减仓前用加权成本 `pos.cost` 算）；
   - 新增 `update_high_water(code, price)`：`high_water_mark[code] = max(旧值, price)`。
2. `scheduler._check_stops_with_alert`：
   - 第二步循环里，对每个持仓先 `broker.update_high_water(code, close)`；
   - 增加 trailing 判定：`high_water > cost 且 close <= high_water * 0.97` → 登记待执行止损，`reason="trailing_stop"`（与回测 `stop_loss.py` 口径一致）；
   - 执行待执行止损时把 reason 传给 `broker.sell(..., reason=...)`。
3. `_STOP_REASON_LABEL` 增加 `"trailing_stop": "移动止盈"`（scheduler.py 第 57 行）。
4. （可选）新增 `scripts/dump_trades.py`：读 `data/paper_state.json` 的 trades，逐笔导出代码/方向/日期/价格/股数/盈亏/原因/持有天数，便于人工核对。

**验收**：
- 持仓浮盈 ≥3% 后回撤至 3% 内 → 次日开盘离场（而非等 MA10 破位）；
- 看板"近十笔交易"的盈亏列、原因列有值；
- 回测/纸面两者"移动止盈"行为对齐。

---

## 实施顺序建议

**4（止盈+交易记录，影响真实收益口径，最优先）→ 2（持仓明细）→ 3（首页指标）→ 1（报告升级）**

## 回归基线（不可破坏）

- 策略/风控逻辑冻结，三口径不变；
- 幂等、心跳、日志、备份、单封 HTML 日报机制保持；
- 100 笔纸面样本期间不改参数（决策日志纪律）。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

# 回测问题修复清单（for Trae）

> 项目：LihuQuantify（A 股日线级量化系统，事件驱动回测）
> 背景：系统已端到端跑通（真实数据 → CherryClaw → Checklist 风控 → 事件驱动回测 → 绩效）。
> 当前问题：近半年回测收益低、胜率差、信号少。
> 诊断结论：根因**不是**参数偏严，而是 ① 止盈执行缺失；② MA10 止损触发过紧；③ Checklist 追高闸门与策略矛盾；④ 股票池与样本量错配；⑤ 缺少参数网格与市场分段评估。另有两个实现问题（配置未接线、统计口径装饰性）。
> **硬约束（不得修改）**：铁律数值——成本 -8% 强制止损、单票 ≤25%、同板块 ≤40%、同票月内 ≤3 次、连亏 3 笔停手 30 天、T+1、绝不向下补仓、买入单+止损条件单必须同时挂（实盘 OMS）。禁止引入未来函数（信号用当日收盘，成交用次日开盘）。保持事件驱动引擎架构。所有输出保留免责声明。

---

## 修复 1（最高优先）：把移动止盈真正接入引擎

**现状（缺陷）**：
- `src/lihu_quantify/backtest/portfolio.py` 维护了 `high_water_mark`（第 27、85 行），但**全仓库没有任何代码读取它触发离场**；
- `src/lihu_quantify/risk/stop_loss.py` 第 100-103 行注释自称"trailing_stop 由 portfolio 层触发"，但 portfolio 没有该逻辑；
- `cherry_claw.py` 计算的 L1-L4 目标价只被 Checklist 检查"存在"，从不执行。

**后果**：盈利单没有任何止盈路径，只能等跌回 MA10 或 -5%/-8% 止损，赢家利润全部回吐 → 盈亏比被压扁、总收益低。

**改法**：

1. `stop_loss.py` 的 `evaluate()` 增加参数并追加移动止盈判定（放在三档止损**之后**、`hold` 之前）：

```python
def evaluate(self, position, bar, ma_vals, high_water_mark: float = 0.0) -> StopAction:
    ...
    # 5. 移动止盈：出现浮盈（高水位 > 成本）后，收盘价回撤 3% 离场
    if high_water_mark > position.cost:
        trail_price = high_water_mark * (1 + self.trailing_pullback)  # 高水位×0.97
        if close <= trail_price:
            return StopAction(
                kind="trailing_stop",
                reason=f"移动止盈触发：高水位{high_water_mark:.2f} 回撤3% → 离场价{trail_price:.2f}",
                suggested_price=trail_price,
            )
    return StopAction(kind="hold")
```

2. `engine.py` 第 123-142 行的止损检查循环：

```python
action = self.stop_loss_mgr.evaluate(
    pos, bar, ma_vals,
    high_water_mark=portfolio.high_water_mark.get(code, 0.0),
)
if action.kind in ("force_stop", "ma_break", "execute", "trailing_stop"):
```

3. （可选加分项）分批止盈：trailing 触发时若浮盈 ≥ L1（+5%），只卖 1/3 仓位，剩余按 L2/高水位继续跟踪。第一版可先做"全仓 trailing"，回测对比后再上分批。

**验收**：
- 单元测试：构造"买入后涨 +5% 再回落 4%"的持仓场景 → 必须触发 `trailing_stop`；
- 回测报告里不再出现"浮盈 10%+ 最后以亏损离场"的成交记录。

---

## 修复 2：MA10 离场改为收盘判定（停止"隐形紧止损"）

**现状**：`stop_loss.py` 第 75 行 `if ma10 > 0 and low < ma10:` —— 盘中低点触碰 MA10 即离场。
入场条件（收盘贴近 MA5 ±1.5% + 刚金叉）使入场价离 MA10 通常只有 1~3%，盘中回踩（趋势内必然发生）就会触发洗盘离场，卖出还按次日开盘成交。实际止损远紧于宣称的 -5%/-8%。

**改法**：

```python
# 盘中触碰 → 收盘价跌破（日线铁律原文语义）
if ma10 > 0 and close < ma10:
    return StopAction(kind="ma_break", reason=f"收盘跌破 10 日线：MA10={ma10:.2f}，收盘{close:.2f}", ...)
```

- **-8% 强制止损仍保留盘中 low 判定**（铁律无条件，不受此修改影响）；
- 可选增强：连续 2 日收盘低于 MA10 才离场（需在持仓状态里加跨日计数），先跑"单日收盘版"与现状做 A/B 对比，再决定是否上 2 日确认。

**验收**：A/B 对比——修复前后回测的平均持仓天数、胜率、总收益记录对比，确认洗盘离场减少。

---

## 修复 3：回测股票池与周期重设计（解决样本量问题）

**现状**：`run_backtest.py` 第 26 行 `DEFAULT_STOCKS = ["600584.SH","600519.SH","601318.SH","600036.SH"]`，`fetch_data(days=180)`。
4 只大票 × 半年 ≈ 个位数信号、两三笔交易——**这个样本量下胜率数字是纯噪声**，无法对策略下任何结论。

**改法**：

1. `DEFAULT_STOCKS` 保留为冒烟测试（快速验证管线是否正常）；
2. 新增**全市场回测模式**（新脚本 `run_full_backtest.py` 或参数开关 `--universe full`）：
   - 用 Tushare `stock_basic` 拉主板池：排除 688/300/301/ST/上市<60 日/近 20 日日均成交额 <1 亿（与 `settings.yaml` universe 一致）；
   - 回测周期 **≥3 年**（如 2023-01-01 至最新交易日），覆盖牛/熊/震荡；
   - 数据先落 DuckDB（`data_manager.ensure_daily`）+ 复用缓存，拉数分批限速（遵守 Tushare 积分频率限制）；
   - 若全市场拉数过慢，第一版可先用中证 1000 成分或分层抽样 300~500 只。
3. **目标验收线：交易笔数 ≥200 笔**。达不到就扩大股票池或延长周期，不要在几十笔样本上谈胜率。

---

## 修复 4：参数网格回测（替代"手工放松参数"）

**现状**：`close_to_ma5_max_dev=0.015` 等参数是硬编码默认值；"信号少就放松参数"是单点拍脑袋，容易过拟合。

**改法**：新增 `scripts/grid_search.py`：

- 网格：
  - `close_to_ma5_max_dev` ∈ {0.005, 0.015, 0.03, 0.05}
  - `golden_cross_max_freshness_days` ∈ {3, 5, 7, 10}
  - `chasing_high_threshold` ∈ {0.05, 0.08, 0.12}（需先把 `ChecklistGate.CHASING_HIGH_THRESHOLD` 改为可配置参数）
- 每组输出：总收益、最大回撤、卡玛、胜率、盈亏比、交易笔数；
- 输出 CSV + **热力图**（主看"收益/回撤比"，其次胜率），并标出"稳健区域"（相邻参数组合都为正的区域）；
- **防过拟合**：前 3 年做训练段跑网格，最后 1 年做验证段只验证最优区域；或做 Walk-Forward。

**验收**：热力图产出；所选参数在验证段收益为正；报告说明"为什么选这组参数"。

---

## 修复 5：配置接线 + 两个统计口径修正

### 5a. settings.yaml 参数真实生效

**现状**：`run_backtest.py` 第 128 行 `EventDrivenEngine(strategy=CherryClaw())` 全用硬编码默认值；yaml 里 strategy/risk/backtest 段是"死配置"（只读了 token 和缓存路径）。

**改法**：

```python
s = settings.strategy
r = settings.risk
b = settings.backtest
strategy = CherryClaw(
    ma_periods=tuple(s.ma_periods),
    golden_cross_max_freshness=s.golden_cross_max_freshness_days,
    volume_ratio_threshold=s.volume_ratio_threshold,
    entity_ratio_threshold=s.entity_ratio_threshold,
    close_to_ma5_max_dev=s.close_to_ma5_max_dev,
    max_position_pct=r.max_single_position,
    stop_loss_force_pct=r.stop_loss_force,
)
broker = SimulatedBroker(commission_rate=b.commission, stamp_tax_rate=b.stamp_tax, slippage=b.slippage)
engine = EventDrivenEngine(strategy=strategy, broker=broker, max_single=r.max_single_position)
```

同时统一印花税口径：当前 A 股卖出印花税为 **0.05%**（万5），`settings.yaml` 第 58 行注释（千1）与取值（0.0001）及 broker 默认值（0.001）三处不一致，统一为 0.0005 并修正注释。

**验收**：改 yaml 里某个参数（如 `close_to_ma5_max_dev` 0.015→0.05）后重跑回测，结果必须发生变化。

### 5b. 信号 1 日有效（修"旧信号复活"）

**现状**：`strategy/base.py` `on_bar` 第 59-62 行，当日无信号时返回 `signals[-1]`（历史上最近一次通过的信号）——被拒信号会在之后每天被重复提交。

**改法**：当日 bar 无信号必须返回 None：

```python
def on_bar(self, ctx):
    signals = self._evaluate(ctx.history, ctx.indicators)
    last_date = ctx.bar.get("trade_date")
    for s in reversed(signals):
        if s.trade_date == last_date:
            return s
    return None   # 删除 signals[-1] 兜底
```

### 5c. 胜率/止损执行率口径修正

**现状**：`metrics.py` 第 58 行起按 sell 单计胜率；`run_backtest.py` 第 101-104 行"止损执行率"把所有亏损单都算止损，永远 100%（装饰性）。

**改法**：
- 胜率按"买入-卖出**轮次**"配对（FIFO 按 ts_code），每轮一个 pnl；
- 止损执行率 = 触发 `force_stop/ma_break/execute/trailing_stop` 的卖出单数 ÷ 应止损单数（信号里带止损价的持仓），如实统计；
- 绩效里新增"每笔平均费用占比"（费用/成交额），回答"摩擦成本吃掉了多少收益"。

---

## 修复 6：市场状态分段统计

**现状**：单一窗口的总收益/胜率混在一起，无法区分"策略不行"还是"环境不行"。

**改法**：在回测报告末尾新增分段统计——用 000001.SH 的 20 日涨跌幅把交易日分三段：
- 上涨段（20 日涨幅 ≥ +3%）、震荡段（-3% ~ +3%）、下跌段（≤ -3%）；
- 各段输出：交易笔数、胜率、平均单笔收益、合计盈亏。

**验收**：报告末尾出现三状态统计表；据此确认策略在哪种环境有优势，实盘只在有优势的环境开机。

---

## 建议实施顺序（每步跑一次回归测试再进下一步）

1. 修复 1（移动止盈）→ 2. 修复 2（MA10 收盘判定）→ 3. 修复 5（接线+口径）→ 4. 修复 3（股票池）→ 5. 修复 4（网格）→ 6. 修复 6（分段统计）

## 回归测试基线（不可破坏）

- 九转序列：600584.SH 2026-08-06 ~ 08-18 卖出序列 1→9，8/18 第 9 根衰竭（收 85.42）；
- MACD 背离：36 日窗口无标准背离（DIF 全程零轴下）；
- 蜡烛图预警：600584 8/19 "天量长阴+光脚大阴线+减仓预警"；
- Checklist：长电案例"板块合计 57.1% > 40% → 拒绝"；
- T+1 与"不向下补仓"状态机行为不变。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

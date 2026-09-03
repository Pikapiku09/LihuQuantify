# 功能实现清单：100 笔评审进度可视化（看板 + 日报 + 邮件）

> 背景：live 纸面验证进入积累期（2026-08-26 启动，截至 2026-09-01 巡检：11 笔买入、5 笔已平仓、当前持仓 6 只、总资产 99,645）。USER_GUIDE §5.0 定义了评审里程碑（≥50 轮预检、≥100 轮正式评审、1-3 个月上实盘），但目前**没有任何地方能一眼看到当前进度**——看板首页指标卡只有资产/盈亏类指标，日报与邮件也没有轮次统计。本清单新增"评审进度"可视化，**纯展示层，零策略/风控/参数变更，零新增数据源**（全部数据已在 paper_state.json / last_scan.json）。

## ⚠️ 执行纪律（必读）

1. **不改**：策略参数、风控铁律、Checklist、settings.yaml 的 strategy/risk 段、回测引擎。本功能只读 trades 做统计 + 展示。
2. **口径单一来源**：轮次统计必须复用 `metrics._pair_rounds` 的 FIFO 配对逻辑（月度复盘 scheduler.py L1314-1331 已用同款口径）——保证"评审进度"与月度复盘、回测基准（58.9%/1.25）三方数字可比。不得自创第二套配对算法。
3. **100 笔计数口径**：按"已平仓轮次"计（一买一卖配对为 1 轮，`_pair_rounds` 返回的 rounds 数量），**不是**买入笔数、也不是 sell 条数。理由：评审要对比胜率/盈亏比，只有配对完成的轮次才有这两个指标（USER_GUIDE §5.0 "≥50 笔轮次初步胜率/盈亏比 vs 回测"）。
4. 完成后全量测试通过；**不得删改既有测试断言**凑通过。

---

## 第一步：新增统一统计模块（核心，先做这个）

**新建文件**：`src/lihu_quantify/monitor/review_progress.py`

职责：把 scheduler 月度复盘里那段配对统计（L1314-1331）抽成可复用纯函数，供 3 处消费（dashboard / 巡检日报 / 邮件日报）+ 1 处收敛（月度复盘可选）。

```python
"""评审进度统计（100 笔 live 验收用，纯统计无 IO）。

口径：与 scheduler._monthly_review / backtest.metrics._pair_rounds 一致
（买入-卖出 FIFO 配对为一轮）。本模块是唯一统计入口，禁止另写配对算法。
"""
from __future__ import annotations

import datetime as _dt
from typing import Callable, Optional

from ..backtest.metrics import _pair_rounds
from ..types import TradeRecord

REVIEW_TARGET = 100          # 正式评审目标轮次（USER_GUIDE §5.0）
CHECKPOINT_50 = 50           # 预检点：胜率 <45% 需停下检查

def _as_date(v) -> Optional[_dt.date]:
    """date / datetime / ISO str 统一转 date（str 截断到 10 位）。"""
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        return _dt.date.fromisoformat(v[:10])
    return None

def review_stats(trades: list[dict]) -> dict:
    """从成交流水算评审指标。trades 元素须含 ts_code/trade_date 或 date/side/
    price/volume/commission/stamp_tax；date 字段 date 或 ISO str 均可。"""
    recs = []
    for t in trades or []:
        d = _as_date(t.get("trade_date") or t.get("date"))
        recs.append(TradeRecord(
            ts_code=t["ts_code"],
            trade_date=d or _dt.date.today(),
            side=t["side"], price=t["price"], volume=t["volume"],
            commission=t.get("commission", 0),
            stamp_tax=t.get("stamp_tax", 0),
        ))
    rounds = _pair_rounds(recs)                    # list[float]，每轮净盈亏
    wins = [r for r in rounds if r > 0]
    losses = [r for r in rounds if r < 0]
    closed = len(rounds)
    win_rate = len(wins) / closed if closed else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = abs(sum(losses) / len(losses)) if losses else None
    pl_ratio = avg_win / avg_loss if (avg_win is not None and avg_loss) else None
    return {
        "closed_rounds": closed,
        "target": REVIEW_TARGET,
        "remaining": max(0, REVIEW_TARGET - closed),
        "win_rate": win_rate,        # None=尚无平仓轮
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pl_ratio": pl_ratio,
        "realized_pnl": sum(rounds), # 已实现净盈亏（已含双边费用）
        "stage": _stage(closed),
    }

def _stage(closed: int) -> str:
    """评审阶段：<50 积累中 / ≥50 达预检点 / ≥100 达评审点。"""
    if closed >= REVIEW_TARGET:
        return "ready_review"          # 可进行 100 笔正式评审
    if closed >= CHECKPOINT_50:
        return "ready_checkpoint50"    # 达 50 轮预检点
    return "accumulating"

def fmt_stage(stage: str) -> str:
    return {
        "accumulating": "积累中",
        "ready_checkpoint50": "≥50 轮 · 可预检",
        "ready_review": "≥100 轮 · 可评审",
    }.get(stage, stage)
```

**验收**：`python -c "from lihu_quantify.monitor.review_progress import review_stats; print(review_stats([]))"` 输出 closed_rounds=0、win_rate=None 不抛错；用现有 paper_state.json 的 trades 跑，closed_rounds 应等于 5（当前 5 笔卖出配对）。

---

## 第二步：巡检时预计算 review，随 last_scan.json 持久化

**文件**：`src/lihu_quantify/monitor/scheduler.py` → `_build_daily_summary()`（L137 起，返回 summary dict）

在函数体内（返回前）追加——让 summary 带上 review 段，日报/邮件/md 报告都能从 summary 读，web 也可读 last_scan：

```python
    # 评审进度（100 笔验收；复用月度复盘同款 FIFO 配对口径）
    from .review_progress import review_stats
    _trades = getattr(broker, "trades", []) or []
    summary_review = review_stats(_trades)
```

然后在函数构造的返回 dict 中加键：`"review": summary_review`（与现有 total_asset/positions/sells_today 同级，随 summary 落盘 last_scan.json）。

注意：定位该函数实际 return 语句（约在 L230-260 区间，构造返回 dict 处），把 review 键加进去。若 _build_daily_summary 内部有局部变量名冲突，改局部名为 `review_stats` 调用结果即可。

**可选收敛（低风险，推荐做）**：月度复盘函数 `_monthly_review` 中 L1314-1331 的配对统计块，替换为 `from .review_progress import review_stats` + 取值，消除两套重复（输出格式保持原样，勿动 L1333+ 的报告行）。如担心动已测代码，可暂留双份并在旧块注释 `# TODO: 收敛到 review_progress.review_stats`。

**验收**：跑一次 `python run_scheduler.py --run-now --mode paper --n 50`（或既有集成测试路径），断言 last_scan.json 的 summary 出现 `review.closed_rounds` 且 = 当前已平仓轮数。

---

## 第三步：看板 API 返回 review 块

**文件**：`web/server.py`

1. 顶部 import 区（L36-38 sys.path.insert(0, src) 已存在，同区块追加）：
```python
from lihu_quantify.monitor.review_progress import review_stats
```
2. `/api/dashboard`（L223-272）返回 dict 加键（在 L247 return 的 dict 里，放在 "live" 键附近）：
```python
        "review": _dashboard_review(state),
```
3. 新增辅助函数（放 `_live_metrics` 附近，纯函数便于单测）：
```python
def _dashboard_review(state: dict) -> dict:
    """dashboard 用：直接读 paper_state.trades 现算（比 last_scan 更新更实时）。
    字段结构见 review_progress.review_stats。"""
    try:
        from lihu_quantify.monitor.review_progress import review_stats
        return review_stats(state.get("trades") or [])
    except Exception:
        return {"closed_rounds": 0, "target": 100, "remaining": 100,
                "win_rate": None, "pl_ratio": None, "stage": "accumulating"}
```
（paper_state.json 的 trades 里 date 是 ISO str，review_stats 已兼容；异常兜底保证看板永不因统计炸掉——沿用现有 _market_state 的降级风格。）

**验收**：`curl http://127.0.0.1:8000/api/dashboard` 返回 JSON 含 `review.closed_rounds`；paper_state 为空时不报错且 closed_rounds=0。

---

## 第四步：前端看板新增"评审进度"卡片

**文件**：`web/static/index.html`

1. **CSS**：在 metric 样式区（L145-189 附近）追加进度条样式：
```css
.progress-wrap { height: 6px; background: var(--bg-alt,#eceff3); border-radius: 3px; margin-top: 6px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--accent,#4f8cff); border-radius: 3px; transition: width .4s; }
.progress-fill.warn { background: #e6a23c; }
.progress-fill.ok { background: var(--green,#16a34a); }
```
2. **渲染**：`renderDashboard(data)`（L696 起）的指标卡数组（L710-718 区域）**追加一张卡**：
```javascript
    // 评审进度卡（live 100 笔验收；统计口径=配对轮次，见 review_progress.py）
    const rv = data.review || {};
    const closed = rv.closed_rounds ?? 0, tgt = rv.target ?? 100;
    const pct = Math.min(100, Math.round(closed / tgt * 100));
    const fillCls = closed >= tgt ? 'ok' : (closed >= 50 ? 'warn' : '');
    cards.push({
        label: '评审进度',
        value: `${closed} / ${tgt} 轮`,
        sub: rv.win_rate != null
            ? `胜率 ${fmt.pct(rv.win_rate)} · 盈亏比 ${(rv.pl_ratio ?? 0).toFixed(2)} · ${stageText(rv.stage)}`
            : `尚无平仓轮 · 已实现 ${fmt.moneySign(rv.realized_pnl ?? 0)}`,
        bar: `<div class="progress-wrap"><div class="progress-fill ${fillCls}" style="width:${pct}%"></div></div>`,
    });
```
3. **卡片模板**（L721-726 的 map 渲染处）支持可选 bar：在 `<div class="metric-sub">${c.sub}</div>` 后追加 `${c.bar || ''}`。
4. 加小函数 `stageText(stage)`：accumulating→"积累中"、ready_checkpoint50→"可预检"、ready_review→"可评审"（就近定义在 renderDashboard 前）。
5. 确认 `fmt.pct` / `fmt.moneySign` 存在（看板已有红涨绿跌格式化，L711-715 正在用，直接用同名 API；若 pct 不存在则用 `(rv.win_rate*100).toFixed(1)+'%'`）。

**验收**：看板首页出现"评审进度 5 / 100 轮"卡 + 蓝色进度条 5%；无平仓轮时显示"尚无平仓轮"不报错；卡片不挤压现有 4 卡（metric-grid 自适应换行）。

---

## 第五步：巡检 .md 报告与邮件日报各加一行

### 5a. rich .md 报告
**文件**：`src/lihu_quantify/monitor/report.py` → `_daily_report_rich` 账户总览节（L120-138，`lines.append` 表格区）

在账户总览表格行后追加：
```python
    _review = (rich.get("review") or {})
    _cr = _review.get("closed_rounds") or 0
    _tgt = _review.get("target") or 100
    if _review:
        _wr = f"{_review['win_rate']:.1%}" if _review.get("win_rate") is not None else "-"
        _pr = f"{_review['pl_ratio']:.2f}" if _review.get("pl_ratio") is not None else "-"
        lines.append(f"| 评审进度 | {_cr}/{_tgt} 轮（胜率 {_wr}，盈亏比 {_pr}） |")
```
（rich dict 的 key 与 daily_report.py 同源；若此处变量命名风格不同，按上下文调整。）

### 5b. 邮件日报
**文件**：`src/lihu_quantify/monitor/daily_report.py` → `_render_overview(d)`（L127-151）

在 rows 列表（L139 附近）追加一行：
```python
    _rv = d.get("review") or {}
    if _rv:
        _cr = _rv.get("closed_rounds") or 0
        _tgt = _rv.get("target") or 100
        _wr = f"{_rv['win_rate']:.1%}" if _rv.get("win_rate") is not None else "-"
        _pr = f"{_rv['pl_ratio']:.2f}" if _rv.get("pl_ratio") is not None else "-"
        rows.append(("评审进度", f"{_cr}/{_tgt} 轮 · 胜率 {_wr} · 盈亏比 {_pr}"))
```
（rows 后续会经 _th/_td 渲染成表，参照 L139 前已有行的写法。）

**验收**：预览邮件 `python scripts/preview_daily_report.py` 与一次巡检生成的 .md 报告，账户总览区均出现"评审进度 X/100 轮"行。

---

## 第六步：测试

**新建文件**：`tests/test_review_progress.py`

覆盖（≥6 个用例）：
1. `test_empty_trades`：review_stats([]) → closed_rounds=0、win_rate=None、stage="accumulating"、不抛错；
2. `test_fifo_pairing_matches_monthly_review`：构造与月度复盘测试相同的 trades 序列（一买一卖全配对），断言 closed_rounds == 买卖对数、realized_pnl == 各轮 pnl 之和（与 `_pair_rounds` 直接跑结果一致）；
3. `test_unclosed_buys_not_counted`：只有 buy 无 sell → closed_rounds=0；两笔 buy + 一笔 sell → 配对 1 轮（FIFO 语义）；
4. `test_date_str_and_date_mixed`：同一条 trades 里 date 混用 ISO str 与 date 对象，结果一致（回归 review_stats 的 _as_date 归一）；
5. `test_stage_boundaries`：closed=49→accumulating、50→ready_checkpoint50、99→ready_checkpoint50、100→ready_review；
6. `test_dashboard_review_endpoint`：mock paper_state.json（含 2 买 1 卖）→ TestClient GET /api/dashboard → json["review"]["closed_rounds"]==1；空 paper_state → 0 不炸（参照既有 web 测试写法，如 test_round6_dashboard_report.py 的 web_server fixture）。

**验收**：`python -m pytest tests/test_review_progress.py -q` 全绿；全量 `python -m pytest -q` 通过（当前 180+ 用例）。

---

## 整轮交验标准

- [ ] `review_progress.py` 纯函数通过第六步全部用例；
- [ ] 看板首页指标卡出现"评审进度 X/100 轮"（进度条、胜率、盈亏比、阶段标签齐全），刷新后随交易数增长；
- [ ] 巡检 .md 报告账户总览 + 邮件日报账户总览各出现一行评审进度；
- [ ] 月度复盘函数与 review_progress 数字一致（同数据段下 closed_rounds/胜率/盈亏比完全相同）；
- [ ] 全量测试通过；未触碰任何策略参数/风控铁律/settings.yaml strategy-risk 段；
- [ ] 决策日志 Live 积累期表登记一行："看板/日报新增评审进度（纯展示层，计数口径=配对轮次，同月度复盘）"——不消耗数据段。

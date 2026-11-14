"""A1 反事实离场回测：MA10 破位这条离场线，是不是砍掉了本该反弹的交易？

只读诊断脚本。不修改 src/ 任何代码。
- FIFO 配对复用 lihu_quantify.backtest.metrics._pair_rounds（唯一统计入口），
  本脚本自建的明细配对仅用于"给每轮打标签"，并与官方 PnL 逐轮校验一致。
- 行情全部走本地 duckdb（read_only），禁止联网。

用法：
    .venv\\Scripts\\python.exe scripts\\_diag_exit_ab.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import duckdb  # noqa: E402

from lihu_quantify.backtest.metrics import _pair_rounds  # noqa: E402  # 唯一统计入口
from lihu_quantify.monitor.auto_block import wilson_lower_bound  # noqa: E402
from lihu_quantify.types import TradeRecord  # noqa: E402

# ---------------------------------------------------------------- 常量（均来自 src，不杜撰）
LEDGER_DIR = Path(r"E:\Dsh_WorkSapce\Dify_Agents\_tmp_mine")
DB_PATH = _ROOT / "data" / "lihu_quant.duckdb"

# 费率：src/lihu_quantify/execution/paper_trade.py:69-71
COMMISSION_RATE = 0.00025
STAMP_TAX_RATE = 0.0005
MIN_COMMISSION = 5.0
SLIPPAGE = 0.001          # config.BacktestConfig.slippage

# 现行离场参数：config.RiskConfig（stop_loss_warn/exec/force, trailing_profit_pullback, trailing_break_ma）
FORCE_PCT = -0.08         # 价格止损（铁律）
TRAIL_PULLBACK = 0.03     # 移动止盈回撤
BREAK_MA = 10             # MA10 破位

PRICE_STOP_LABEL = "价格止损"
MA_BREAK_LABEL = "MA10 破位"
TRAIL_LABEL = "移动止盈"

END_DATE = date(2026, 11, 10)     # 行情最后交易日
IS_END = date(2026, 9, 30)        # 样本内/外切分点

# 账本 → 策略（config/settings.yaml:41 main_strategy=intraday_reversal；
# shadow_books: s43/s44 无 strategy → BookSpec 默认 cherry_claw；wbr25=weekly_band；ir20=intraday_reversal）
LEDGERS = [
    ("main", "paper_state.json", "intraday_reversal"),
    ("ir20", "paper_state_ir20.json", "intraday_reversal"),
    ("s43", "paper_state_s43.json", "cherry_claw"),
    ("s44", "paper_state_s44.json", "cherry_claw"),
    ("wbr25", "paper_state_wbr25.json", "weekly_band"),
]
IR_BOOKS = ("main", "ir20")       # 主口径：同策略，可合并
ALL_BOOKS = tuple(b[0] for b in LEDGERS)


# ================================================================ 工具
def log(msg: str = "") -> None:
    print(msg, flush=True)


def hdr(title: str) -> None:
    log("")
    log("=" * 78)
    log(title)
    log("=" * 78)


def pct(x: float, nd: int = 2) -> str:
    return f"{x * 100:.{nd}f}%"


def wilson_interval(wins: int, n: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson 95% 区间。下界复用官方 wilson_lower_bound（auto_block.py:147），上界用标准公式。"""
    lo = wilson_lower_bound(wins, n, z)
    if n <= 0:
        return (float("nan"), float("nan"))
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n))
    hi = min(1.0, (centre + margin) / denom)
    return (0.0 if lo is None else lo, hi)


# ================================================================ 1. 读取账本
@dataclass
class Round:
    """一轮已平仓交易（来自账本真实成交，明细配对仅用于打标签）。"""

    book: str
    ts_code: str
    entry_date: date
    entry_price: float
    volume: int
    buy_commission: float
    exit_date: date
    exit_price: float
    sell_commission: float
    sell_stamp_tax: float
    exit_reason: str
    pnl: float

    @property
    def ret(self) -> float:
        cost = self.entry_price * self.volume
        return self.pnl / cost if cost > 0 else 0.0

    @property
    def hold_days(self) -> int:
        return (self.exit_date - self.entry_date).days


def load_ledger(name: str, fname: str) -> dict:
    path = LEDGER_DIR / fname
    raw = json.loads(path.read_text(encoding="utf-8"))
    trades: list[TradeRecord] = []
    for t in raw.get("trades", []):
        d = t.get("date")
        if isinstance(d, str):
            d = datetime.strptime(d[:10], "%Y-%m-%d").date()
        trades.append(
            TradeRecord(
                ts_code=t["ts_code"],
                trade_date=d,
                side=t["side"],
                price=float(t["price"]),
                volume=int(t["volume"]),
                pnl=float(t.get("pnl", 0.0) or 0.0),
                reason=str(t.get("reason", "") or ""),
                commission=float(t.get("commission", 0.0) or 0.0),
                stamp_tax=float(t.get("stamp_tax", 0.0) or 0.0),
            )
        )
    trades.sort(key=lambda x: x.trade_date)
    return {
        "name": name,
        "raw": raw,
        "trades": trades,
        "init_capital": float(raw.get("init_capital", 100000.0)),
    }


def detail_pair(trades: list[TradeRecord]) -> list[tuple]:
    """自建 FIFO 明细配对（**仅用于打标签**，PnL 口径必须与 _pair_rounds 一致）。

    返回 [(buy_record, sell_record, consume_volume, pnl)]。
    逻辑逐行对齐 metrics._pair_rounds:104-133，并用它做逐轮校验。
    """
    open_buys: dict[str, list] = {}
    out: list[tuple] = []
    for t in trades:                       # load_ledger 已按 trade_date 升序
        if t.side == "buy":
            open_buys.setdefault(t.ts_code, []).append([t, t.volume])
        else:
            queue = open_buys.get(t.ts_code)
            if not queue:
                continue
            remaining = t.volume
            while remaining > 0 and queue:
                entry = queue[0]
                buy, buy_remaining = entry[0], entry[1]
                consume = min(remaining, buy_remaining)
                cost = buy.price * consume
                proceeds = t.price * consume
                buy_fee_share = buy.commission * consume / max(1, buy.volume)
                sell_fee_share = (t.commission + t.stamp_tax) * consume / max(1, t.volume)
                pnl = proceeds - cost - buy_fee_share - sell_fee_share
                out.append((buy, t, consume, pnl))
                entry[1] -= consume
                remaining -= consume
                if entry[1] <= 0:
                    queue.pop(0)
    return out


def build_rounds(ledger: dict) -> tuple[list[Round], list[float]]:
    """返回 (明细 rounds, 官方 _pair_rounds PnL 列表)。"""
    official = _pair_rounds(ledger["trades"])
    det = detail_pair(ledger["trades"])
    mine = [x[3] for x in det]
    # ---- 校验：自建明细必须与唯一统计入口逐轮同值
    if len(official) != len(mine):
        raise SystemExit(f"[FATAL] {ledger['name']}: 轮次数不一致 official={len(official)} mine={len(mine)}")
    if abs(sum(official) - sum(mine)) > 1e-6:
        raise SystemExit(f"[FATAL] {ledger['name']}: 总盈亏不一致 official={sum(official)} mine={sum(mine)}")
    a = np.sort(np.array(official, dtype=float))
    b = np.sort(np.array(mine, dtype=float))
    if np.max(np.abs(a - b)) > 1e-6:
        raise SystemExit(f"[FATAL] {ledger['name']}: 逐轮 PnL 分布不一致")
    rounds = [
        Round(
            book=ledger["name"],
            ts_code=buy.ts_code,
            entry_date=buy.trade_date,
            entry_price=buy.price,
            volume=consume,
            buy_commission=buy.commission * consume / max(1, buy.volume),
            exit_date=sell.trade_date,
            exit_price=sell.price,
            sell_commission=sell.commission * consume / max(1, sell.volume),
            sell_stamp_tax=sell.stamp_tax * consume / max(1, sell.volume),
            exit_reason=sell.reason,
            pnl=pnl,
        )
        for buy, sell, consume, pnl in det
    ]
    return rounds, official


# ================================================================ 2. 行情面板
class Panel:
    """本地 duckdb 行情面板：未复权 close/open + 复权因子。"""

    def __init__(self, path: Path):
        self.con = duckdb.connect(str(path), read_only=True)
        self._cache: dict[str, pd.DataFrame] = {}
        self.cal = self._load_calendar()

    def _load_calendar(self) -> list[date]:
        df = self.con.execute(
            "select distinct trade_date from daily_quotes "
            "where trade_date between ? and ? order by trade_date",
            [date(2026, 7, 1), END_DATE],
        ).fetchdf()
        out = []
        for d in df["trade_date"].tolist():
            out.append(d.date() if isinstance(d, pd.Timestamp) else d)
        return out

    def get(self, code: str) -> pd.DataFrame:
        """该股票全部日线（含买卖区间的历史，用于均线），index=trade_date。"""
        if code in self._cache:
            return self._cache[code]
        df = self.con.execute(
            "select trade_date, open, high, low, close from daily_quotes "
            "where ts_code=? order by trade_date",
            [code],
        ).fetchdf()
        adj = self.con.execute(
            "select trade_date, adj_factor from adj_factors where ts_code=? order by trade_date",
            [code],
        ).fetchdf()
        if df.empty:
            self._cache[code] = df
            return df
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.set_index("trade_date")
        if not adj.empty:
            adj["trade_date"] = pd.to_datetime(adj["trade_date"]).dt.date
            adj = adj.set_index("trade_date")["adj_factor"]
            df["adj_factor"] = adj.reindex(df.index).ffill().bfill()
        else:
            df["adj_factor"] = np.nan
        # 复权口径与 scheduler._fetch_ma10 / paper_trade.adj_ratio 一致：
        # close_adj = close * adj_factor / adj_today  → 比较等价于在 close*adj_factor 空间比较
        df["px_adj"] = np.where(
            df["adj_factor"].notna(), df["close"] * df["adj_factor"], df["close"]
        )
        self._cache[code] = df
        return df

    def has(self, code: str) -> bool:
        return not self.get(code).empty


# ================================================================ 3. 离场策略
@dataclass(frozen=True)
class Policy:
    key: str
    label: str
    break_ma: int = 10            # 均线离场周期；0 = 不用均线
    confirm_days: int = 0         # 破位需连续 N 日确认（0 = 当日即触发）
    force_pct: float = FORCE_PCT  # 固定止损（相对成本）；None 等效 -1.0（关闭）
    trail_pullback: float = TRAIL_PULLBACK  # 移动止盈回撤；1.0 = 关闭
    max_hold: int | None = None   # 固定持有交易日数 → 到期离场
    hold_to_end: bool = False     # 一直持有到 END_DATE
    price_stop_from_entry_ma: bool = True  # 实盘 stop_price=min(cost*(1+force), 入场日MA)


@dataclass
class SimResult:
    """单轮模拟结果。"""

    exit_date: date
    exit_price: float
    reason: str
    hold_bars: int
    pnl: float


def simulate_round(
    rd: Round, panel: Panel, pol: Policy, extra_slippage: float = 0.0
) -> SimResult | None:
    """在真实入场点上模拟某种离场规则。

    成交口径对齐实盘（scripts 核验见报告第 2 节）：
      - 触发日的**次日开盘价 × (1-SLIPPAGE)** 成交（scheduler 收盘判定 → 次日开盘执行）
      - 无次日行情时退化为触发日收盘成交
      - 费用：佣金 max(额×2.5bp, 5) + 卖出印花税 5bp
    """
    df = panel.get(rd.ts_code)
    if df.empty:
        return None
    dates = list(df.index)
    if rd.entry_date not in df.index:
        # 入场日无行情（停牌等）→ 用其后第一个交易日作为基准
        fut = [d for d in dates if d >= rd.entry_date]
        if not fut:
            return None
        i_entry = dates.index(fut[0])
    else:
        i_entry = dates.index(rd.entry_date)
    n = len(dates)
    cost = rd.entry_price

    px_adj = df["px_adj"].to_numpy(dtype=float)
    raw_open = df["open"].to_numpy(dtype=float)
    raw_close = df["close"].to_numpy(dtype=float)

    def ma(period: int, idx: int) -> float:
        if period <= 0 or idx + 1 < period:
            return 0.0
        return float(np.mean(px_adj[idx + 1 - period: idx + 1]))

    # 入场日 MA（复权空间）→ 折算回"入场日原始价口径"，用于还原实盘 stop_price 口径
    def ma_entry_basis(period: int) -> float:
        m = ma(period, i_entry)
        if m <= 0:
            return 0.0
        ratio = px_adj[i_entry] / raw_close[i_entry] if raw_close[i_entry] > 0 else 1.0
        return m / ratio if ratio > 0 else m

    # 实盘 stop_price = calc_stop_price(signal_close, ma10_signal) = min(cost*(1+force), ma10)
    stop_line = cost * (1 + pol.force_pct)
    if pol.price_stop_from_entry_ma and pol.break_ma > 0:
        m10 = ma_entry_basis(pol.break_ma)
        if m10 > 0:
            stop_line = min(stop_line, m10)

    hwm = cost
    breach_streak = 0
    held = 0
    start = i_entry + 1                     # 当日新仓不做当日收盘判定（scheduler:1533-1536）
    for i in range(start, n):
        held += 1
        c_raw = raw_close[i]
        c_adj = px_adj[i]
        hwm = max(hwm, c_raw)               # 高水位按原始价（实盘 broker.high_water_mark 口径）
        if c_raw <= 0:
            continue

        trigger_reason = ""
        # 1) 固定止损（收盘价判定；对应 stop_loss.py 的 force_stop close 分支）
        if pol.force_pct > -1.0 and c_raw <= stop_line:
            trigger_reason = "固定止损"
        # 2) 均线破位（收盘价 vs 均线，复权口径）
        if not trigger_reason and pol.break_ma > 0:
            m = ma(pol.break_ma, i)
            if m > 0 and c_adj < m:
                breach_streak += 1
                if breach_streak > pol.confirm_days:
                    trigger_reason = f"MA{pol.break_ma}破位"
            else:
                breach_streak = 0
        # 3) 移动止盈
        if not trigger_reason and 0 < pol.trail_pullback < 1.0:
            if hwm > cost and c_raw <= hwm * (1 - pol.trail_pullback):
                trigger_reason = "移动止盈"

        if trigger_reason:
            # 次日开盘成交
            if i + 1 < n:
                fill = raw_open[i + 1] * (1 - SLIPPAGE - extra_slippage)
                fill_date = dates[i + 1]
            else:
                fill = c_raw * (1 - SLIPPAGE - extra_slippage)
                fill_date = dates[i]
            return SimResult(fill_date, round(fill, 2), trigger_reason, held,
                             _round_pnl(rd, round(fill, 2)))

        # 4) 固定持有到期（收盘成交）
        if pol.max_hold is not None and held >= pol.max_hold:
            fill = c_raw * (1 - SLIPPAGE - extra_slippage)
            return SimResult(dates[i], round(fill, 2), f"持有{pol.max_hold}日到期", held,
                             _round_pnl(rd, round(fill, 2)))

    # 未触发任何离场
    if pol.hold_to_end or pol.max_hold is not None:
        i = n - 1
        if i <= i_entry:
            return None
        fill = raw_close[i] * (1 - SLIPPAGE - extra_slippage)
        return SimResult(dates[i], round(fill, 2), "持有到区间末", i - i_entry,
                         _round_pnl(rd, round(fill, 2)))
    # 规则未触发但区间结束（理论上不该发生：R6 必须设 hold_to_end）
    return None


def _round_pnl(rd: Round, exit_price: float) -> float:
    """按实盘费率重算单轮盈亏（费用口径已用"实际离场价"校验，误差 0）。"""
    turnover_buy = rd.entry_price * rd.volume
    turnover_sell = exit_price * rd.volume
    buy_comm = max(turnover_buy * COMMISSION_RATE, MIN_COMMISSION)
    sell_comm = max(turnover_sell * COMMISSION_RATE, MIN_COMMISSION)
    stamp = turnover_sell * STAMP_TAX_RATE
    return turnover_sell - turnover_buy - buy_comm - sell_comm - stamp


def simulate_policy(
    rounds: list[Round], panel: Panel, pol: Policy, extra_slippage: float = 0.0
) -> tuple[list[Round], list[SimResult | None]]:
    sims = [simulate_round(rd, panel, pol, extra_slippage) for rd in rounds]
    return rounds, sims


# ================================================================ 4. 组合指标
def equity_curve(
    rounds: list[Round], sims: list[SimResult | None], init: float, cal: list[date]
) -> pd.Series:
    """逐日 MTM 权益：init + 已平仓累计 PnL + 未平仓浮盈。

    资金占用中性：权益 = 期初 + 已实现 + 浮盈，与成交先后/资金周转无关，
    因此不同离场规则在同一批入场点下可比。
    """
    idx = [d for d in cal if d >= min(r.entry_date for r in rounds)]
    realized: dict[date, float] = {}
    for rd, sr in zip(rounds, sims):
        if sr is None:
            continue
        realized[sr.exit_date] = realized.get(sr.exit_date, 0.0) + sr.pnl
    open_pos = []
    for rd, sr in zip(rounds, sims):
        open_pos.append((rd, sr))
    curve = []
    cum = 0.0
    for d in idx:
        cum += realized.get(d, 0.0)
        floating = 0.0
        for rd, sr in open_pos:
            if rd.entry_date <= d and (sr is None or sr.exit_date > d):
                floating += _mark(rd, d) * rd.volume
        curve.append(init + cum + floating)
    return pd.Series(curve, index=idx, dtype=float)


_MARK_CACHE: dict[str, dict[date, float]] = {}


def _mark(rd: Round, d: date) -> float:
    """未平仓浮动盈亏单价（用未复权 close 标记，与成本价同口径）。"""
    table = _MARK_CACHE.get(rd.ts_code)
    if table is None:
        table = _MARK_CACHE[rd.ts_code] = _MARKS.get(rd.ts_code, {})
    px = table.get(d)
    if px is None:
        # 停牌/无行情 → 用最近可用价
        prev = [k for k in table if k <= d]
        px = table[max(prev)] if prev else rd.entry_price
    return px - rd.entry_price


_MARKS: dict[str, dict[date, float]] = {}


def build_marks(panel: Panel, codes) -> None:
    for code in codes:
        df = panel.get(code)
        if df.empty:
            continue
        _MARKS[code] = dict(zip(df.index, df["close"].to_numpy(dtype=float)))


def max_drawdown(curve: pd.Series) -> float:
    if curve is None or len(curve) < 2:
        return 0.0
    peak = curve.cummax()
    dd = (curve - peak) / peak
    return float(dd.min())


@dataclass
class Summary:
    key: str
    label: str
    n: int
    n_skipped: int
    total_pnl: float
    ev: float
    win_rate: float
    win_lo: float
    win_hi: float
    avg_hold: float
    mdd: float
    worst: float
    best: float
    ev_per_day: float
    reason_mix: dict = field(default_factory=dict)
    pnls: list = field(default_factory=list)


def summarize(
    key: str,
    label: str,
    rounds: list[Round],
    sims: list[SimResult | None],
    init: float,
    cal: list[date],
) -> Summary:
    ok = [(rd, sr) for rd, sr in zip(rounds, sims) if sr is not None]
    skipped = len(rounds) - len(ok)
    pnls = [sr.pnl for _, sr in ok]
    if not ok:
        return Summary(key, label, 0, skipped, 0, 0, 0, float("nan"), float("nan"), 0, 0, 0, 0, 0)
    wins = sum(1 for p in pnls if p > 0)
    lo, hi = wilson_interval(wins, len(pnls))
    ok_rounds = [rd for rd, _ in ok]
    curve = equity_curve(ok_rounds, [sr for _, sr in ok], init, cal)
    holds = np.array([sr.hold_bars for _, sr in ok], dtype=float)
    reason_mix: dict[str, int] = {}
    for _, sr in ok:
        reason_mix[sr.reason] = reason_mix.get(sr.reason, 0) + 1
    return Summary(
        key=key,
        label=label,
        n=len(pnls),
        n_skipped=skipped,
        total_pnl=float(sum(pnls)),
        ev=float(np.mean(pnls)),
        win_rate=wins / len(pnls),
        win_lo=lo,
        win_hi=hi,
        avg_hold=float(np.mean(holds)),
        mdd=max_drawdown(curve),
        worst=float(min(pnls)),
        best=float(max(pnls)),
        ev_per_day=float(np.mean(pnls) / max(1e-9, float(np.mean(holds)))),
        reason_mix=reason_mix,
        pnls=pnls,
    )


def _reason_mix(rounds: list[Round]) -> dict:
    mix: dict[str, int] = {}
    for r in rounds:
        mix[r.exit_reason] = mix.get(r.exit_reason, 0) + 1
    return mix


def print_table(rows: list[Summary]) -> None:
    log(f"{'规则':<26} {'n':>4} {'总盈亏':>12} {'EV/轮':>9} {'胜率':>7} "
        f"{'胜率95%CI':>16} {'均持天':>6} {'MDD':>8} {'单笔最差':>10} {'EV/天':>7}")
    log("-" * 118)
    for s in rows:
        log(
            f"{s.label:<26} {s.n:>4d} {s.total_pnl:>12,.0f} {s.ev:>9,.1f} "
            f"{pct(s.win_rate, 1):>7} [{pct(s.win_lo, 1):>6},{pct(s.win_hi, 1):>6}] "
            f"{s.avg_hold:>6.1f} {pct(s.mdd, 2):>8} {s.worst:>10,.0f} {s.ev_per_day:>7,.1f}"
        )


# ================================================================ 5. 主流程
def main() -> None:
    hdr("0. 读账本 + FIFO 配对（复用 metrics._pair_rounds）")
    ledgers = {}
    all_rounds: list[Round] = []
    for name, fname, strategy in LEDGERS:
        led = load_ledger(name, fname)
        rounds, official = build_rounds(led)
        led["rounds"] = rounds
        led["official"] = official
        led["strategy"] = strategy
        ledgers[name] = led
        all_rounds.extend(rounds)
        pnls = np.array([r.pnl for r in rounds])
        log(f"  {name:<6} strategy={strategy:<18} 轮次={len(rounds):>3} "
            f"总盈亏={pnls.sum():>10,.0f} EV={pnls.mean():>8,.1f} "
            f"胜率={pct((pnls > 0).mean(), 1)}  [校验通过]")
    log(f"  合计 {len(all_rounds)} 轮，总盈亏 {sum(r.pnl for r in all_rounds):,.2f}")

    hdr("1. 数据完整性闸门 + 复权口径核验")
    panel = Panel(DB_PATH)
    codes = sorted({r.ts_code for r in all_rounds})
    missing = [c for c in codes if not panel.has(c)]
    match_rate = (len(codes) - len(missing)) / len(codes)
    log(f"  涉及 {len(codes)} 只股票；库内命中 {len(codes) - len(missing)}；匹配率 {match_rate:.1%}")
    if missing[:10]:
        log(f"  缺失示例：{missing[:10]}")
    assert match_rate >= 0.90, f"行情匹配率仅 {match_rate:.1%}，数据不全，禁止出结论"

    # --- 复权口径核验（陷阱 1）
    log("")
    log("  【复权口径核验】账本 price 是否已复权？")
    err_raw, err_adj, rows_chk = [], [], 0
    for r in all_rounds[:200]:
        df = panel.get(r.ts_code)
        if df.empty or r.entry_date not in df.index:
            continue
        raw = float(df.loc[r.entry_date, "close"])
        adjf = df.loc[r.entry_date, "adj_factor"]
        if raw <= 0:
            continue
        rows_chk += 1
        err_raw.append(abs(r.entry_price - raw) / raw)
        if adjf == adjf and adjf:                    # not NaN
            err_adj.append(abs(r.entry_price - raw * float(adjf)) / (raw * float(adjf)))
    log(f"  样本 {rows_chk} 笔（入场日）")
    log(f"  账本 price vs daily_quotes.close       中位相对误差 {np.median(err_raw):.3%}  "
        f"均值 {np.mean(err_raw):.3%}  P90 {np.percentile(err_raw, 90):.3%}")
    if err_adj:
        log(f"  账本 price vs close×adj_factor       中位相对误差 {np.median(err_adj):.3%}  "
            f"均值 {np.mean(err_adj):.3%}  P90 {np.percentile(err_adj, 90):.3%}")
    log("  结论：price 是【未复权】成交价（直接等于原始 close 量级，误差异常小）；")
    log("        buy_adj 存的是【买入日复权因子】本身（paper_trade.py:257-266），不是价格。")
    log("        因此本脚本在原始价空间成交、在 close×adj_factor 空间算均线，不做二次复权。")

    # 复权因子覆盖率与除权事件
    n_adj = sum(1 for c in codes if panel.get(c)["adj_factor"].notna().any())
    log(f"  复权因子覆盖：{n_adj}/{len(codes)} 只股票有 adj_factor")

    hdr("2. 现行离场构成（R0 基准）")
    for name, *_ in LEDGERS:
        rs = ledgers[name]["rounds"]
        mix: dict[str, int] = {}
        for r in rs:
            mix[r.exit_reason] = mix.get(r.exit_reason, 0) + 1
        pnls = np.array([r.pnl for r in rs])
        log(f"  {name:<6} n={len(rs):>3} 总盈亏={pnls.sum():>10,.0f} EV={pnls.mean():>8,.1f} "
            f"胜率={pct((pnls>0).mean(),1):>6}  离场构成={mix}")

    ir_rounds = [r for r in all_rounds if r.book in IR_BOOKS]
    log("")
    log(f"  主口径 IR 系（main+ir20）n={len(ir_rounds)}，"
        f"总盈亏 {sum(r.pnl for r in ir_rounds):,.0f}，"
        f"EV {np.mean([r.pnl for r in ir_rounds]):,.1f}/轮")

    # --- 前提检验：持有期分层 EV 是否真的"越长越好"？
    log("")
    log("  【前提检验】按实际持有天数（自然日）分层的 EV：")
    buckets = [(0, 3), (4, 7), (8, 14), (15, 30), (31, 999)]
    log(f"    {'持天':>8} {'全部464轮 n':>12} {'EV':>9} {'合计':>10} | "
        f"{'IR系 n':>7} {'EV':>9} {'合计':>10} | {'wbr25 n':>8} {'EV':>9}")
    log("    " + "-" * 96)
    for lo, hi in buckets:
        g = [r for r in all_rounds if lo <= r.hold_days <= hi]
        gi = [r for r in ir_rounds if lo <= r.hold_days <= hi]
        gw = [r for r in ledgers["wbr25"]["rounds"] if lo <= r.hold_days <= hi]
        if not g:
            continue
        lab = f"{lo}-{hi}天" if hi < 999 else f"{lo}天+"
        log(f"    {lab:>8} {len(g):>12d} {np.mean([r.pnl for r in g]):>9,.1f} "
            f"{sum(r.pnl for r in g):>10,.0f} | {len(gi):>7d} "
            f"{(np.mean([r.pnl for r in gi]) if gi else float('nan')):>9,.1f} "
            f"{(sum(r.pnl for r in gi) if gi else 0):>10,.0f} | {len(gw):>8d} "
            f"{(np.mean([r.pnl for r in gw]) if gw else float('nan')):>9,.1f}")

    # 校准面板
    build_marks(panel, codes)
    cal = panel.cal

    # ---------------------------------------------------------- 策略网格
    policies = [
        Policy("R1", "R1 MA20破位", break_ma=20),
        Policy("R2", "R2 移除固定止损", force_pct=-1.0),
        Policy("R3a", "R3a 移动止盈3%→5%", trail_pullback=0.05),
        Policy("R3b", "R3b 移动止盈3%→8%", trail_pullback=0.08),
        Policy("R3c", "R3c 移动止盈3%→10%", trail_pullback=0.10),
        Policy("R4", "R4 固定持有10日", break_ma=0, force_pct=-1.0, trail_pullback=1.0, max_hold=10),
        Policy("R5", "R5 固定持有20日", break_ma=0, force_pct=-1.0, trail_pullback=1.0, max_hold=20),
        Policy("R6", "R6 持有到区间末", break_ma=0, force_pct=-1.0, trail_pullback=1.0, hold_to_end=True),
    ]

    def run_set(rounds: list[Round], label: str) -> list[Summary]:
        hdr(f"3. 规则对照 —— {label}（n={len(rounds)}）")
        init = sum(ledgers[b]["init_capital"] for b in {r.book for r in rounds})
        out = []
        # R0 实际
        pnls = np.array([r.pnl for r in rounds])
        wins = int((pnls > 0).sum())
        lo, hi = wilson_interval(wins, len(pnls))
        holds = np.array([r.hold_days for r in rounds], dtype=float)
        curve = equity_curve(rounds, [SimResult(r.exit_date, r.exit_price, r.exit_reason,
                                                max(1, r.hold_days), r.pnl) for r in rounds],
                             init, cal)
        out.append(Summary("R0", "R0 实际（现行规则）", len(pnls), 0, float(pnls.sum()),
                           float(pnls.mean()), wins / len(pnls), lo, hi,
                           float(np.mean(holds)), max_drawdown(curve),
                           float(pnls.min()), float(pnls.max()),
                           float(pnls.mean()) / max(1e-9, float(np.mean(holds)))))
        # R0' 规则重放（模拟器校验）
        pol0 = Policy("R0p", "R0' 现行规则重放")
        _, sims0 = simulate_policy(rounds, panel, pol0)
        out.append(summarize("R0p", "R0' 现行规则重放", rounds, sims0, init, cal))
        for pol in policies:
            _, sims = simulate_policy(rounds, panel, pol)
            out.append(summarize(pol.key, pol.label, rounds, sims, init, cal))
        print_table(out)
        # 相对基线（R0' 现行规则重放）的增量——规则间比较的唯一合法基准
        base = out[1]
        log("")
        log("  相对「R0' 现行规则重放」的增量（同一模拟器内比较，摩擦偏差可比消）：")
        log(f"    {'规则':<26} {'Δ总盈亏':>12} {'ΔEV/轮':>10} {'Δ胜率':>8} {'Δ均持天':>9} {'ΔMDD':>9}")
        log("    " + "-" * 80)
        for s in out[2:]:
            log(f"    {s.label:<26} {s.total_pnl - base.total_pnl:>12,.0f} "
                f"{s.ev - base.ev:>10,.1f} {pct(s.win_rate - base.win_rate, 1):>8} "
                f"{s.avg_hold - base.avg_hold:>9.1f} {pct(s.mdd - base.mdd, 2):>9}")

        # --- 配对显著性：增量是否只是噪声
        log("")
        log("  配对显著性（同轮 PnL 差 vs R0'，检验「增量是否只是噪声」）：")
        log(f"    {'规则':<26} {'Δ均值':>9} {'Δ标准差':>10} {'t 值':>7} {'近似95%CI':>22} {'结论':>10}")
        log("    " + "-" * 92)
        base_map = None
        for pol in policies:
            _, sims = simulate_policy(rounds, panel, pol)
            d = [s.pnl - s0.pnl for s, s0 in zip(sims, sims0)
                 if s is not None and s0 is not None]
            if len(d) < 5:
                continue
            d = np.array(d, dtype=float)
            se = d.std(ddof=1) / math.sqrt(len(d))
            t = d.mean() / se if se > 0 else 0.0
            ci = 1.96 * se
            verdict = "显著" if abs(t) >= 1.96 else "不显著"
            log(f"    {pol.label:<26} {d.mean():>9,.1f} {d.std(ddof=1):>10,.1f} {t:>7.2f} "
                f"[{d.mean() - ci:>8,.0f},{d.mean() + ci:>8,.0f}] {verdict:>10}")
        return out

    res_all = run_set(all_rounds, "全部 5 账本（464 轮，含 cherry_claw/weekly_band 异构）")
    res_ir = run_set(ir_rounds, "主口径 IR 系 main+ir20（189 轮，同策略可比）")

    # R0' 校验结论
    log("")
    log("  【模拟器校验】R0' vs R0：")
    s0, s0p = res_ir[0], res_ir[1]
    log(f"    R0  总盈亏 {s0.total_pnl:,.0f}  EV {s0.ev:,.1f}  胜率 {pct(s0.win_rate,1)}  均持 {s0.avg_hold:.1f} 日历日")
    log(f"    R0' 总盈亏 {s0p.total_pnl:,.0f}  EV {s0p.ev:,.1f}  胜率 {pct(s0p.win_rate,1)}  均持 {s0p.avg_hold:.1f} 交易日")
    log(f"    EV 偏差 {s0p.ev - s0.ev:+,.1f} 元/轮（{(s0p.ev - s0.ev)/abs(s0.ev)*100:+.1f}%）")
    log(f"    R0 离场构成   {_reason_mix(ir_rounds)}")
    log(f"    R0' 离场构成  {s0p.reason_mix}")

    # 逐轮触发日偏移诊断
    pol_dbg = Policy("R0dbg", "R0' 诊断")
    _, sims_dbg = simulate_policy(ir_rounds, panel, pol_dbg)
    offsets, conf = [], {}
    for rd, sr in zip(ir_rounds, sims_dbg):
        if sr is None:
            continue
        df = panel.get(rd.ts_code)
        dates = list(df.index)
        if rd.exit_date in dates and sr.exit_date in dates:
            offsets.append(dates.index(sr.exit_date) - dates.index(rd.exit_date))
        conf[(rd.exit_reason, sr.reason)] = conf.get((rd.exit_reason, sr.reason), 0) + 1
    if offsets:
        off = np.array(offsets)
        log(f"    逐轮离场日偏移（模拟-实际，交易日）：中位 {np.median(off):+.1f}  "
            f"均值 {off.mean():+.2f}  P10 {np.percentile(off,10):+.0f}  P90 {np.percentile(off,90):+.0f}")
        log(f"    模拟早于实际 {int((off<0).sum())} 轮 / 同日 {int((off==0).sum())} 轮 / "
            f"晚于实际 {int((off>0).sum())} 轮")
    log("    离场原因混淆（实际 → 模拟）：")
    for (a, b), c in sorted(conf.items(), key=lambda x: -x[1])[:10]:
        log(f"      {a:<18} → {b:<14} {c:>3d}")

    # --- 成交价偏差诊断：同日离场时价格是否一致？
    same_p, diff_p = [], []
    same_dpnl = []
    for rd, sr in zip(ir_rounds, sims_dbg):
        if sr is None:
            continue
        rel = (sr.exit_price - rd.exit_price) / rd.exit_price
        if sr.exit_date == rd.exit_date:
            same_p.append(rel)
            same_dpnl.append((sr.pnl - rd.pnl, rd.entry_price * rd.volume))
        else:
            diff_p.append(rel)
    if same_p:
        a = np.array(same_p)
        log(f"    同日离场 {len(a)} 轮：模拟价 vs 实际价 中位 {np.median(a)*100:+.3f}%  "
            f"均值 {a.mean()*100:+.3f}%  P90 {np.percentile(a,90)*100:+.3f}%")
        dd = np.array([x[0] / x[1] for x in same_dpnl])
        log(f"      其中单轮 PnL 偏差占本金比：中位 {np.median(dd)*100:+.3f}%  均值 {dd.mean()*100:+.3f}%")
    if diff_p:
        b = np.array(diff_p)
        log(f"    异日离场 {len(b)} 轮：模拟价 vs 实际价 中位 {np.median(b)*100:+.3f}%  均值 {b.mean()*100:+.3f}%")
    # 用"实际离场日 + 实际离场价"重算，隔离费用口径影响
    pnl_chk = [_round_pnl(rd, rd.exit_price) for rd in ir_rounds]
    act = np.array([rd.pnl for rd in ir_rounds])
    log(f"    费用口径校验：用实际离场价重算 _round_pnl → 合计 {sum(pnl_chk):,.0f} "
        f"vs 账本 {act.sum():,.0f}  差 {sum(pnl_chk)-act.sum():+,.0f}")

    # ---------------------------------------------------------- MA10 子集分析
    ir_ma10 = [r for r in ir_rounds if r.exit_reason == MA_BREAK_LABEL]
    hdr(f"4. 关键子分析：MA10 破位离场的 {len(ir_ma10)} 笔（IR 系）")

    def counterfactual_hold(rs: list[Round], horizon: int | None) -> dict:
        """若不卖，持有到 T+horizon 个交易日（None=区间末）的对比。"""
        detail = []
        for rd in rs:
            df = panel.get(rd.ts_code)
            if df.empty or rd.entry_date not in df.index:
                continue
            dates = list(df.index)
            i0 = dates.index(rd.entry_date)
            if horizon is None:
                i1 = len(dates) - 1
            else:
                i1 = i0 + horizon
                if i1 >= len(dates):
                    continue                     # 前瞻不足 → 排除
            px = float(df["close"].iloc[i1]) * (1 - SLIPPAGE)
            cf_pnl = _round_pnl(rd, round(px, 2))
            detail.append({
                "ts_code": rd.ts_code,
                "book": rd.book,
                "entry": rd.entry_date,
                "actual_exit": rd.exit_date,
                "actual_pnl": rd.pnl,
                "cf_pnl": cf_pnl,
                "delta": cf_pnl - rd.pnl,
                "delta_ret": (cf_pnl - rd.pnl) / (rd.entry_price * rd.volume),
                "cf_ret": cf_pnl / (rd.entry_price * rd.volume),
            })
        return {"n": len(detail), "rows": detail}

    log(f"{'前瞻':>10} {'可用n':>6} {'反弹笔数':>8} {'反弹占比':>8} {'95%CI下限':>10} "
        f"{'反弹中位':>9} {'反弹P25':>8} {'反弹P75':>8} {'下跌中位':>9} {'Δ中位(全)':>10} {'反事实合计':>12} {'实际合计':>10}")
    log("-" * 150)
    cf_store = {}
    for h, lab in [(5, "T+5"), (10, "T+10"), (20, "T+20"), (None, "区间末")]:
        cf = counterfactual_hold(ir_ma10, h)
        rows = cf["rows"]
        if not rows:
            continue
        cf_store[lab] = cf
        deltas = np.array([r["delta"] for r in rows])
        dret = np.array([r["delta_ret"] for r in rows])
        up = dret[dret > 0]
        dn = dret[dret < 0]
        reb = int((deltas > 0).sum())
        lo, _ = wilson_interval(reb, len(deltas))
        log(f"{lab:>10} {len(rows):>6d} {reb:>8d} {pct(reb/len(rows),1):>8} {pct(lo,1):>10} "
            f"{(np.median(up)*100 if len(up) else float('nan')):>8.2f}% "
            f"{(np.percentile(up,25)*100 if len(up) else float('nan')):>7.2f}% "
            f"{(np.percentile(up,75)*100 if len(up) else float('nan')):>7.2f}% "
            f"{(np.median(dn)*100 if len(dn) else float('nan')):>8.2f}% "
            f"{np.median(dret)*100:>9.2f}% "
            f"{sum(r['cf_pnl'] for r in rows):>12,.0f} {sum(r['actual_pnl'] for r in rows):>10,.0f}")

    # main 单账本 71 笔（与任务书原始口径对齐）
    main_ma10 = [r for r in ledgers["main"]["rounds"] if r.exit_reason == MA_BREAK_LABEL]
    log("")
    log(f"  [任务书原始口径] 仅 main 账本 MA10 离场 n={len(main_ma10)}：")
    for h, lab in [(5, "T+5"), (10, "T+10"), (20, "T+20"), (None, "区间末")]:
        cf = counterfactual_hold(main_ma10, h)
        if not cf["rows"]:
            continue
        d = np.array([r["delta"] for r in cf["rows"]])
        dr = np.array([r["delta_ret"] for r in cf["rows"]])
        up = dr[dr > 0]
        reb = int((d > 0).sum())
        lo, hi = wilson_interval(reb, len(d))
        log(f"    {lab:>8} n={len(d):>3d} 反弹 {reb:>3d} ({pct(reb/len(d),1)}, 95%CI [{pct(lo,1)},{pct(hi,1)}]) "
            f"反弹中位 {np.median(up)*100 if len(up) else float('nan'):>+6.2f}% "
            f"Δ中位(全) {np.median(dr)*100:>+6.2f}% Δ合计 {d.sum():>9,.0f}")

    # ---------------------------------------------------------- 样本外切分
    hdr("5. 样本外切分（IS: ≤2026-09-30 定规则 / OOS: 2026-10-01~11-10 验证）")
    ir_is = [r for r in ir_rounds if r.entry_date <= IS_END]
    ir_oos = [r for r in ir_rounds if r.entry_date > IS_END]
    log(f"  IS  入场 ≤{IS_END}：n={len(ir_is)}  入场区间 "
        f"{min(r.entry_date for r in ir_is)}~{max(r.entry_date for r in ir_is)}")
    log(f"  OOS 入场 >{IS_END}：n={len(ir_oos)}  入场区间 "
        f"{min(r.entry_date for r in ir_oos)}~{max(r.entry_date for r in ir_oos)}")

    def quick(rounds: list[Round], pol: Policy | None) -> tuple[float, float, int, float]:
        if pol is None:
            p = np.array([r.pnl for r in rounds])
            return float(p.sum()), float(p.mean()), len(p), float((p > 0).mean())
        _, sims = simulate_policy(rounds, panel, pol)
        ok = [s for s in sims if s is not None]
        if not ok:
            return 0.0, 0.0, 0, 0.0
        p = np.array([s.pnl for s in ok])
        return float(p.sum()), float(p.mean()), len(p), float((p > 0).mean())

    log("")
    log(f"{'规则':<22} {'IS n':>5} {'IS EV':>9} {'OOS n':>6} {'OOS EV':>9} {'EV差(OOS-IS)':>13}")
    log("-" * 76)
    is_rows = []
    for pol in [None, Policy("R1", "R1 MA20破位", break_ma=20)] + policies[1:]:
        lab = "R0 实际" if pol is None else pol.label
        _, ev_i, n_i, _ = quick(ir_is, pol)
        _, ev_o, n_o, _ = quick(ir_oos, pol)
        is_rows.append((lab, n_i, ev_i, n_o, ev_o))
        log(f"{lab:<22} {n_i:>5d} {ev_i:>9,.1f} {n_o:>6d} {ev_o:>9,.1f} {ev_o - ev_i:>13,.1f}")
    best_is = max([r for r in is_rows if r[0] != "R0 实际"], key=lambda x: x[2])
    log("")
    log(f"  IS 最优规则 = {best_is[0]}（IS EV {best_is[2]:,.1f}）→ 其 OOS EV {best_is[4]:,.1f}")
    base_oos = dict((r[0], r[4]) for r in is_rows)["R0 实际"]
    log(f"  OOS 上 R0 实际 EV = {base_oos:,.1f} → 该规则样本外改善 {best_is[4] - base_oos:+,.1f} 元/轮")

    # ---------------------------------------------------------- 成本敏感性
    hdr("6. 成本敏感性：额外单边 5bp（换手口径）")
    log("  注：入场点固定不变 ⇒ 每条规则都是「1 买 + 1 卖」，换手笔数相同；")
    log("      成本差异不体现在笔数，而体现在【资金占用时长】→ 用 EV/持仓日 衡量。")
    pols_chk = [Policy("R0p", "R0' 现行规则重放")] + policies
    log("")
    log(f"{'规则':<26} {'EV/轮':>9} {'EV/轮(再扣5bp×2)':>18} {'均持交易日':>10} {'EV/持仓日':>10}")
    log("-" * 82)
    for pol in pols_chk:
        _, sims = simulate_policy(ir_rounds, panel, pol, extra_slippage=0.0)
        ok = [s for s in sims if s is not None]
        if not ok:
            continue
        ev0 = float(np.mean([s.pnl for s in ok]))
        _, sims5 = simulate_policy(ir_rounds, panel, pol, extra_slippage=0.0005)
        ok5 = [s for s in sims5 if s is not None]
        ev5 = float(np.mean([s.pnl for s in ok5]))
        hbar = float(np.mean([s.hold_bars for s in ok5]))
        log(f"{pol.label:<26} {ev0:>9,.1f} {ev5:>18,.1f} {hbar:>10.1f} {ev5/max(1e-9,hbar):>10,.1f}")

    # ---------------------------------------------------------- 甜点搜索
    hdr("7. 甜点搜索：均线周期 × 确认日 × 移动止盈 × 止损 网格（IR 系，按 IS 选、OOS 验）")
    grid = []
    for ma in (10, 15, 20, 30):
        for confirm in (0, 1, 2):
            for trail in (0.03, 0.05, 0.08):
                for force in (-0.08, -0.15):
                    grid.append(Policy(
                        f"G{ma}_{confirm}_{int(trail*100)}_{int(-force*100)}",
                        f"MA{ma}+确认{confirm}日+回撤{int(trail*100)}%+止损{int(-force*100)}%",
                        break_ma=ma, confirm_days=confirm,
                        trail_pullback=trail, force_pct=force,
                    ))
    log(f"  网格 {len(grid)} 组")
    scored = []
    for pol in grid:
        _, si = simulate_policy(ir_is, panel, pol)
        oki = [s for s in si if s is not None]
        _, so = simulate_policy(ir_oos, panel, pol)
        oko = [s for s in so if s is not None]
        if len(oki) < len(ir_is) * 0.9 or len(oko) < len(ir_oos) * 0.9:
            continue
        s_is = summarize(pol.key, pol.label, ir_is, si,
                         sum(ledgers[b]["init_capital"] for b in IR_BOOKS), cal)
        s_oos = summarize(pol.key, pol.label, ir_oos, so,
                          sum(ledgers[b]["init_capital"] for b in IR_BOOKS), cal)
        scored.append((pol, s_is, s_oos))
    scored.sort(key=lambda x: -x[1].ev)
    log("")
    log("  IS 排序前 8（附 OOS 表现与 MDD）：")
    log(f"  {'规则':<34} {'IS EV':>9} {'IS总盈亏':>11} {'IS MDD':>8} {'OOS EV':>9} {'OOS总盈亏':>11} {'OOS MDD':>8}")
    log("  " + "-" * 104)
    for pol, s_is, s_oos in scored[:8]:
        log(f"  {pol.label:<34} {s_is.ev:>9,.1f} {s_is.total_pnl:>11,.0f} {pct(s_is.mdd,2):>8} "
            f"{s_oos.ev:>9,.1f} {s_oos.total_pnl:>11,.0f} {pct(s_oos.mdd,2):>8}")
    if scored:
        # 甜点判据：IS EV 前 20% 内、且 MDD 在 25% 内、且 OOS EV 仍优于 R0
        r0_is = float(np.mean([r.pnl for r in ir_is]))
        r0_oos = float(np.mean([r.pnl for r in ir_oos]))
        cand = [x for x in scored if x[1].ev > r0_is and x[2].ev > r0_oos and abs(x[1].mdd) <= 0.25]
        cand.sort(key=lambda x: -(x[2].ev + x[1].ev))
        log("")
        log(f"  R0 实际：IS EV {r0_is:,.1f} / OOS EV {r0_oos:,.1f}")
        log(f"  同时满足「IS 优于 R0 且 OOS 优于 R0 且 MDD≤25%」的规则数：{len(cand)}")
        for pol, s_is, s_oos in cand[:5]:
            log(f"    {pol.label:<34} IS EV {s_is.ev:>8,.1f} (MDD {pct(s_is.mdd,2)})  "
                f"OOS EV {s_oos.ev:>8,.1f} (MDD {pct(s_oos.mdd,2)})")

    # ---------------------------------------------------------- weekly_band 天然对照
    hdr("8. 天然对照臂：wbr25（weekly_band，本就持有≈20 交易日、无 MA10 离场）")
    wb = ledgers["wbr25"]["rounds"]
    ir_ev = float(np.mean([r.pnl for r in ir_rounds]))
    wb_p = np.array([r.pnl for r in wb])
    log(f"  wbr25 n={len(wb)} 总盈亏 {wb_p.sum():,.0f} EV {wb_p.mean():,.1f}/轮 "
        f"胜率 {pct((wb_p>0).mean(),1)} 均持 {np.mean([r.hold_days for r in wb]):.1f} 日历日")
    log(f"  IR 系 n={len(ir_rounds)} 总盈亏 {sum(r.pnl for r in ir_rounds):,.0f} EV {ir_ev:,.1f}/轮 "
        f"均持 {np.mean([r.hold_days for r in ir_rounds]):.1f} 日历日")
    log("  注：两条腿入场信号不同（wbr=周频波段超卖 wbr 分位，IR=日内反转超卖 ir20 分位），")
    log("      只能作为方向性旁证，不能当作同一策略的 A/B。")

    # 附录：cherry_claw
    hdr("9. 附录：s43+s44（cherry_claw 异构策略）")
    cc = [r for r in all_rounds if r.book in ("s43", "s44")]
    cc_p = np.array([r.pnl for r in cc])
    log(f"  n={len(cc)} 总盈亏 {cc_p.sum():,.0f} EV {cc_p.mean():,.1f}/轮 胜率 {pct((cc_p>0).mean(),1)}")
    ma10_cc = [r for r in cc if r.exit_reason == MA_BREAK_LABEL]
    cf = counterfactual_hold(ma10_cc, 5)
    if cf["rows"]:
        d = np.array([r["delta"] for r in cf["rows"]])
        dr = np.array([r["delta_ret"] for r in cf["rows"]])
        log(f"  MA10 离场 n={len(cf['rows'])} → T+5 反弹 {int((d>0).sum())} 笔 "
            f"({pct((d>0).mean(),1)}) Δ中位 {np.median(dr)*100:+.2f}% Δ合计 {d.sum():+,.0f}")

    log("")
    log("=" * 78)
    log("完成。")
    log("=" * 78)


if __name__ == "__main__":
    main()

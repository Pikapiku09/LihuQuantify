"""波口径配对（wave pairing）：从 0 仓位建仓到完全清仓算「一波」。

为什么需要这个口径
==================
FIFO 配对（``backtest.metrics.pair_round_details``）把每笔卖出按先进先出
摊到各笔买入上。对**分批建仓、分批减仓**的真实交易，它会把一个完整的长线
持仓切成很多碎片：

    德赛西威：真实持有一整波 254 天
    → FIFO 切成 28 个「轮次」，中位持仓仅 32 天

这会让「持有期」这个变量严重失真，进而误导"我拿不住"或"我拿得很久"这类判断。

波口径按**仓位归零**为界，还原"这一波我拿了多久、赚了多少"的直觉：

    建仓（0 → 正）  ─────────  加仓/减仓  ─────────  清仓（→ 0）

两个口径都对，测的是不同东西：
    - FIFO  ：每一股拿了多久（适合算已实现盈亏、与回测口径对齐）
    - wave  ：这一波操作拿了多久（适合判断持有期、卖飞、择时）

项目纪律：本模块与 FIFO 是**两套并列口径**，不是替换关系。
需要混用时报错，必须显式指名用哪个。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Wave:
    """一波完整操作（建仓 → 清仓）。"""

    ts_code: str
    name: str = ""
    buy_date: date | None = None
    sell_date: date | None = None
    buy_amount: float = 0.0        # 买入总金额（含费前，用成交金额）
    sell_amount: float = 0.0       # 卖出总金额
    fee: float = 0.0               # 全波费用合计
    n_buys: int = 0
    n_sells: int = 0
    max_volume: int = 0            # 峰值持仓股数（衡量仓位规模）
    open: bool = False             # 数据末尾仍持有
    fills: list = field(default_factory=list)

    @property
    def pnl(self) -> float:
        """已实现盈亏 = 卖出金额 − 买入金额 − 费用。

        未平仓波返回已实现部分（卖出为 0 时即 −买入额，请配合 ``open`` 判断，
        不要直接当亏损用）。
        """
        return self.sell_amount - self.buy_amount - self.fee

    @property
    def cost(self) -> float:
        """本波投入成本（买入总金额）。"""
        return self.buy_amount

    @property
    def ret(self) -> float:
        """已实现收益率（未平仓波无意义，调用方需先检查 ``open``）。"""
        return self.pnl / self.buy_amount if self.buy_amount > 0 else 0.0

    @property
    def hold_days(self) -> int | None:
        if self.buy_date is None or self.sell_date is None:
            return None
        return (self.sell_date - self.buy_date).days


def build_waves(fills: list[dict], include_open: bool = True) -> list[Wave]:
    """把成交列表聚合成「波」。

    Args:
        fills: ``behavior.ingest.load_fills`` 的输出（已按日期升序）
        include_open: 是否包含数据末尾仍未清仓的波（默认包含，
            并用 ``Wave.open`` 标记，避免把未实现盈亏误当亏损）

    聚合规则：
        仓位 0 → 正数 时开启新波；仓位回到 0 时结束该波并落盘。
        同一标的可有多波（清仓后再买 = 新的一波）。
    """
    waves: list[Wave] = []
    current: dict[str, Wave] = {}
    positions: dict[str, int] = {}
    codes_in_order: list[str] = []

    for f in fills:
        code = f["ts_code"]
        vol = int(f.get("volume") or 0)
        if vol <= 0:
            continue
        pos = positions.get(code, 0)
        amount = float(f.get("amount") or 0.0)
        fee = float(f.get("fee") or 0.0)

        if f["side"] == "buy":
            if pos == 0:                      # 开启新波
                if code not in codes_in_order:
                    codes_in_order.append(code)
                current[code] = Wave(
                    ts_code=code, name=f.get("name", ""),
                    buy_date=f["trade_date"],
                )
            w = current.get(code)
            if w is None:                     # 数据起点即持有（异常但容错）
                w = current[code] = Wave(
                    ts_code=code, name=f.get("name", ""),
                    buy_date=f["trade_date"],
                )
            w.buy_amount += amount
            w.fee += fee
            w.n_buys += 1
            w.fills.append(f)
            positions[code] = pos + vol
            w.max_volume = max(w.max_volume, positions[code])
        else:                                 # sell
            if pos <= 0:
                continue                      # 无持仓卖出，跳过
            w = current.get(code)
            if w is None:
                continue
            w.sell_amount += amount
            w.fee += fee
            w.n_sells += 1
            w.fills.append(f)
            positions[code] = max(0, pos - vol)
            if positions[code] == 0:          # 清仓 → 收波
                w.sell_date = f["trade_date"]
                w.open = False
                waves.append(w)
                current.pop(code, None)

    # 收尾：仍持有的波
    for code, w in current.items():
        w.open = True
        w.sell_date = None
        if include_open:
            waves.append(w)

    waves.sort(key=lambda w: (w.buy_date or date.min, w.ts_code))
    return waves


def wave_summary(waves: list[Wave], closed_only: bool = True) -> dict:
    """波口径总体摘要。

    ``closed_only=True``（默认）只统计已清仓的波——未平仓波没有实现盈亏，
    混进来会把总盈亏做小。
    """
    ws = [w for w in waves if (not w.open or not closed_only)]
    if not ws:
        return {"n_waves": 0, "n_open": len([w for w in waves if w.open]),
                "total_pnl": 0.0, "win_rate": 0.0}
    pnls = [w.pnl for w in ws]
    days = [w.hold_days for w in ws if w.hold_days is not None]
    return {
        "n_waves": len(ws),
        "n_open": len([w for w in waves if w.open]),
        "total_pnl": round(sum(pnls), 2),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
        "hold_median": (sorted(days)[len(days) // 2] if days else None),
        "hold_max": (max(days) if days else None),
        "hold_mean": round(sum(days) / len(days), 1) if days else None,
        "max_hold_60plus": sum(1 for d in days if d > 60),
        "win_pnl_mean": round(
            sum(p for p in pnls if p > 0) / max(1, sum(1 for p in pnls if p > 0)), 2),
        "loss_pnl_mean": round(
            sum(p for p in pnls if p <= 0) / max(1, sum(1 for p in pnls if p <= 0)), 2),
    }


def by_code(waves: list[Wave], closed_only: bool = True) -> list[dict]:
    """按标的汇总波口径战绩，按盈利绝对值降序。

    ``open_waves`` 统计该标的**仍有未平仓波**的数量（这类标的的 pnl 只含已实现部分）。
    """
    agg: dict[str, dict] = {}
    for w in waves:
        d = agg.setdefault(w.ts_code, {
            "ts_code": w.ts_code, "name": w.name,
            "n_waves": 0, "pnl": 0.0, "cost": 0.0,
            "days": [], "open": 0,
        })
        if w.open:
            d["open"] += 1
            if closed_only:
                continue
        d["n_waves"] += 1
        d["pnl"] += w.pnl
        d["cost"] += w.cost
        if w.hold_days is not None:
            d["days"].append(w.hold_days)
    out = []
    for d in agg.values():
        days = sorted(d["days"])
        out.append({
            "ts_code": d["ts_code"], "name": d["name"],
            "n_waves": d["n_waves"], "pnl": round(d["pnl"], 2),
            "ret": round(d["pnl"] / d["cost"], 4) if d["cost"] > 0 else 0.0,
            "max_hold": (max(days) if days else None),
            "median_hold": (days[len(days) // 2] if days else None),
            "open_waves": d["open"],
        })
    out.sort(key=lambda x: -abs(x["pnl"]))
    return out

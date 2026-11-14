"""行为审计指标（全部纯函数，无 IO / 无网络 / 无全局状态）。

这些指标回答的是**交易者本人的行为偏差**，不是策略好坏：

======================  ====================================================
函数                    量的是什么
======================  ====================================================
``pgr_plr``             盈利单 vs 亏损单的持有天数差（处置效应经典测度）
``loss_add_ratio``      浮亏加仓占买入额比（本项目实证最大亏损来源：27.4%）
``peak_capture``        峰值捕获率 = 卖出价 ÷ 持有期最高价（中位 26.5%）
``post_sell_upside``    卖出后是否还在涨（94.4% 的盈利单卖后继续上涨）
``turnover``            年化换手 + 摩擦成本估算（6.5 → 29.6 倍）
``position_sizing``     单笔仓位分布 + 盈亏单规模比（1.40x）
======================  ====================================================

口径约定（重要，避免再次出现「同一问题两个答案」）：
    - 轮次一律来自 ``backtest.metrics.pair_round_details``（FIFO 配对单一实现）
    - 持有天数 = 卖出日 − 买入日（自然日，非交易日）
    - 「浮亏加仓」用移动加权平均成本口径：买入时该票已有正持仓且
      成交价 < 当前平均成本
    - 行情相关指标通过 ``high_lookup(code, start, end) -> float | None``
      回调注入，保证本模块不直接依赖 DuckDB
"""

from __future__ import annotations

import random
from datetime import date

import numpy as np

# 一次完整往返摩擦成本：佣金万2.5×2 + 印花税万5 + 滑点0.1%×2 = 0.30%
ROUND_TRIP_COST = 0.0030


# ============================================================
# 小工具
# ============================================================

def _finite(xs) -> list[float]:
    out = []
    for x in xs:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v == v and abs(v) != float("inf"):
            out.append(v)
    return out


def _mean(xs) -> float:
    v = _finite(xs)
    return float(np.mean(v)) if v else 0.0


def _median(xs) -> float:
    v = _finite(xs)
    return float(np.median(v)) if v else 0.0


def _quantile(xs, q: float) -> float:
    v = _finite(xs)
    return float(np.quantile(v, q)) if v else 0.0


# ============================================================
# 1. 处置效应：盈利单 vs 亏损单的持有期差
# ============================================================

def pgr_plr(rounds: list[dict], n_boot: int = 2000, seed: int = 42) -> dict:
    """盈利/亏损卖出的持有期对比 + bootstrap 95% CI。

    处置效应的经典预期是「卖赢家、扛亏家」→ 亏损单持有更久。
    若 CI 跨 0，则该样本**不支持**持有期层面的处置效应（这本身是有价值的结论）。
    """
    win = [r for r in rounds if float(r.get("pnl") or 0) > 0
           and r.get("hold_days") is not None]
    loss = [r for r in rounds if float(r.get("pnl") or 0) <= 0
            and r.get("hold_days") is not None]
    win_days = [float(r["hold_days"]) for r in win]
    loss_days = [float(r["hold_days"]) for r in loss]
    win_pnl = [float(r["pnl"]) for r in win]
    loss_pnl = [float(r["pnl"]) for r in loss]

    diff = (_mean(win_days) - _mean(loss_days)) if (win_days and loss_days) else 0.0
    ci_low = ci_high = 0.0
    if win_days and loss_days:
        rng = random.Random(seed)
        boots = []
        for _ in range(max(200, int(n_boot))):
            w = [rng.choice(win_days) for _ in win_days]
            l = [rng.choice(loss_days) for _ in loss_days]
            boots.append(float(np.mean(w)) - float(np.mean(l)))
        boots.sort()
        lo = boots[int(0.025 * (len(boots) - 1))]
        hi = boots[int(0.975 * (len(boots) - 1))]
        ci_low, ci_high = lo, hi

    win_abs = abs(_mean(win_pnl))
    loss_abs = abs(_mean(loss_pnl))
    return {
        "n_win": len(win), "n_loss": len(loss),
        "hold_win_mean": round(_mean(win_days), 2),
        "hold_loss_mean": round(_mean(loss_days), 2),
        "hold_diff": round(diff, 2),
        "diff_ci_low": round(ci_low, 2),
        "diff_ci_high": round(ci_high, 2),
        # CI 跨 0 → 不显著：不能用"拿不住"解释
        "significant": bool(ci_low > 0 or ci_high < 0),
        "win_pnl_mean": round(_mean(win_pnl), 2),
        "loss_pnl_mean": round(_mean(loss_pnl), 2),
        "win_loss_magnitude_ratio": (round(win_abs / loss_abs, 2) if loss_abs > 0 else 0.0),
    }


# ============================================================
# 2. 浮亏加仓（本项目最大的亏损来源）
# ============================================================

def loss_add_ratio(fills: list[dict]) -> dict:
    """买入时该票已有正持仓、且成交价低于当前平均成本 → 记为「浮亏加仓」。

    用移动加权平均成本：买入抬升成本、卖出按均价摊减。
    返回笔数/金额/占全部买入额比——这是可直接指导规则的硬数字。
    """
    pos: dict[str, dict] = {}
    total_buy_amount = 0.0
    n_buys = 0
    n_adds = 0
    add_amount = 0.0

    for f in fills:
        code = f["ts_code"]
        amount = float(f.get("amount") or 0.0)
        price = float(f.get("price") or 0.0)
        volume = int(f.get("volume") or 0)
        if volume <= 0:
            continue
        p = pos.get(code)
        if f["side"] == "buy":
            n_buys += 1
            total_buy_amount += amount
            if p and p["volume"] > 0:
                avg = p["cost"] / p["volume"]
                if price < avg:
                    n_adds += 1
                    add_amount += amount
            if p is None:
                pos[code] = {"volume": volume, "cost": price * volume}
            else:
                p["volume"] += volume
                p["cost"] += price * volume
        else:  # sell：按均价摊减，清零则移出
            if not p or p["volume"] <= 0:
                continue
            sell_vol = min(volume, p["volume"])
            avg = p["cost"] / p["volume"]
            p["cost"] -= avg * sell_vol
            p["volume"] -= sell_vol
            if p["volume"] <= 0:
                pos.pop(code, None)

    ratio = (add_amount / total_buy_amount) if total_buy_amount > 0 else 0.0
    return {
        "n_buys": n_buys,
        "n_adds": n_adds,
        "add_amount": round(add_amount, 2),
        "total_buy_amount": round(total_buy_amount, 2),
        "ratio": round(ratio, 4),               # 占全部买入额
        "ratio_of_buys": round(n_adds / n_buys, 4) if n_buys else 0.0,
    }


# ============================================================
# 3. 峰值捕获率
# ============================================================

def peak_capture(rounds: list[dict], high_lookup) -> dict:
    """涨幅捕获率 = (卖出价 - 买入价) ÷ (持有期最高价 - 买入价)。

    ``high_lookup(code, start_date, end_date) -> float | None``
    1.0 = 吃满整段涨幅；0.0 = 白干一场；负数表示持有期内几乎没涨却卖了。

    为什么不用「卖出价 ÷ 最高价」：持有期中位仅 12 天时，区间最高收盘价
    天然贴近卖出价，价格比会给出 0.96 这种"几乎卖在最高点"的假象。
    涨幅口径才对得上"卖飞了多少"这个直觉，也与"96% 卖在峰值以下"自洽。
    持有期内未上涨（最高价 <= 买入价）的轮次无法定义"捕获"，直接跳过。
    """
    vals = []
    skipped_no_rise = 0
    skipped_tiny_range = 0
    for r in rounds:
        bd, sd = r.get("buy_date"), r.get("sell_date")
        if not isinstance(bd, date) or not isinstance(sd, date):
            continue
        hi = high_lookup(r["ts_code"], bd, sd)
        if hi is None:
            continue
        try:
            hi = float(hi)
            bp = float(r["buy_price"])
            sp = float(r["sell_price"])
        except (TypeError, ValueError):
            continue
        denom = hi - bp
        if denom <= 0:
            skipped_no_rise += 1
            continue
        # 分母过小（区间最高价仅比买价高不到 1%）时捕获率极不稳定：
        # 一个 denom=0.01 的轮次就能让比值变成 -230%，把均值彻底带偏。
        # 这类轮次剔除并单独计数，比 winsorize 更可解释。
        if denom < abs(bp) * 0.01:
            skipped_tiny_range += 1
            continue
        vals.append((sp - bp) / denom)
    if not vals:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p25": 0.0, "p75": 0.0,
                "below_half_pct": 0.0, "skipped_no_rise": skipped_no_rise,
                "skipped_tiny_range": skipped_tiny_range}
    arr = np.array(vals)
    return {
        "n": len(vals),
        "mean": round(float(arr.mean()), 4),
        "median": round(float(np.median(arr)), 4),
        "p25": round(float(np.quantile(arr, 0.25)), 4),
        "p75": round(float(np.quantile(arr, 0.75)), 4),
        "below_half_pct": round(float((arr < 0.5).mean()), 4),
        "skipped_no_rise": skipped_no_rise,
        "skipped_tiny_range": skipped_tiny_range,
    }


# ============================================================
# 4. 卖出后是否还在涨（卖飞的成本）
# ============================================================

def post_sell_upside(rounds: list[dict], high_lookup,
                     window: int = 20, profit_only: bool = True) -> dict:
    """卖出后 ``window`` 个自然日内的最高价相对卖出价的涨幅。

    ``profit_only=True`` 只看盈利单——这类"卖飞"才是机会成本。
    返回「继续上涨的笔数占比」与涨幅分布。
    """
    gains = []
    for r in rounds:
        if profit_only and float(r.get("pnl") or 0) <= 0:
            continue
        sd = r.get("sell_date")
        if not isinstance(sd, date):
            continue
        end = date.fromordinal(sd.toordinal() + int(window))
        hi = high_lookup(r["ts_code"], sd, end)
        if hi is None:
            continue
        try:
            hi = float(hi)
            sp = float(r["sell_price"])
        except (TypeError, ValueError):
            continue
        if sp <= 0 or hi <= 0:
            continue
        gains.append(hi / sp - 1.0)
    if not gains:
        return {"n": 0, "window": window, "continued_up_pct": 0.0,
                "mean_gain_pct": 0.0, "median_gain_pct": 0.0}
    arr = np.array(gains)
    return {
        "n": len(gains),
        "window": window,
        "continued_up_pct": round(float((arr > 0).mean()), 4),
        "mean_gain_pct": round(float(arr.mean()), 4),
        "median_gain_pct": round(float(np.median(arr)), 4),
    }


# ============================================================
# 5. 换手率与摩擦成本
# ============================================================

def turnover(fills: list[dict], capital_base: float | None = None) -> dict:
    """年化换手 = 总买入额 ÷ 平均资金规模 ×（365 / 样本天数）。

    ⚠ ``capital_base`` 缺省时退回交割单「资金余额」均值，那是**纯现金口径**，
    不含持仓市值，会把换手率显著高估。已知账户总资产/平均仓位时务必显式传入。
    返回里的 ``basis`` 标明本次用的是哪种分母，避免读者误读。
    """
    if not fills:
        return {"total_buy_amount": 0.0, "capital_base": 0.0, "span_days": 0,
                "turnover_annual": 0.0, "friction_cost_pct": 0.0, "n_fills": 0,
                "basis": "无数据"}
    total_buy = sum(float(f.get("amount") or 0) for f in fills if f["side"] == "buy")
    balances = [float(f["cash_balance"]) for f in fills
                if f.get("cash_balance") and float(f["cash_balance"]) > 0]
    if capital_base and float(capital_base) > 0:
        base = float(capital_base)
        basis = "显式传入（推荐）"
    else:
        base = _mean(balances)
        basis = "资金余额均值（仅现金，偏高估）"
    dates = [f["trade_date"] for f in fills if isinstance(f.get("trade_date"), date)]
    span = (max(dates) - min(dates)).days if len(dates) >= 2 else 0
    annual = 0.0
    if base > 0 and span > 0:
        annual = (total_buy / base) * (365.0 / span)
    return {
        "n_fills": len(fills),
        "total_buy_amount": round(total_buy, 2),
        "capital_base": round(base, 2),
        "span_days": span,
        "turnover_annual": round(annual, 2),
        "basis": basis,
        # 摩擦成本 = 年化换手 × 一次往返成本
        "friction_cost_pct": round(annual * ROUND_TRIP_COST, 4),
    }


# ============================================================
# 6. 仓位规模与盈亏单规模比
# ============================================================

def position_sizing(rounds: list[dict]) -> dict:
    """单笔投入（成本金额）分布 + 亏损单/盈利单的平均规模比。

    规模比 > 1 意味着**亏钱的仓位比赚钱的仓位更重**——这是资金分配层面的
    处置效应，比"持有天数"更能解释亏损来源。
    """
    costs = [float(r.get("cost") or 0) for r in rounds]
    win_cost = [float(r.get("cost") or 0) for r in rounds
                if float(r.get("pnl") or 0) > 0]
    loss_cost = [float(r.get("cost") or 0) for r in rounds
                 if float(r.get("pnl") or 0) <= 0]
    w = _mean(win_cost)
    l = _mean(loss_cost)
    return {
        "n": len(costs),
        "p25": round(_quantile(costs, 0.25), 2),
        "p50": round(_quantile(costs, 0.50), 2),
        "p75": round(_quantile(costs, 0.75), 2),
        "p90": round(_quantile(costs, 0.90), 2),
        "win_cost_mean": round(w, 2),
        "loss_cost_mean": round(l, 2),
        "loss_to_win_ratio": round(l / w, 2) if w > 0 else 0.0,
    }

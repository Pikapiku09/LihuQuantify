"""集中度压力测试：蒙特卡洛 + 真实「波」收益分布（不做策略回测）。

回答的问题
----------
11 万左右的账户、用户实际是「重仓少数票」的打法（122 波 / 81 只 / 2 年）。
单票上限 15%→20% 在两个口径下结论互相打架（CherryClaw −15.26%→−20.59%，
IR −10.84%→−5.46%），说明**单票上限本身不是决定变量**，真正的决定量是
「单票上限 × 同时持仓数 × 个股相关性」。

本脚本不碰策略、不碰行情接口，只从 `data/wave_entry_panel.parquet` 的
122 波**经验收益分布**里做有放回抽样，模拟「每周期同时持有 K 只、每只按
max_single 配资」的权益路径，给出 12 组（3 cap × 4 K）的期末权益与回撤分布，
并回答：在 11 万账户上，同时拿几只 + 单票多少，才能在 95% 分位把回撤压在 25%
（项目 MDD 硬上限）以内。

口径（写进输出，便于复核）
--------------------------
- 周期长度：样本平均持有天数（122 波均值）；周期数 = 样本跨度天数 / 周期长度
  （即同一「2 年」时间窗，K 只并行轮转）。
- 配资：K=1 → 单票满仓（100%）；K≥2 → 每只 max_single，若 K×cap > 100%
  则等比压缩到 1/K（**不加杠杆**），未投部分按现金 0 收益计。
- 回撤：**周期边界**上的权益曲线峰值回撤（只有波级收益，没有日内/波内路径，
  故真实回撤会被低估）。
- 抽样：有放回独立抽样（**未建模个股相关性**，真实市场同涨同跌 → 结果偏乐观）。

用法
----
    .venv\\Scripts\\python.exe scripts/concentration_stress.py [--n-sims 5000] [--seed 42]

输出：`outputs/concentration_stress.json`（唯一输出文件；不写库、不 git 操作）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

# ---- 路径（脚本不写除 outputs/concentration_stress.json 以外的任何文件）----
ROOT = Path(__file__).resolve().parents[1]
PANEL_PATH = ROOT / "data" / "wave_entry_panel.parquet"
OUT_PATH = ROOT / "outputs" / "concentration_stress.json"

# ---- 与项目风控口径对齐的常量 ----
INIT_EQUITY = 110_000.0          # 用户账户约 11 万
MDD_LIMIT = 0.25                 # 项目已设 MDD 硬上限 25%
GRID_CAP = (0.15, 0.20, 0.25)    # 单票上限（15% 为原值，20% 为调整后）
GRID_K = (1, 2, 3, 5)            # 同时持仓数
N_SIMS = 5000                    # 每组模拟次数（≥2000；默认 5000 让 p95 更稳）
SEED = 42                        # 固定随机种子（写进输出，可复现）
LOSS_30 = 0.30                   # 「爆仓式亏损」门槛：−30%
MIN_WAVES = 30                   # 经验分布少于该数量 → 判定不足以做蒙特卡洛
CORRELATION_ROBUST_MARGIN = 0.02  # 相关性稳健推荐要留的余量（2pp）：ρ=0.8 下
                                  # p95 回撤须 ≤23% 才算"稳"，避免擦线组合被噪声翻转


# ======================================================================
# 1. 数据
# ======================================================================

def load_panel(path: Path = PANEL_PATH) -> pd.DataFrame:
    """读波级面板。优先 pandas(parquet 引擎)，退化用 duckdb 内存库（不碰主库）。"""
    if not path.exists():
        raise FileNotFoundError(f"缺少数据文件：{path}")
    try:
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - 缺 pyarrow/fastparquet 时走 duckdb
        import duckdb

        con = duckdb.connect(":memory:")   # 内存库：绝不连接 data/lihu_quant.duckdb
        try:
            return con.execute(
                "SELECT * FROM read_parquet(?)", [str(path)]
            ).df()
        finally:
            con.close()


def empirical_stats(df: pd.DataFrame) -> dict:
    """经验分布概要（报告与 JSON 共用，避免两处口径分叉）。"""
    r = df["ret"].to_numpy(dtype=float)
    r = r[np.isfinite(r)]
    hold = df["hold_days"].to_numpy(dtype=float)
    buy = pd.to_datetime(df["buy_date"], errors="coerce")
    sell = pd.to_datetime(df["sell_date"], errors="coerce")
    span_days = int((sell.max() - buy.min()).days) if buy.notna().any() else 0
    return {
        "n_waves": int(r.size),
        "ret_mean": round(float(r.mean()), 6),
        "ret_median": round(float(np.median(r)), 6),
        "ret_std": round(float(r.std(ddof=1)), 6),
        "ret_min": round(float(r.min()), 6),
        "ret_max": round(float(r.max()), 6),
        "win_rate": round(float((r > 0).mean()), 4),
        "hold_days_mean": round(float(np.nanmean(hold)), 3),
        "hold_days_median": round(float(np.nanmedian(hold)), 3),
        "span_days": span_days,
        "span_start": str(buy.min().date()) if buy.notna().any() else None,
        "span_end": str(sell.max().date()) if sell.notna().any() else None,
    }


# ======================================================================
# 2. 模拟核心
# ======================================================================

def weights_for(cap: float, k: int) -> tuple[np.ndarray, float]:
    """单票权重向量 + 总投入比例（不加杠杆）。

    K=1 → 单票满仓（100%）；K≥2 → 每只 cap，若 K×cap > 100% 等比压缩到 1/K。
    """
    if k <= 0:
        raise ValueError("K 必须 ≥ 1")
    if k == 1:
        return np.array([1.0], dtype=float), 1.0
    if cap * k <= 1.0 + 1e-12:
        return np.full(k, float(cap)), round(cap * k, 10)
    return np.full(k, 1.0 / k), 1.0


def simulate(rets: np.ndarray, weights: np.ndarray, cycles: int, n_sims: int,
             rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """一次蒙特卡洛（独立抽样）：返回 (期末净值倍数, 周期级最大回撤)。

    每个周期：抽 K 只（有放回、独立）→ 组合收益 = Σ w_i·ret_i → 权益累乘。
    回撤 = 权益曲线（含起点）峰值回撤的最大值。
    """
    k = int(weights.size)
    draws = rng.choice(rets, size=(n_sims, cycles, k), replace=True)
    port = draws @ weights                          # (n_sims, cycles)
    return _path_metrics(port)


def simulate_correlated(rets: np.ndarray, weights: np.ndarray, cycles: int,
                        n_sims: int, rng: np.random.Generator,
                        rho: float) -> tuple[np.ndarray, np.ndarray]:
    """一次性补充敏感性：高斯 Copula 等相关系数 ρ 的抽样（边际 = 经验分布）。

    r_i = F^-1(Φ(z_i))，z_i = √ρ·Z + √(1-ρ)·e_i（K 只共享一个公共冲击 Z）。
    边际分布与主口径完全一致，只改变**同一周期内**个股间的同涨同跌程度；
    ρ=0 时退化为独立抽样（仅蒙特卡洛噪声差异）。
    """
    k = int(weights.size)
    if not 0.0 <= rho < 1.0:
        raise ValueError("rho 必须在 [0,1) 内")
    if k == 1 or rho == 0.0:
        return simulate(rets, weights, cycles, n_sims, rng)
    common = rng.standard_normal((n_sims, cycles, 1))
    idio = rng.standard_normal((n_sims, cycles, k))
    z = np.sqrt(rho) * common + np.sqrt(1.0 - rho) * idio
    try:                                     # scipy 已在项目依赖内
        from scipy.special import erf as _erf

        cdf = 0.5 * (1.0 + _erf(z / np.sqrt(2.0)))
    except ImportError:                      # 兜底：math.erf 向量化（慢但正确）
        from math import erf, sqrt

        cdf = 0.5 * (1.0 + np.vectorize(erf)(z / sqrt(2.0)))
    u = np.clip(cdf, 1e-6, 1.0 - 1e-6)
    # numpy 的 quantile 只收标量/1 维 q → 展平再还原形状
    draws = np.quantile(rets, u.ravel()).reshape(u.shape)
    port = draws @ weights
    return _path_metrics(port)


def _path_metrics(port: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """组合周期收益矩阵 (n_sims, cycles) → (期末净值, 周期级最大回撤)。"""
    n_sims, cycles = port.shape
    eq = np.empty((n_sims, cycles + 1), dtype=float)
    eq[:, 0] = 1.0
    eq[:, 1:] = np.cumprod(1.0 + port, axis=1)
    terminal = eq[:, -1]
    peak = np.maximum.accumulate(eq, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak, 0.0)
    mdd = dd[:, 1:].max(axis=1) if cycles > 0 else np.zeros(n_sims)
    return terminal, mdd


def group_rng(seed: int, cycles: int, n_sims: int, weights: np.ndarray,
              salt: int = 0) -> np.random.Generator:
    """按「权重向量」派生随机种子。

    同一组合（如 cap=20%/K=5 与 cap=25%/K=5 归一化后都是 1/5 权重）必须得到
    完全相同的模拟结果，避免把同一组合当成两个不同结论；salt 用于敏感性分支。
    """
    key = [int(seed), int(salt), int(cycles), int(n_sims)]
    key += [int(round(float(w) * 1_000_000)) for w in weights]
    return np.random.default_rng(key)


def _q(x: np.ndarray, q: float) -> float:
    return float(np.percentile(x, q))


def summarize_group(terminal: np.ndarray, mdd: np.ndarray, init_equity: float) -> dict:
    """单组指标：期末权益分位/胜率/回撤分位/爆仓概率/是否满足 MDD 95% 分位 ≤25%。"""
    eq = terminal * init_equity
    mdd_p50, mdd_p95 = _q(mdd, 50), _q(mdd, 95)
    return {
        "equity_p5": round(_q(eq, 5), 2),
        "equity_p25": round(_q(eq, 25), 2),
        "equity_p50": round(_q(eq, 50), 2),
        "equity_p75": round(_q(eq, 75), 2),
        "equity_p95": round(_q(eq, 95), 2),
        "total_return_p5": round(_q(terminal - 1.0, 5), 6),
        "total_return_p50": round(_q(terminal - 1.0, 50), 6),
        "total_return_p95": round(_q(terminal - 1.0, 95), 6),
        "win_rate": round(float((terminal > 1.0).mean()), 4),
        "mdd_p50": round(mdd_p50, 6),
        "mdd_p95": round(mdd_p95, 6),
        "mdd_mean": round(float(mdd.mean()), 6),
        "mdd_worst": round(float(mdd.max()), 6),
        # 爆仓式亏损：期末权益较初始亏 ≥30%（主口径）
        "p_loss_ge_30_terminal": round(float((terminal <= 1.0 - LOSS_30).mean()), 6),
        # 过程口径：期间最大回撤 ≥30% 的发生概率（参考）
        "p_mdd_ge_30": round(float((mdd >= LOSS_30).mean()), 6),
        "pass_mdd_p95_le_25": bool(mdd_p95 <= MDD_LIMIT),
    }


# ======================================================================
# 3. 结论提取（全部由本次模拟结果计算，不写死任何数字）
# ======================================================================

def key_findings(rows: list[dict], sensitivity: list[dict] | None = None) -> dict:
    """把 Lead 要的四问算出来：通过组合 / K=1 vs K=3 / cap 15→20 / 推荐。

    另附相关性敏感性（ρ=0.5/0.8）作为"独立抽样偏乐观"的量化修正。
    """
    by_key = {(r["max_single"], r["k"]): r for r in rows}
    passing = [r for r in rows if r["pass_mdd_p95_le_25"]]

    # K=1 与 K=3（同 cap=0.20 对照）：回撤与收益的差别
    ref_cap = 0.20
    k1, k3 = by_key.get((ref_cap, 1)), by_key.get((ref_cap, 3))
    k1_vs_k3 = None
    if k1 and k3:
        k1_vs_k3 = {
            "cap": ref_cap,
            "k1": {"mdd_p95": k1["mdd_p95"], "mdd_p50": k1["mdd_p50"],
                   "equity_p50": k1["equity_p50"], "win_rate": k1["win_rate"],
                   "invested_pct": k1["invested_pct"],
                   "p_loss_ge_30_terminal": k1["p_loss_ge_30_terminal"]},
            "k3": {"mdd_p95": k3["mdd_p95"], "mdd_p50": k3["mdd_p50"],
                   "equity_p50": k3["equity_p50"], "win_rate": k3["win_rate"],
                   "invested_pct": k3["invested_pct"],
                   "p_loss_ge_30_terminal": k3["p_loss_ge_30_terminal"]},
            "mdd_p95_delta_pp": round((k3["mdd_p95"] - k1["mdd_p95"]) * 100, 2),
            "equity_p50_delta": round(k3["equity_p50"] - k1["equity_p50"], 2),
        }

    # cap 15% → 20%：同 K 下的回撤/收益变化（K=1 满仓口径下 cap 不生效 → 0）
    cap_sweep = {}
    for k in GRID_K:
        a, b, c = by_key.get((0.15, k)), by_key.get((0.20, k)), by_key.get((0.25, k))
        if a and b:
            item = {
                "mdd_p95_at_15": a["mdd_p95"],
                "mdd_p95_at_20": b["mdd_p95"],
                "mdd_p95_delta_pp": round((b["mdd_p95"] - a["mdd_p95"]) * 100, 2),
                "equity_p50_at_15": a["equity_p50"],
                "equity_p50_at_20": b["equity_p50"],
                "equity_p50_delta": round(b["equity_p50"] - a["equity_p50"], 2),
                "cap_effective": bool(k > 1),
                "note": ("K=1 为单票满仓口径，cap 不改变配资 → 无差异"
                         if k == 1 else "K≥2：cap 直接决定总投入 = cap×K"),
            }
            if c:
                item["mdd_p95_at_25"] = c["mdd_p95"]
                item["mdd_p95_delta_20_to_25_pp"] = round(
                    (c["mdd_p95"] - b["mdd_p95"]) * 100, 2)
                item["cap_saturated_at_25"] = bool(
                    abs(c["invested_pct"] - b["invested_pct"]) < 1e-9)
            cap_sweep[str(k)] = item

    # 推荐：在"满足 p95 回撤 ≤25%"的组合里，取 p50 期末权益最高者（收益优先），
    # 同分则取 p95 回撤更低者。若没有组合通过 → 无法推荐。
    recommended = None
    if passing:
        best = sorted(passing,
                      key=lambda r: (-r["equity_p50"], r["mdd_p95"], r["max_single"], r["k"]))[0]
        smallest = sorted(passing,
                          key=lambda r: (r["invested_pct"], r["k"], r["max_single"]))[0]
        recommended = {
            "max_single": best["max_single"],
            "k": best["k"],
            "invested_pct": best["invested_pct"],
            "equity_p50": best["equity_p50"],
            "mdd_p95": best["mdd_p95"],
            "win_rate": best["win_rate"],
            "rationale": ("在通过的组合中 p50 期末权益最高（收益优先），"
                          "且 p95 回撤仍 ≤25%"),
            "most_conservative_passing": {
                "max_single": smallest["max_single"], "k": smallest["k"],
                "invested_pct": smallest["invested_pct"],
                "mdd_p95": smallest["mdd_p95"], "equity_p50": smallest["equity_p50"],
            },
        }

    # 相关性敏感性：ρ=0.5/0.8 下哪些组合仍满足 p95 回撤 ≤25%（独立口径的保守修正）
    corr = {"note": ("同一组合同周期内个股等相关系数 ρ 下的 p95 回撤"
                     "（高斯 Copula，边际仍为 122 波经验分布）；"
                     "ρ=0 与主口径一致（仅蒙特卡洛噪声差异）"),
            "rho_0.5": [], "rho_0.8": []}
    robust = None
    if sensitivity:
        for s in sensitivity:
            if s["rho"] not in (0.5, 0.8):
                continue
            corr[f"rho_{s['rho']}"].append({
                "max_single": s["max_single"], "k": s["k"],
                "invested_pct": s["invested_pct"],
                "mdd_p95": s["mdd_p95"], "equity_p50": s["equity_p50"],
                "pass_mdd_p95_le_25": s["pass_mdd_p95_le_25"],
            })
        corr["n_passing_rho_0.5"] = sum(
            1 for s in sensitivity if s["rho"] == 0.5 and s["pass_mdd_p95_le_25"])
        corr["n_passing_rho_0.8"] = sum(
            1 for s in sensitivity if s["rho"] == 0.8 and s["pass_mdd_p95_le_25"])
        # 相关性稳健推荐：ρ=0.8（真实同涨同跌的保守上界）下 p95 回撤须 ≤上限−2pp
        # 才算"稳"（擦线 24.9% 的组合会被蒙特卡洛噪声/模型误差翻转），
        # 在此约束内取 p50 收益最高者。
        cands = [s for s in sensitivity
                 if s["rho"] == 0.8
                 and s["mdd_p95"] <= MDD_LIMIT - CORRELATION_ROBUST_MARGIN]
        corr["robust_margin_pp"] = round(CORRELATION_ROBUST_MARGIN * 100, 2)
        corr["robust_rule"] = (f"ρ=0.8 下 p95 回撤 ≤ "
                               f"{(MDD_LIMIT - CORRELATION_ROBUST_MARGIN):.0%}，"
                               "再取 p50 收益最高者")
        if cands:
            best_r = max(cands, key=lambda s: (s["equity_p50"], -s["mdd_p95"]))
            row = by_key.get((best_r["max_single"], best_r["k"])) or {}
            robust = {
                "max_single": best_r["max_single"],
                "k": best_r["k"],
                "invested_pct": best_r["invested_pct"],
                "mdd_p95_rho_0": row.get("mdd_p95"),
                "mdd_p95_rho_0.5": next(
                    (x["mdd_p95"] for x in sensitivity
                     if x["rho"] == 0.5 and x["max_single"] == best_r["max_single"]
                     and x["k"] == best_r["k"]), None),
                "mdd_p95_rho_0.8": best_r["mdd_p95"],
                "equity_p50": row.get("equity_p50"),
                "win_rate": row.get("win_rate"),
                "rationale": ("若把'个股同涨同跌'计入（ρ=0.8）仍要守 25%，"
                              "此组合是 p50 收益最高的通过项；"
                              "它在独立口径下的 p95 回撤也远低于上限"),
            }
        if recommended:
            for rho in (0.5, 0.8):
                hit = next((s for s in sensitivity
                            if s["rho"] == rho
                            and s["max_single"] == recommended["max_single"]
                            and s["k"] == recommended["k"]), None)
                if hit:
                    recommended.setdefault("under_correlation", {})[
                        f"rho_{rho}"] = {
                        "mdd_p95": hit["mdd_p95"],
                        "equity_p50": hit["equity_p50"],
                        "still_pass": hit["pass_mdd_p95_le_25"],
                    }

    return {
        "mdd_limit": MDD_LIMIT,
        "passing_groups": [
            {"max_single": r["max_single"], "k": r["k"],
             "invested_pct": r["invested_pct"],
             "mdd_p95": r["mdd_p95"], "equity_p50": r["equity_p50"],
             "win_rate": r["win_rate"],
             "p_loss_ge_30_terminal": r["p_loss_ge_30_terminal"]}
            for r in passing
        ],
        "n_passing": len(passing),
        "k1_vs_k3": k1_vs_k3,
        "cap_15_to_20": cap_sweep,
        "recommended": recommended,
        "correlation_robust_recommendation": robust,
        "correlation_sensitivity": corr,
    }


# ======================================================================
# 4. 主流程
# ======================================================================

def build_report(n_sims: int = N_SIMS, seed: int = SEED,
                 init_equity: float = INIT_EQUITY) -> dict:
    """跑完整网格并返回可 JSON 化的结果（不落盘）。"""
    panel = load_panel()
    if "ret" not in panel.columns:
        raise ValueError("面板缺少 ret 列，无法计算")
    panel = panel.copy()
    panel["ret"] = pd.to_numeric(panel["ret"], errors="coerce")
    panel = panel[np.isfinite(panel["ret"])]
    stats = empirical_stats(panel)
    if stats["n_waves"] < MIN_WAVES:
        raise ValueError(
            f"有效波数 {stats['n_waves']} < {MIN_WAVES}，经验分布不足以做蒙特卡洛（无法计算）")
    rets = panel["ret"].to_numpy(dtype=float)
    # 实测锚点：122 波合计 pnl 与「相对初始权益」的累计收益（用于对照模型分位，
    # 避免把模型 p50 当成真实可实现年化；cost 为波内累计买入额，不能当持仓规模）
    if "pnl" in panel.columns:
        _pnl = pd.to_numeric(panel["pnl"], errors="coerce").fillna(0.0).sum()
        stats["panel_pnl_sum"] = round(float(_pnl), 2)
        stats["panel_pnl_on_init_equity"] = round(float(_pnl) / init_equity, 6)
    if "cost" in panel.columns:
        _cost_max = float(pd.to_numeric(panel["cost"], errors="coerce").max())
        stats["cost_max"] = round(_cost_max, 2)
        stats["cost_note"] = (
            f"cost 为波内累计买入额（含多次加仓，最大 {_cost_max / 10000:.1f} 万"
            f" > {init_equity / 10000:.0f} 万账户），不能当单票持仓规模；"
            "本模型不使用 cost 定权重")

    # 周期数：同一 2 年时间窗内，按平均持有天数轮转（K 只并行 → 每周期同时结算）
    mean_hold = max(float(stats["hold_days_mean"]), 1.0)
    cycles = max(1, int(round(stats["span_days"] / mean_hold)))

    rows: list[dict] = []
    equivalent: dict[tuple, dict] = {}
    for cap in GRID_CAP:
        for k in GRID_K:
            weights, invested = weights_for(cap, k)
            # 同一权重向量 → 同一随机种子（同一组合不得出现两个不同结论）
            rng = group_rng(seed, cycles, n_sims, weights)
            terminal, mdd = simulate(rets, weights, cycles, n_sims, rng)
            row = {
                "max_single": cap,
                "k": k,
                "weights": [round(float(w), 6) for w in weights],
                "invested_pct": round(invested, 6),
                "cash_pct": round(1.0 - invested, 6),
                "cycles": cycles,
                "n_sims": n_sims,
                "seed": int(seed),
            }
            row.update(summarize_group(terminal, mdd, init_equity))
            wkey = tuple(row["weights"])
            if wkey in equivalent:
                row["equivalent_to"] = equivalent[wkey]
            else:
                equivalent[wkey] = {"max_single": cap, "k": k}
            rows.append(row)

    # ---- 相关性敏感性（补充）：ρ=0 / 0.5 / 0.8 下同一网格的 p95 回撤 ----
    sensitivity: list[dict] = []
    for rho in (0.0, 0.5, 0.8):
        for cap in GRID_CAP:
            for k in GRID_K:
                weights, invested = weights_for(cap, k)
                rng = group_rng(seed, cycles, n_sims, weights,
                                salt=7 + int(round(rho * 1000)))
                terminal, mdd = simulate_correlated(
                    rets, weights, cycles, n_sims, rng, rho)
                sensitivity.append({
                    "rho": rho, "max_single": cap, "k": k,
                    "invested_pct": round(invested, 6),
                    "mdd_p95": round(_q(mdd, 95), 6),
                    "equity_p50": round(_q(terminal - 1.0, 50), 6),
                    "pass_mdd_p95_le_25": bool(_q(mdd, 95) <= MDD_LIMIT),
                    "n_sims": n_sims,
                })

    findings = key_findings(rows, sensitivity)
    return {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "question": ("11 万账户、重仓少数票：同时拿几只 + 单票多少，"
                     f"才能把 95% 分位回撤压在 {MDD_LIMIT:.0%} 以内？"),
        "source": {
            "panel": str(PANEL_PATH.relative_to(ROOT)).replace("\\", "/"),
            "note": "波级面板（122 波/81 只/2 年）；只读 parquet，未连接 DuckDB 主库、未调用 Tushare",
        },
        "assumptions": {
            "init_equity": init_equity,
            "mdd_limit": MDD_LIMIT,
            "loss_30_threshold": LOSS_30,
            "n_sims": n_sims,
            "seed": seed,
            "grid": {"max_single": list(GRID_CAP), "k": list(GRID_K)},
            "cycles": cycles,
            "cycles_rule": ("周期长度=样本平均持有天数；周期数=样本跨度天数/周期长度"
                            "（同一 2 年时间窗，K 只并行轮转）"),
            "weight_rule": ("K=1 → 单票满仓(100%)；K≥2 → 每只 max_single，"
                            "K×max_single>100% 时等比压缩到 1/K；未投部分按现金 0 收益"),
            "drawdown_rule": "周期边界权益曲线（含起点）峰值回撤的最大值；未建模波内路径",
            "sampling": "122 波经验分布有放回独立抽样；未建模个股相关性",
            "seed_rule": ("每组随机种子由 [seed, salt, cycles, n_sims, 权重向量] 派生："
                          "同一组合（如 K=5 的 cap=20% 与 cap=25% 归一化后同为 1/5）"
                          "得到完全相同的模拟结果，避免同一组合给出两个结论"),
            "leveraged": False,
        },
        "empirical_distribution": stats,
        "grid_results": rows,
        "key_findings": findings,
        "sensitivity_correlation": sensitivity,
        "limitations": [
            "样本小：经验分布来自用户自己 122 波（2024-10~2026-11），单一市场环境，"
            "且是已平仓波（止损/离场截断了左尾），真实极端亏损可能被低估。",
            "未建模个股相关性：默认独立抽样，真实市场同涨同跌，分散效果被高估 → "
            "回撤结果偏乐观；K 越大，这一偏差越明显。",
            "回撤按周期边界计算：手里只有波级收益（ret），没有波内路径，"
            "周期内最大回撤会被低估（真实回撤应更大）。",
            (f"模型 p50 期末权益区间约 "
             f"{min(r['equity_p50'] for r in rows) / init_equity - 1:+.0%}"
             f"~{max(r['equity_p50'] for r in rows) / init_equity - 1:+.0%}"
             f"（其中满仓组合约 "
             f"{min(r['equity_p50'] for r in rows if r['invested_pct'] >= 0.999) / init_equity - 1:+.0%}"
             f"~{max(r['equity_p50'] for r in rows if r['invested_pct'] >= 0.999) / init_equity - 1:+.0%}），"
             f"而账户实测 2 年累计仅 {stats.get('panel_pnl_on_init_equity', 0):+.1%}"
             f"（122 波 pnl 合计 {stats.get('panel_pnl_sum', 0):,.0f} 元）：模型假设资金"
             "无空窗地按 cap×K 持续投入，真实有等待信号/现金闲置/分批建仓 → "
             "只应看**组间相对排序**，不要把分位数值当可实现收益。"),
            "ret=pnl/cost 为已实现净额口径（含买卖费用，未单独复算税/滑点差异）；"
            "未叠加未来税费或极端流动性冲击。",
            "K=1 采用『单票满仓』口径（Lead 指定）：此口径下 cap 不生效，"
            "故 K=1 的三行结果相同，cap 只在 K≥2 时有区分度。",
            "K=5 时 cap=20% 与 cap=25% 归一化后是同一组合（5×20%=100%，"
            "5×25%>100% 压缩到 1/K=20%），两行结果完全相同（已标 equivalent_to）。",
            "相关性敏感性（sensitivity_correlation）只是把同一个经验分布换成"
            "等相关系数 ρ 的 Copula 抽样，属于补充口径；主结论仍以独立抽样为准，"
            "两者不要混用。",
            "蒙特卡洛只重排收益顺序/组合，不改变经验分布本身；若用户的策略在"
            "不同集中度下会改变选股，本模拟不覆盖该反馈。",
        ],
        "reproduce": {
            "command": (".venv\\Scripts\\python.exe scripts/concentration_stress.py "
                        f"--n-sims {n_sims} --seed {seed}"),
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="集中度压力测试（蒙特卡洛，波级经验分布）")
    ap.add_argument("--n-sims", type=int, default=N_SIMS, help="每组模拟次数（默认 5000）")
    ap.add_argument("--seed", type=int, default=SEED, help="随机种子（默认 42）")
    ap.add_argument("--init-equity", type=float, default=INIT_EQUITY,
                    help="初始权益（默认 110000）")
    ap.add_argument("--out", type=str, default=str(OUT_PATH), help="输出 JSON 路径")
    args = ap.parse_args(argv)

    if args.n_sims < 2000:
        print(f"[警告] n_sims={args.n_sims} < 2000，p95 分位估计不稳（任务要求 ≥2000）")

    try:
        report = build_report(n_sims=args.n_sims, seed=args.seed,
                              init_equity=args.init_equity)
    except (FileNotFoundError, ValueError, KeyError) as e:
        # 数据缺失/不可用 → 如实输出「无法计算」+ 缺什么，不编造数字
        payload = {
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "无法计算",
            "reason": str(e),
            "missing": [] if PANEL_PATH.exists() else [str(PANEL_PATH)],
            "assumptions": {"init_equity": args.init_equity, "mdd_limit": MDD_LIMIT},
        }
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[无法计算] {e}")
        print(f"已写 {out}")
        return 2

    report["status"] = "ok"
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # 控制台摘要（便于直接核对，不替代 JSON）
    print(f"[经验分布] {report['empirical_distribution']}")
    print(f"[模型] 周期数={report['assumptions']['cycles']}，模拟次数={args.n_sims}，种子={args.seed}")
    print(f"{'cap':>5} {'K':>2} {'投入':>6} {'p50权益':>12} {'p95回撤':>8} "
          f"{'胜率':>6} {'亏30%概率':>9} {'通过':>4}")
    for r in report["grid_results"]:
        print(f"{r['max_single']:>5.2f} {r['k']:>2} {r['invested_pct']:>6.0%} "
              f"{r['equity_p50']:>12,.0f} {r['mdd_p95']:>7.2%} "
              f"{r['win_rate']:>6.1%} {r['p_loss_ge_30_terminal']:>9.2%} "
              f"{'✅' if r['pass_mdd_p95_le_25'] else '❌':>4}")
    kf = report["key_findings"]
    print(f"[通过 {kf['n_passing']}/12] " +
          ", ".join(f"cap={g['max_single']:.0%}/K={g['k']}" for g in kf["passing_groups"]))
    if kf["recommended"]:
        rec = kf["recommended"]
        print(f"[推荐] cap={rec['max_single']:.0%}, K={rec['k']}（投入 {rec['invested_pct']:.0%}，"
              f"p50 权益 {rec['equity_p50']:,.0f}，p95 回撤 {rec['mdd_p95']:.2%}）")
        for rho, hit in (rec.get("under_correlation") or {}).items():
            print(f"  └ 相关性敏感性 ρ={rho.split('_')[-1]}: p95 回撤 {hit['mdd_p95']:.2%}"
                  f"，仍通过={hit['still_pass']}")
    corr = kf["correlation_sensitivity"]
    print(f"[相关性敏感性] ρ=0.5 通过 {corr.get('n_passing_rho_0.5', '-')}/12，"
          f"ρ=0.8 通过 {corr.get('n_passing_rho_0.8', '-')}/12")
    rb = kf.get("correlation_robust_recommendation")
    if rb:
        print(f"[相关性稳健推荐] cap={rb['max_single']:.0%}, K={rb['k']}（投入 "
              f"{rb['invested_pct']:.0%}；p95 回撤：独立 {rb['mdd_p95_rho_0']:.2%} / "
              f"ρ=0.8 {rb['mdd_p95_rho_0.8']:.2%}；p50 权益 {rb['equity_p50']:,.0f}）")
    print(f"已写 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

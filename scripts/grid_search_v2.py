"""修复I（网格扩边+稳健性报告）+ 修复J（分层抽样换池）。

统计纪律声明（docs/决策日志.md）：
    训练段/验证段/holdout 三段均已消费。本轮实验性质是【稳健性诊断】——
    验证原结论 (ma5=0.05, fresh=3, chase=0.08) 是否为：
      ① 网格边界伪影（第一轮最优 0.05 落在网格边界）
      ② 池子偏差（head-50 深市老牌股权重）
    不据此挑选新参数（诊断性使用，不是调参）。若需要重新选参，
    必须使用新数据段（2023 年前历史或未来 live 积累）。

网格（修复I 扩边）：
    ma5_dev    ∈ {0.03, 0.05, 0.08, 0.10}   （第一轮 {0.005,0.015,0.03,0.05} 边界外扩）
    freshness  ∈ {2, 3, 5}
    chasing    ∈ {0.06, 0.08, 0.10}
    共 36 组

股票池（修复J）：
    --pool old   ：head-50（第一轮训练池，对照）
    --pool strat ：成交额分层抽样 N 只（默认 200；主板/剔ST/上市≥60日/日均额≥1亿；
                   按近20日日均成交额分5层等比抽样，固定 seed 可复现）

输出：
    outputs/grid_v2_<pool>.csv            36 组明细
    outputs/grid_v2_<pool>_robustness.md  稳健区域+平坦度报告
    控制台：原参数稳健性判定 + 新旧池结论对比

用法：
    python scripts/grid_search_v2.py --pool strat --n 200
    python scripts/grid_search_v2.py --pool old
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine
from lihu_quantify.risk.checklist import ChecklistGate

# ===== 修复I 扩边网格 =====
GRID_MA5_DEV = [0.03, 0.05, 0.08, 0.10]
GRID_FRESHNESS = [2, 3, 5]
GRID_CHASING = [0.06, 0.08, 0.10]

# 原参数（第一轮网格结论，本轮验证其稳健性）
ORIG_PARAMS = (0.05, 3, 0.08)

# 训练段（与第一轮网格一致，仅做稳健性对比）
TRAIN_START = date(2024, 7, 26)
TRAIN_END = date(2025, 12, 12)


# ============================================================
# 修复J：分层抽样股票池
# ============================================================

def build_stratified_pool(
    client: TushareClient,
    store: DuckDBStore,
    target_n: int = 200,
    layers: int = 5,
    seed: int = 42,
    universe_cfg=None,
) -> list[str]:
    """按成交额分层抽样构建股票池。

    高效取数：daily 接口支持 trade_date 参数（一次返回全市场当日行情），
    拉最近 20 个交易日 → 20 次 API 而非逐股 2000+ 次。

    过滤：主板 / 剔ST / 上市≥60日 / 近20日日均成交额≥1亿
    分层：按日均成交额排序分 N 层，每层等比抽样（保证大中小盘代表性）
    """
    rng = random.Random(seed)   # 固定种子，可复现

    # 1. 基础过滤（stock_basic）
    basic = client.query("stock_basic", {"list_status": "L"})
    if basic.empty:
        return []
    store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    dfb = basic.copy()
    mask = (
        ~dfb["ts_code"].str.startswith("688")
        & ~dfb["ts_code"].str.startswith("300")
        & ~dfb["ts_code"].str.startswith("301")
    )
    if "name" in dfb.columns:
        mask &= ~dfb["name"].str.contains("ST", na=False)
    min_list_days = getattr(universe_cfg, "min_list_days", 60) if universe_cfg else 60
    if "list_date" in dfb.columns:
        list_dates = pd.to_datetime(dfb["list_date"], errors="coerce").dt.date
        cutoff = date.today() - timedelta(days=min_list_days)
        mask &= list_dates.apply(lambda d: bool(d) and d <= cutoff)
    dfb = dfb[mask]
    candidates = set(dfb["ts_code"])
    logger.info(f"[分层池] 基础过滤后候选 {len(candidates)} 只")

    # 2. 近 20 个交易日全市场成交额（trade_date 批量取）
    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    latest = store.get_latest_trade_date()
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    recent_dates = sorted(idx_df[idx_df["trade_date"] <= latest]["trade_date"])[-20:]

    amount_sum: dict[str, float] = {}
    days_counted = 0
    for td in reversed(recent_dates):
        try:
            day = client.query("daily", {"trade_date": td.strftime("%Y%m%d")}, use_cache=True)
        except Exception as e:
            logger.warning(f"[分层池] {td} 行情拉取失败: {e}")
            continue
        if day.empty:
            continue
        days_counted += 1
        for _, row in day.iterrows():
            code = row["ts_code"]
            if code in candidates:
                amount_sum[code] = amount_sum.get(code, 0.0) + float(row["amount"])
    if days_counted == 0:
        logger.error("[分层池] 无行情数据")
        return []
    avg_amount = {c: s / days_counted for c, s in amount_sum.items()}
    # amount 单位千元；阈值 1 亿元 = 1e5 千元
    min_amt = 1e5
    liquid = sorted(
        [(c, a) for c, a in avg_amount.items() if a >= min_amt],
        key=lambda x: x[1],
    )
    logger.info(f"[分层池] 流动性过滤（日均额≥1亿）后 {len(liquid)} 只（{days_counted} 日均值）")

    # 3. 分层等比抽样
    n = len(liquid)
    if n <= target_n:
        return [c for c, _ in liquid]
    per_layer = target_n // layers
    picked: list[str] = []
    layer_size = n / layers
    for i in range(layers):
        lo, hi = int(i * layer_size), int((i + 1) * layer_size)
        layer_codes = [c for c, _ in liquid[lo:hi]]
        take = min(per_layer, len(layer_codes))
        picked.extend(rng.sample(layer_codes, take))
        logger.debug(f"[分层池] 层{i+1}（成交额 {liquid[lo][1]/1e4:.1f}万~{liquid[min(hi,n-1)][1]/1e4:.1f}万千元）"
                     f"抽 {take}/{len(layer_codes)}")
    # 不足则从剩余补齐
    if len(picked) < target_n:
        rest = [c for c, _ in liquid if c not in set(picked)]
        picked.extend(rng.sample(rest, min(target_n - len(picked), len(rest))))
    picked = sorted(set(picked))
    logger.info(f"[分层池] 最终 {len(picked)} 只（seed={seed}，{layers} 层等比）")
    return picked


def fetch_pool_data(client: TushareClient, store: DuckDBStore, codes: list[str],
                    start: date, end: date) -> dict:
    """拉取池内全部股票日线。"""
    data = {}
    for i, code in enumerate(codes):
        try:
            df = client.query("daily", {
                "ts_code": code,
                "start_date": (start - timedelta(days=90)).strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            })
        except Exception as e:
            logger.warning(f"{code} 拉取失败: {e}")
            continue
        if df.empty or len(df) < 60:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        data[code] = df.sort_values("trade_date").reset_index(drop=True)
        if (i + 1) % 50 == 0:
            logger.info(f"[取数] {i+1}/{len(codes)}")
    logger.info(f"[取数] 完成：{len(data)}/{len(codes)} 只有有效数据")
    return data


# ============================================================
# 修复I：稳健区域分析 + 参数平坦度
# ============================================================

def robustness_analysis(grid_df: pd.DataFrame, metric: str = "total_return") -> dict:
    """稳健区域分析。

    稳健点定义：该参数点及其全部一阶邻域（每维±1档）的 metric 均为正。
    平坦度：最优点的 metric 与其邻域均值之差（越小越平坦=越稳健）。
    """
    ma5_vals = sorted(grid_df["ma5_dev"].unique())
    fresh_vals = sorted(grid_df["freshness"].unique())
    chase_vals = sorted(grid_df["chasing"].unique())

    lookup = {
        (row.ma5_dev, row.freshness, row.chasing): getattr(row, metric)
        for row in grid_df.itertuples()
    }

    def neighbors(m, f, c):
        """一阶邻域参数组合（含自身）。"""
        result = []
        for dm in (-1, 0, 1):
            for df_ in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dm == df_ == dc == 0:
                        continue
                    mi = ma5_vals.index(m) + dm
                    fi = fresh_vals.index(f) + df_
                    ci = chase_vals.index(c) + dc
                    if 0 <= mi < len(ma5_vals) and 0 <= fi < len(fresh_vals) and 0 <= ci < len(chase_vals):
                        result.append((ma5_vals[mi], fresh_vals[fi], chase_vals[ci]))
        return result

    robust_points = []
    flatness = {}
    for (m, f, c), val in lookup.items():
        nb = neighbors(m, f, c)
        nb_vals = [lookup[k] for k in nb if k in lookup]
        all_pos = val > 0 and all(v > 0 for v in nb_vals)
        if all_pos:
            robust_points.append((m, f, c, val))
        if nb_vals:
            flatness[(m, f, c)] = val - sum(nb_vals) / len(nb_vals)

    # 最优点平坦度
    best_key = max(lookup, key=lookup.get)
    best_flat = flatness.get(best_key, 0.0)
    # 原参数是否稳健
    orig_in = ORIG_PARAMS in lookup
    orig_val = lookup.get(ORIG_PARAMS)
    orig_nb = neighbors(*ORIG_PARAMS) if orig_in else []
    orig_nb_vals = [lookup[k] for k in orig_nb if k in lookup]
    orig_robust = (
        orig_in and orig_val > 0
        and all(v > 0 for v in orig_nb_vals)
    )
    return {
        "robust_points": robust_points,
        "n_robust": len(robust_points),
        "n_total": len(lookup),
        "best_key": best_key,
        "best_val": lookup[best_key],
        "best_flatness": best_flat,
        "orig_key": ORIG_PARAMS,
        "orig_val": orig_val,
        "orig_nb_vals": orig_nb_vals,
        "orig_robust": orig_robust,
        "lookup": lookup,
    }


def build_sector_map(client: TushareClient, store: DuckDBStore) -> dict[str, str]:
    """修复E(第三轮)：从 stock_basic 构建 {ts_code: industry} 板块映射。

    industry 空/NaN → "未分类"（不因未知放行——空板块在 Checklist._check_sector
    中不参与累计，但"未分类"会参与，确保训练口径与实盘一致）。
    """
    basic = client.query("stock_basic", {"list_status": "L"})
    if basic.empty:
        return {}
    store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    sector_map: dict[str, str] = {}
    if "industry" in basic.columns:
        for _, row in basic.iterrows():
            ind = row.get("industry")
            ind = str(ind).strip() if pd.notna(ind) and str(ind).strip() else "未分类"
            sector_map[row["ts_code"]] = ind
    logger.info(f"[板块映射] 构建完成：{len(sector_map)} 只（修复E 接线）")
    return sector_map


def run_one(data: dict, settings, ma5_dev: float, freshness: int, chasing: float,
            start: date, end: date,
            sector_by_code: dict[str, str] | None = None) -> dict:
    """跑一组参数（reduce 市场模式，与现行配置一致）。

    修复E(第三轮)：sector_by_code 传入 engine.run()，板块 40% 铁律在训练中生效。
    修复A(第三轮)：Portfolio.apply_fill 已接线连亏3笔停手（无需此处改动）。
    """
    s, r, b = settings.strategy, settings.risk, settings.backtest
    strategy = CherryClaw(
        ma_periods=tuple(s.ma_periods),
        golden_cross_max_freshness=freshness,
        volume_ratio_threshold=s.volume_ratio_threshold,
        entity_ratio_threshold=s.entity_ratio_threshold,
        close_to_ma5_max_dev=ma5_dev,
        max_position_pct=r.max_single_position,
        stop_loss_force_pct=r.stop_loss_force,
    )
    broker = SimulatedBroker(commission_rate=b.commission, stamp_tax_rate=b.stamp_tax, slippage=b.slippage)
    gate = ChecklistGate(chasing_high_threshold=chasing)
    # 市场参考信号（reduce 模式）：用数据段内指数分类
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker, checklist_gate=gate,
        max_single=r.max_single_position,
        market_filter_on=s.market_filter, market_filter_mode=s.market_filter_mode,
    )
    result = engine.run(data, init_capital=settings.init_capital, start=start, end=end,
                        sector_by_code=sector_by_code)
    m = result.metrics
    return {
        "total_return": m["total_return"],
        "max_drawdown": m["max_drawdown"],
        "calmar": m["calmar"],
        "win_rate": m["win_rate"],
        "profit_loss_ratio": m["profit_loss_ratio"],
        "trades": m["total_trades"],
    }


def write_robustness_report(pool_label: str, grid_df: pd.DataFrame, ra: dict, out_dir: Path) -> Path:
    """稳健性报告 Markdown。"""
    pool_built_date = date.today()
    lines = [
        f"# 扩边网格稳健性报告 · {pool_label} 池（含连亏停手）",
        "",
        f"> 数据段：训练段 {TRAIN_START} ~ {TRAIN_END}（已消费段，仅做稳健性诊断，不据此选参）",
        f"> 网格：ma5_dev {GRID_MA5_DEV} × freshness {GRID_FRESHNESS} × chasing {GRID_CHASING}"
        f"（{ra['n_total']} 组）",
        f"> 口径：含连亏3笔停手30天（铁律F，修复A第三轮已接线）+ 板块≤40%（修复E第三轮已接线）",
        "",
        "> ⚠️ **幸存者偏差声明（修复B，第三轮）**：本池按 "
        f"**{pool_built_date}** 的 stock_basic(list_status='L') 与最近 20 日成交额构建，",
        "> 用于回测历史区间时，期间退市/被 ST/失去流动性的股票不在池内，",
        "> **历史收益被系统性高估**。本报告全部收益数字为乐观口径，",
        "> 仅用于横向比较参数组合的相对稳健性，不代表真实可实现收益。",
        "",
        "## 一、总览",
        "",
        f"- 稳健点数（自身+全部一阶邻域均正）：**{ra['n_robust']}/{ra['n_total']}**"
        f"（{ra['n_robust']/max(1,ra['n_total']):.0%}）",
        f"- 最优组合：ma5={ra['best_key'][0]}, fresh={ra['best_key'][1]}, chase={ra['best_key'][2]}"
        f"（收益 {ra['best_val']:.2%}，乐观口径）",
        f"- 最优点平坦度（收益-邻域均值）：{ra['best_flatness']:+.2%}"
        f"（越接近 0 越平坦）",
        "",
        "## 二、原参数稳健性判定（核心结论）",
        "",
        f"- 原参数：ma5_dev={ORIG_PARAMS[0]}, freshness={ORIG_PARAMS[1]}, chasing={ORIG_PARAMS[2]}",
        f"- 原参数收益：**{ra['orig_val']:.2%}**（乐观口径）" if ra['orig_val'] is not None else "- 原参数不在网格内",
    ]
    if ra["orig_nb_vals"]:
        lines.append(f"- 一阶邻域（{len(ra['orig_nb_vals'])} 个）收益范围："
                     f"{min(ra['orig_nb_vals']):.2%} ~ {max(ra['orig_nb_vals']):.2%}，"
                     f"全正：{'是' if all(v > 0 for v in ra['orig_nb_vals']) else '否'}")
    if ra["orig_robust"]:
        lines.append("- **判定：✅ 原参数稳健**（自身+邻域全正，非边界伪影/池子偏差）")
    else:
        lines.append("- **判定：⚠️ 原参数不稳健**（自身或邻域存在非正值——"
                     "原结论可能是网格边界伪影或池子偏差）")
    lines += [
        "",
        "## 三、稳健区域明细",
        "",
    ]
    if ra["robust_points"]:
        lines.append("| ma5_dev | freshness | chasing | 收益 |")
        lines.append("|---|---|---|---|")
        for m, f, c, v in sorted(ra["robust_points"], key=lambda x: -x[3]):
            lines.append(f"| {m} | {f} | {c} | {v:.2%} |")
    else:
        lines.append("无稳健点（整格网无自身+邻域全正的组合）。")
    lines += [
        "",
        "## 四、全网格收益矩阵（按 chasing 分面，行=ma5_dev，列=freshness）",
        "",
    ]
    for ch in GRID_CHASING:
        sub = grid_df[grid_df["chasing"] == ch]
        if sub.empty:
            continue
        pivot = sub.pivot_table(index="ma5_dev", columns="freshness", values="total_return")
        lines.append(f"### chasing = {ch}")
        lines.append("")
        lines.append("| ma5\\fresh | " + " | ".join(str(c) for c in pivot.columns) + " |")
        lines.append("|---|" + "---|" * len(pivot.columns))
        for idx, row in pivot.iterrows():
            cells = " | ".join(
                f"{v:+.1%}" if pd.notna(v) else "—" for v in row
            )
            lines.append(f"| {idx} | {cells} |")
        lines.append("")
    lines += [
        "---",
        "统计纪律：本报告为稳健性诊断（修复I/J），不据此挑选新参数。"
        "如需重新选参须用新数据段（见 docs/决策日志.md）。",
        "口径备注：收益数字含幸存者偏差（乐观），横向比较有效、绝对值无效。",
        "",
        "以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。",
    ]
    out = out_dir / f"grid_v2_{pool_label}_halt_robustness.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"稳健性报告已写入: {out}")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool", choices=["old", "strat"], default="strat",
                        help="old=head-50 对照池 / strat=分层抽样池")
    parser.add_argument("--n", type=int, default=200, help="分层池目标数量")
    parser.add_argument("--layers", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    client = TushareClient(token=settings.resolved_tushare_token(),
                           cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())
    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)

    # ===== 构池（修复J） =====
    if args.pool == "old":
        basic = client.query("stock_basic", {"list_status": "L"})
        dfb = basic.copy()
        mask = (
            ~dfb["ts_code"].str.startswith("688")
            & ~dfb["ts_code"].str.startswith("300")
            & ~dfb["ts_code"].str.startswith("301")
        )
        if "name" in dfb.columns:
            mask &= ~dfb["name"].str.contains("ST", na=False)
        codes = dfb[mask].sort_values("ts_code")["ts_code"].head(50).tolist()
        pool_label = "old50"
        logger.info(f"[池] 对照组：head-50（{len(codes)} 只）")
    else:
        codes = build_stratified_pool(
            client, store, target_n=args.n, layers=args.layers,
            seed=args.seed, universe_cfg=settings.universe,
        )
        pool_label = f"strat{args.n}"
        if not codes:
            logger.error("分层池构建失败")
            sys.exit(1)
        # 池清单落盘（复现用）
        (out_dir / f"pool_{pool_label}_seed{args.seed}.json").write_text(
            json.dumps(codes, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ===== 取数 =====
    data = fetch_pool_data(client, store, codes, TRAIN_START, TRAIN_END)
    if len(data) < 20:
        logger.error(f"有效数据不足（{len(data)}）")
        sys.exit(1)

    # ===== 修复E(第三轮)：板块映射（训练口径与实盘一致） =====
    sector_map = build_sector_map(client, store)

    # ===== 扩边网格（修复I；修复A含停手口径重跑 → 输出 _halt 后缀） =====
    combos = list(itertools.product(GRID_MA5_DEV, GRID_FRESHNESS, GRID_CHASING))
    logger.info(f"[网格] {len(combos)} 组 × {len(data)} 只（训练段 {TRAIN_START}~{TRAIN_END}，"
                f"含连亏停手+板块40%口径）")
    rows = []
    t0 = time.time()
    for k, (ma5_dev, fresh, chasing) in enumerate(combos, 1):
        t1 = time.time()
        m = run_one(data, settings, ma5_dev, fresh, chasing, TRAIN_START, TRAIN_END,
                    sector_by_code=sector_map)
        rows.append({
            "ma5_dev": ma5_dev, "freshness": fresh, "chasing": chasing,
            **{kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in m.items()},
        })
        logger.info(f"[{k}/{len(combos)}] ma5={ma5_dev} fresh={fresh} chase={chasing} "
                    f"→ ret={m['total_return']:.2%} calmar={m['calmar']:.2f} "
                    f"trades={m['trades']} ({time.time()-t1:.0f}s)")
    grid_df = pd.DataFrame(rows)
    csv_path = out_dir / f"grid_v2_{pool_label}_halt.csv"
    grid_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info(f"网格完成（{time.time()-t0:.0f}s）→ {csv_path.name}")

    # ===== 稳健性分析（修复I） =====
    ra = robustness_analysis(grid_df, metric="total_return")
    report_path = write_robustness_report(pool_label, grid_df, ra, out_dir)

    # ===== 修复H.2：summary JSON（后端读结构化数据，不再正则抓 MD） =====
    summary = {
        "pool_label": pool_label,
        "pool_built_date": str(date.today()),
        "data_segment": [str(TRAIN_START), str(TRAIN_END)],
        "n_robust": ra["n_robust"],
        "n_total": ra["n_total"],
        "best": {
            "ma5_dev": ra["best_key"][0], "freshness": ra["best_key"][1],
            "chasing": ra["best_key"][2], "total_return": ra["best_val"],
        },
        "orig_params": list(ORIG_PARAMS),
        "orig_total_return": ra["orig_val"],
        "orig_robust": ra["orig_robust"],
        "halt_rule": "连亏3笔停手30天（铁律F，含在口径内）",
        "sector_rule": "板块≤40%（修复E，含在口径内）",
        "survivorship_bias": "本池按当前上市股票构建，历史回测收益系统性偏高（乐观口径）",
    }
    summary_path = out_dir / f"grid_v2_{pool_label}_halt_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"摘要已写入: {summary_path.name}")

    # ===== 控制台结论 =====
    print("\n" + "=" * 78)
    print(f"扩边网格稳健性诊断 · {pool_label} 池（{len(data)} 只，训练段）")
    print("=" * 78)
    print(f"稳健点: {ra['n_robust']}/{ra['n_total']}"
          f"（{ra['n_robust']/max(1,ra['n_total']):.0%} 的组合自身+邻域全正）")
    print(f"最优: ma5={ra['best_key'][0]} fresh={ra['best_key'][1]} chase={ra['best_key'][2]}"
          f" → {ra['best_val']:.2%}（平坦度 {ra['best_flatness']:+.2%}）")
    print(f"原参数 {ORIG_PARAMS}: {ra['orig_val']:.2%}"
          if ra['orig_val'] is not None else "原参数不在网格内")
    if ra["orig_nb_vals"]:
        print(f"  邻域收益: {min(ra['orig_nb_vals']):.2%} ~ {max(ra['orig_nb_vals']):.2%}"
              f"（全正: {'是' if all(v > 0 for v in ra['orig_nb_vals']) else '否'}）")
    print(f"\n判定: {'✅ 原参数稳健' if ra['orig_robust'] else '⚠️ 原参数不稳健'}")
    print(f"报告: {report_path}")
    print("\n统计纪律: 本轮为诊断性使用，不据此选参（三数据段已消费，见 docs/决策日志.md）")
    print("\n以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")


if __name__ == "__main__":
    main()

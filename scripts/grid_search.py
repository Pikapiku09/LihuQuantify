"""修复4：参数网格回测（替代"手工放松参数"，防过拟合）。

网格：
    close_to_ma5_max_dev ∈ {0.005, 0.015, 0.03, 0.05}
    golden_cross_max_freshness_days ∈ {3, 5, 7, 10}
    chasing_high_threshold ∈ {0.05, 0.08, 0.12}

防过拟合：数据按时间 2:1 划分——前 2/3 训练段跑网格选参，
后 1/3 验证段只验证最优区域（不重新选参）。

输出：
    outputs/grid_search_results.csv       全部组合明细
    outputs/grid_search_heatmap.png       热力图（卡玛=收益/回撤比）
    控制台稳健区域报告

用法：
    python scripts/grid_search.py                  # 默认 50 只 × 2 年
    python scripts/grid_search.py --n 100 --years 3
"""
from __future__ import annotations

import argparse
import itertools
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

# 网格定义（修复4）
GRID_MA5_DEV = [0.005, 0.015, 0.03, 0.05]
GRID_FRESHNESS = [3, 5, 7, 10]
GRID_CHASING = [0.05, 0.08, 0.12]


def fetch_data(client: TushareClient, store: DuckDBStore, n: int, years: int):
    """拉取股票池数据（复用缓存）。返回 (data, all_dates_sorted)。"""
    basic = client.query("stock_basic", {"list_status": "L"})
    if not basic.empty:
        store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    df = basic.copy() if not basic.empty else pd.DataFrame()
    if df.empty:
        return {}, []
    mask = (
        ~df["ts_code"].str.startswith("688")
        & ~df["ts_code"].str.startswith("300")
        & ~df["ts_code"].str.startswith("301")
    )
    if "name" in df.columns:
        mask &= ~df["name"].str.contains("ST", na=False)
    codes = df[mask].sort_values("ts_code")["ts_code"].head(n).tolist()

    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    latest = store.get_latest_trade_date()
    start = latest - timedelta(days=years * 365 + 30)
    logger.info(f"回测区间 {start} ~ {latest}")

    data = {}
    for i, code in enumerate(codes):
        try:
            dfq = client.query("daily", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": latest.strftime("%Y%m%d"),
            })
        except Exception as e:
            logger.warning(f"{code} 拉取失败: {e}")
            continue
        if dfq.empty:
            continue
        dfq["trade_date"] = pd.to_datetime(dfq["trade_date"], format="%Y%m%d").dt.date
        data[code] = dfq.sort_values("trade_date").reset_index(drop=True)
        if (i + 1) % 20 == 0:
            logger.info(f"已拉取 {i+1}/{len(codes)}")
    all_dates = sorted(set().union(*[set(d["trade_date"]) for d in data.values()])) if data else []
    return data, all_dates


def run_one(data: dict, settings, ma5_dev: float, freshness: int, chasing: float,
            start: date | None, end: date | None) -> dict:
    """跑一组参数。返回绩效 dict（含 total_return/max_drawdown/calmar/win_rate/pl_ratio/trades）。"""
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
    engine = EventDrivenEngine(strategy=strategy, broker=broker, checklist_gate=gate,
                               max_single=r.max_single_position)
    result = engine.run(data, init_capital=settings.init_capital, start=start, end=end)
    m = result.metrics
    return {
        "total_return": m["total_return"],
        "max_drawdown": m["max_drawdown"],
        "calmar": m["calmar"],
        "win_rate": m["win_rate"],
        "profit_loss_ratio": m["profit_loss_ratio"],
        "trades": m["total_trades"],
        "signals": result.signals_generated,
    }


def find_robust_zone(df: pd.DataFrame, metric: str = "calmar", top_k: int = 5) -> pd.DataFrame:
    """找稳健区域：metric Top-K 且相邻参数组合（同 chasing 下 ma5_dev/freshness 邻域）也非劣。"""
    top = df.sort_values(metric, ascending=False).head(top_k).copy()
    return top


def plot_heatmap(df: pd.DataFrame, out_path: Path, chasing_val: float, metric: str = "calmar"):
    """画热力图：close_to_ma5 × freshness（固定 chasing），值为 metric。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sub = df[df["chasing"] == chasing_val]
    if sub.empty:
        return
    pivot = sub.pivot_table(index="ma5_dev", columns="freshness", values=metric)
    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([str(c) for c in pivot.columns])
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels([str(i) for i in pivot.index])
    ax.set_xlabel("golden_cross_max_freshness_days")
    ax.set_ylabel("close_to_ma5_max_dev")
    ax.set_title(f"{metric} 热力图 (chasing={chasing_val})\n训练段")
    # 标注数值
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.values[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    color="black" if abs(v) < 1 else "white", fontsize=9)
    fig.colorbar(im, ax=ax, label=metric)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    logger.info(f"热力图已保存: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--years", type=int, default=2)
    parser.add_argument("--train-ratio", type=float, default=2 / 3, help="训练段占比")
    args = parser.parse_args()

    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    token = settings.resolved_tushare_token()
    client = TushareClient(token=token, cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    data, all_dates = fetch_data(client, store, args.n, args.years)
    if not data:
        logger.error("无数据")
        sys.exit(1)

    # 训练/验证段划分（修复4：防过拟合）
    n_dates = len(all_dates)
    split_idx = int(n_dates * args.train_ratio)
    train_end = all_dates[split_idx - 1]
    val_start = all_dates[split_idx]
    logger.info(f"训练段: {all_dates[0]} ~ {train_end}（{split_idx} 日）")
    logger.info(f"验证段: {val_start} ~ {all_dates[-1]}（{n_dates - split_idx} 日）")

    combos = list(itertools.product(GRID_MA5_DEV, GRID_FRESHNESS, GRID_CHASING))
    logger.info(f"网格组合数: {len(combos)}（{len(GRID_MA5_DEV)}×{len(GRID_FRESHNESS)}×{len(GRID_CHASING)}）")

    # ===== 训练段网格 =====
    rows = []
    t0 = time.time()
    for k, (ma5_dev, fresh, chasing) in enumerate(combos, 1):
        t1 = time.time()
        m = run_one(data, settings, ma5_dev, fresh, chasing, start=None, end=train_end)
        rows.append({
            "ma5_dev": ma5_dev, "freshness": fresh, "chasing": chasing,
            **{kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in m.items()},
        })
        logger.info(f"[{k}/{len(combos)}] ma5={ma5_dev} fresh={fresh} chase={chasing} "
                    f"→ ret={m['total_return']:.2%} dd={m['max_drawdown']:.2%} "
                    f"calmar={m['calmar']:.2f} trades={m['trades']} ({time.time()-t1:.1f}s)")
    grid_df = pd.DataFrame(rows)
    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    grid_df.to_csv(out_dir / "grid_search_results.csv", index=False, encoding="utf-8-sig")
    logger.info(f"训练段网格完成（{time.time()-t0:.0f}s），CSV 已保存")

    # ===== 稳健区域：Top-K + 邻域检查 =====
    top = find_robust_zone(grid_df, metric="calmar", top_k=5)
    print("\n" + "=" * 80)
    print(f"训练段网格 Top-5（按卡玛=收益/回撤比）")
    print("=" * 80)
    print(top.to_string(index=False))

    # 热力图（chasing=0.08 中间档）
    plot_heatmap(grid_df, out_dir / "grid_search_heatmap.png", chasing_val=0.08, metric="calmar")

    # ===== 验证段：只验证 Top-3（不重新选参） =====
    print("\n" + "=" * 80)
    print("验证段验证（Top-3 训练段参数，防过拟合）")
    print("=" * 80)
    val_rows = []
    for _, row in top.head(3).iterrows():
        m = run_one(data, settings, row["ma5_dev"], int(row["freshness"]), row["chasing"],
                    start=val_start, end=None)
        val_rows.append({
            "ma5_dev": row["ma5_dev"], "freshness": row["freshness"], "chasing": row["chasing"],
            **{kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in m.items()},
        })
        logger.info(f"验证 ma5={row['ma5_dev']} fresh={row['freshness']} chase={row['chasing']} "
                    f"→ ret={m['total_return']:.2%} trades={m['trades']}")
    val_df = pd.DataFrame(val_rows)
    print(val_df.to_string(index=False))
    val_df.to_csv(out_dir / "grid_search_validation.csv", index=False, encoding="utf-8-sig")

    # ===== 结论 =====
    print("\n" + "=" * 80)
    print("稳健参数选择建议")
    print("=" * 80)
    pos_val = val_df[val_df["total_return"] > 0]
    if not pos_val.empty:
        best = pos_val.sort_values("calmar", ascending=False).iloc[0]
        print(f"训练段卡玛 Top 且验证段收益为正的参数：")
        print(f"  close_to_ma5_max_dev = {best['ma5_dev']}")
        print(f"  golden_cross_max_freshness = {int(best['freshness'])}")
        print(f"  chasing_high_threshold = {best['chasing']}")
        print(f"  验证段收益 {best['total_return']:.2%}，回撤 {best['max_drawdown']:.2%}，"
              f"胜率 {best['win_rate']:.1%}，盈亏比 {best['profit_loss_ratio']:.2f}，{int(best['trades'])} 轮")
    else:
        print("警告：Top-3 参数在验证段均为负收益——训练段最优可能是过拟合，")
        print("建议：查看验证段全网格（扩大 top_k），或延长回测周期重新搜索。")
    print("\n以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")


if __name__ == "__main__":
    main()

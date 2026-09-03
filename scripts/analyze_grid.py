"""补充分析：热力图重绘（绕过沙箱）+ 验证段市场状态诊断。

目的：判断验证段负收益是"参数过拟合"还是"市场环境切换"（震荡市）。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

# matplotlib 配置目录指向项目内（绕过用户目录沙箱限制）
ROOT = Path(__file__).resolve().parent.parent
os.environ["MPLCONFIGDIR"] = str(ROOT / "outputs" / ".mpl")
(ROOT / "outputs" / ".mpl").mkdir(parents=True, exist_ok=True)
os.environ["MPLBACKEND"] = "Agg"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.market import classify_market_state


def main():
    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    client = TushareClient(token=settings.resolved_tushare_token(), cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    latest = store.get_latest_trade_date() or idx_df["trade_date"].max()
    start = latest - timedelta(days=2 * 365 + 30)
    idx_df = idx_df[(idx_df["trade_date"] >= start) & (idx_df["trade_date"] <= latest)].sort_values("trade_date").reset_index(drop=True)

    n = len(idx_df)
    split_idx = int(n * 2 / 3)
    train_end = idx_df["trade_date"].iloc[split_idx - 1]
    val_start = idx_df["trade_date"].iloc[split_idx]
    print(f"训练段: {idx_df['trade_date'].iloc[0]} ~ {train_end}（{split_idx} 交易日）")
    print(f"验证段: {val_start} ~ {idx_df['trade_date'].iloc[-1]}（{n - split_idx} 交易日）")

    # 分段统计训练段 vs 验证段市场状态
    states = classify_market_state(idx_df)
    df = idx_df.copy()
    df["state"] = df["trade_date"].map(states)
    df["seg"] = ["训练"] * split_idx + ["验证"] * (n - split_idx)

    print("\n--- 市场状态分布（000001.SH 20日涨幅分段）---")
    pivot = df.groupby(["seg", "state"]).size().unstack(fill_value=0)
    print(pivot.to_string())
    pct = df.groupby("seg")["state"].value_counts(normalize=True).unstack(fill_value=0)
    print("\n占比:")
    print((pct * 100).round(1).to_string())

    # 验证段指数涨跌
    val_df = df[df["seg"] == "验证"]
    ret = val_df["close"].iloc[-1] / val_df["close"].iloc[0] - 1
    print(f"\n验证段上证指数涨跌: {ret:.2%}")
    train_df = df[df["seg"] == "训练"]
    tret = train_df["close"].iloc[-1] / train_df["close"].iloc[0] - 1
    print(f"训练段上证指数涨跌: {tret:.2%}")

    # ===== 重绘热力图 =====
    grid = pd.read_csv(ROOT / "outputs" / "grid_search_results.csv")
    for metric in ["calmar", "total_return"]:
        for chasing in [0.05, 0.08, 0.12]:
            sub = grid[grid["chasing"] == chasing]
            if sub.empty:
                continue
            pivot = sub.pivot_table(index="ma5_dev", columns="freshness", values=metric)
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(7, 5))
            im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto")
            ax.set_xticks(range(len(pivot.columns)))
            ax.set_xticklabels([str(c) for c in pivot.columns])
            ax.set_yticks(range(len(pivot.index)))
            ax.set_yticklabels([str(i) for i in pivot.index])
            ax.set_xlabel("golden_cross_max_freshness_days")
            ax.set_ylabel("close_to_ma5_max_dev")
            ax.set_title(f"{metric} (chasing={chasing}) train-segment")
            for i in range(pivot.shape[0]):
                for j in range(pivot.shape[1]):
                    v = pivot.values[i, j]
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                            color="black" if abs(v) < 2 else "white", fontsize=9)
            fig.colorbar(im, ax=ax)
            fig.tight_layout()
            out = ROOT / "outputs" / f"grid_heatmap_{metric}_chase{chasing}.png"
            fig.savefig(out, dpi=110)
            plt.close(fig)
            print(f"热力图: {out.name}")

    print("\n以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")


if __name__ == "__main__":
    main()

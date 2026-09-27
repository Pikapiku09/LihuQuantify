"""PIT-3：intraday_rev_20 在 PIT 池（含退市股）复验——量化幸存者偏差。

原池（幸存者 200 只）基线：RankIC=+0.0725 ICIR=0.48 多空+15.2%（2019-2026.8 全样本）
PIT 池（390 只 = 200 幸存 + 190 真退市）重跑同口径。
判定：IC 同向且幅度降幅 <30% → 原结论稳健；大幅衰减 → 幸存者偏差主导。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
from loguru import logger

from lihu_quantify.research import compute_factor, evaluate_factor
from lihu_quantify.research.regime import classify_regime


def main() -> None:
    # 合并面板（duckdb 直读两 parquet union）
    panel = duckdb.query(f"""
        SELECT ts_code, trade_date, open, high, low, close, vol, amount
        FROM '{ROOT / 'data' / 'research_panel.parquet'}'
        UNION ALL
        SELECT ts_code, trade_date, open, high, low, close, vol, amount
        FROM '{ROOT / 'data' / 'pit_delisted.parquet'}'
    """).fetchdf()
    panel = panel[(panel["close"] > 0.5) & (panel["amount"] > 0)]
    panel = panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    logger.info(f"PIT 面板：{len(panel)} 行 × {panel['ts_code'].nunique()} 只")

    # 退市股单独看（它们对 IC 的贡献方向）
    delisted = set(open(ROOT / "data" / "pit_delisted_list.txt").read().split())
    is_del = panel["ts_code"].isin(delisted)
    logger.info(f"其中退市股行数：{int(is_del.sum())}（{panel.loc[is_del, 'ts_code'].nunique()} 只）")

    con = duckdb.connect(str(ROOT / "data" / "lihu_quant.duckdb"), read_only=True)
    idx = con.execute("SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
                      "AND trade_date BETWEEN '2019-01-01' AND '2026-08-31' ORDER BY trade_date").fetchdf()
    con.close()
    regime = classify_regime(idx)

    factor = compute_factor(panel, "intraday_rev_20")
    res = evaluate_factor(panel, factor, name="intraday_rev_20_PIT", horizon=20,
                          cost_bps=15.0, n_trials=21, regime=regime)
    d = res.as_dict()
    logger.info(f"[PIT 全池] RankIC={d['rank_ic_mean']:+.4f} ICIR={d['rank_ic_ir']:+.2f} "
                f"t={d['rank_ic_t']:+.1f} 多空={d['long_short_ann']:+.2%} 单调ρ={d['group_monotonic']:+.2f} "
                f"icDSR={d['ic_dsr']:.3f} regime={d['regime_consistency']}")

    # 仅退市股子样本（检验退市前信号是否同样有效——更本质的偏差检验）
    sub = panel[is_del].reset_index(drop=True)
    f_sub = factor[is_del].reset_index(drop=True)
    if len(sub) > 10000:
        res2 = evaluate_factor(sub, f_sub, name="intraday_rev_20_delisted_only", horizon=20,
                               cost_bps=15.0, n_trials=21, regime=regime)
        d2 = res2.as_dict()
        logger.info(f"[仅退市股] RankIC={d2['rank_ic_mean']:+.4f} ICIR={d2['rank_ic_ir']:+.2f} "
                    f"t={d2['rank_ic_t']:+.1f} 多空={d2['long_short_ann']:+.2%} 单调ρ={d2['group_monotonic']:+.2f}")

    out = {"pit_full": d, "delisted_only": d2 if len(sub) > 10000 else None,
           "baseline_survivor_only": {"rank_ic_mean": 0.0725, "rank_ic_ir": 0.48,
                                       "long_short_ann": 0.1524}}
    p = ROOT / "outputs" / "pit_verify_result.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"→ {p}")


if __name__ == "__main__":
    main()

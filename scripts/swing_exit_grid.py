"""波段池出场参数梯度网格（机制验证用，非参数优选；2026-10-19 审计修正版）。

修正内容：出场/期末清算双重计数修复后重跑；新增剔除 TOP1/TOP3 后收益列（集中度审计）。
组合：trail ∈ {无,20%,15%,12%,10%,8%} × 时间帽 ∈ {63,126}，另在 trail=20% 上做时间维度 90/112/140/189/无。
输出：outputs/swing_exit_grid.json + 表格
用法：python scripts/swing_exit_grid.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from loguru import logger

spec = importlib.util.spec_from_file_location("bt", ROOT / "scripts" / "run_swing_backtest.py")
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

GRID = [(t, 63) for t in (0.0, 0.80, 0.85, 0.88, 0.90, 0.92)]
GRID += [(t, 126) for t in (0.0, 0.80, 0.85, 0.88, 0.90, 0.92)]
GRID += [(0.80, t) for t in (90, 112, 140, 189, 999)]


def main() -> None:
    gold, series = bt.load_code_series()
    logger.info(f"网格开始：{len(series)} 只，{len(GRID)} 组合")
    rows = []
    for trail, tstop in GRID:
        res = bt.simulate(gold, series, bt.SLOPE_MIN, trail=trail, time_stop=tstop)
        m = res["metrics"]
        c = res.get("concentration") or {}
        y = res["yearly"]
        rows.append({
            "trail": trail, "time_stop": tstop,
            "total": m["total_return"], "cagr": m["cagr"], "max_dd": m["max_drawdown"],
            "pf": m["profit_factor"], "wl": m["win_loss_ratio"], "win_rate": m["win_rate"],
            "n": m["n_trades"], "avg_win": m["avg_win"], "avg_loss": m["avg_loss"],
            "y2024": (y.get("2024") or {}).get("ret"),
            "y2025": (y.get("2025") or {}).get("ret"),
            "y2026": (y.get("2026") or {}).get("ret"),
            "ex_top1": c.get("ex_top1"), "ex_top3": c.get("ex_top3"),
            "top1": c.get("top1"), "exits": res["exit_reasons"],
        })
        logger.info(f"trail={trail} time={tstop} -> total={rows[-1]['total']:.3f} pf={rows[-1]['pf']} "
                    f"ex_top1={rows[-1]['ex_top1']} ex_top3={rows[-1]['ex_top3']} y26={rows[-1]['y2026']}")
    out = {"generated_at": bt.date.today().isoformat(), "engine": "2026-10-19 审计修复版",
           "note": "trail=0 表示无固定百分比移动止盈；time_stop=999 视为无时间帽；ex_top1/ex_top3 为剔除最大盈利单后的近似累计收益",
           "rows": rows}
    (ROOT / "outputs" / "swing_exit_grid.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    hdr = f"{'trail':>6} {'time':>5} {'total':>8} {'cagr':>7} {'maxDD':>7} {'PF':>5} {'W/L':>5} {'win%':>5} {'n':>4} {'2024':>7} {'2025':>7} {'2026':>7} {'ex_top1':>8} {'ex_top3':>8}"
    print(hdr)
    for r in rows:
        t = 'none' if r['trail'] == 0 else f"{int((1 - r['trail']) * 100)}%"
        fmt = lambda x: ' ' * 7 if x is None else f"{x * 100:>6.1f}%"
        print(f"{t:>6} {r['time_stop']:>5} {r['total']*100:>7.1f}% {r['cagr']*100:>6.1f}% {r['max_dd']*100:>6.1f}% "
              f"{(r['pf'] or 0):>5.2f} {(r['wl'] or 0):>5.2f} {(r['win_rate'] or 0)*100:>4.0f}% {r['n']:>4} "
              f"{(r['y2024'] or 0)*100:>6.1f}% {(r['y2025'] or 0)*100:>6.1f}% {(r['y2026'] or 0)*100:>6.1f}% "
              f"{fmt(r['ex_top1'])} {fmt(r['ex_top3'])}")


if __name__ == "__main__":
    main()

"""波段池出场参数梯度网格（机制验证用，非参数优选）。

目的：验证移动止盈阈值对收益/盈亏比的影响是"光滑的机制性改善"还是"孤立尖峰"。
组合：trail ∈ {无, 20%, 15%, 12%, 10%, 8% 回撤} × 时间止损 ∈ {63, 126} 交易日（ma20 出场固定关闭：实测有害）
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

TRAILS = [0.0, 0.80, 0.85, 0.88, 0.90, 0.92]   # 0=无移动止盈
TIMES = [63, 126]


def main() -> None:
    gold, series = bt.load_code_series()
    logger.info(f"网格开始：{len(series)} 只")
    rows = []
    for tstop in TIMES:
        for trail in TRAILS:
            res = bt.simulate(gold, series, bt.SLOPE_MIN, trail=trail, time_stop=tstop)
            m = res["metrics"]
            y26 = (res["yearly"].get("2026") or {}).get("ret")
            row = {
                "trail": trail, "time_stop": tstop,
                "total": m["total_return"], "cagr": m["cagr"], "max_dd": m["max_drawdown"],
                "pf": m["profit_factor"], "wl": m["win_loss_ratio"], "win_rate": m["win_rate"],
                "n": m["n_trades"], "avg_win": m["avg_win"], "avg_loss": m["avg_loss"],
                "y2024": (res["yearly"].get("2024") or {}).get("ret"),
                "y2025": (res["yearly"].get("2025") or {}).get("ret"),
                "y2026": y26,
                "exits": res["exit_reasons"],
            }
            rows.append(row)
            logger.info(f"trail={trail} time={tstop} -> total={row['total']} pf={row['pf']} wl={row['wl']} dd={row['max_dd']} y26={y26}")
    out = {"generated_at": bt.date.today().isoformat(), "rows": rows}
    (ROOT / "outputs" / "swing_exit_grid.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{'trail':>6} {'time':>4} {'total':>8} {'cagr':>7} {'maxDD':>7} {'PF':>5} {'W/L':>5} {'win%':>5} {'n':>4} {'2024':>7} {'2025':>7} {'2026':>7}")
    for r in rows:
        t = 'none' if r['trail'] == 0 else f"{int((1 - r['trail']) * 100)}%"
        print(f"{t:>6} {r['time_stop']:>4} {r['total']*100:>7.1f}% {r['cagr']*100:>6.1f}% {r['max_dd']*100:>6.1f}% "
              f"{(r['pf'] or 0):>5} {(r['wl'] or 0):>5} {(r['win_rate'] or 0)*100:>4.0f}% {r['n']:>4} "
              f"{(r['y2024'] or 0)*100:>6.1f}% {(r['y2025'] or 0)*100:>6.1f}% {(r['y2026'] or 0)*100:>6.1f}%")


if __name__ == "__main__":
    main()

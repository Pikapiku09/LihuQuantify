"""模拟盘组合权益历史（每日一个点，追加写）。

为什么必须持久化
----------------
Lihu 现有的 ``/api/equity`` 读的是 ``outputs/backtest_result.json``——那是**回测**曲线，
不是模拟盘真实权益；``data/paper_state.json`` 只存当期快照。
没有日级序列，「量化策略」页就只能把回撤显示成 null（契约里的 ``drawdown_note``）。
更糟的是 ``paper_state.high_water_mark`` 是**每只股票**的价格高点
（``{"000429.SZ": 13.78}``），拿它算组合回撤会得出一个看起来很像真的假数字。

两类数据来源，必须分开记
------------------------
- ``live``：巡检每日落盘，来自 ``paper_state.asset.total_asset``，是真实记账值
- ``reconstructed``：由历史交割单重建（``scripts/monthly_perf.py`` 的口径），
  含一处已知估值跳变（600584.SH 2026-08-03，−8,688 元）

不区分的话，谁也不知道曲线前半段是补的、后半段是真的——那正是本仓库反复吃的亏。

写法：JSONL，一天一行；同日冲突时 ``live`` 优先于 ``reconstructed``（真值胜过重建值）。
"""

from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path
from typing import Any, Iterable, Optional

EQUITY_FILE = Path("data") / "equity_history.jsonl"
SOURCE_LIVE = "live"
SOURCE_RECONSTRUCTED = "reconstructed"
SOURCE_PRIORITY = {SOURCE_RECONSTRUCTED: 0, SOURCE_LIVE: 1}


def _norm_date(value: Any) -> Optional[str]:
    """把 2026-11-17 / 20261117 / date 统一成 YYYY-MM-DD；认不出返回 None。"""
    if value is None:
        return None
    if isinstance(value, _date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def load_points(path: Path) -> list[dict]:
    """读全部权益点，按日期升序。坏行跳过而不是整份失败。"""
    if not path.exists():
        return []
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        d = _norm_date(row.get("date"))
        total = row.get("total_asset")
        if d is None or not isinstance(total, (int, float)):
            continue
        src = row.get("source") if row.get("source") in SOURCE_PRIORITY else SOURCE_LIVE
        row["date"] = d
        row["total_asset"] = float(total)
        row["source"] = src
        # 同日多行时，来源优先级高者胜出
        prev = out.get(d)
        if prev is None or SOURCE_PRIORITY[src] >= SOURCE_PRIORITY[prev["source"]]:
            out[d] = row
    return [out[d] for d in sorted(out)]


def append_point(path: Path, when: Any, total_asset: float, *,
                 cash: Optional[float] = None, market_value: Optional[float] = None,
                 source: str = SOURCE_LIVE) -> bool:
    """追加或覆盖一个权益点。返回 True 表示文件被改动。

    同日已有更高优先级的来源时不覆盖（重建值不得覆盖真实记账值）。
    """
    d = _norm_date(when)
    if d is None or not isinstance(total_asset, (int, float)):
        return False
    src = source if source in SOURCE_PRIORITY else SOURCE_LIVE
    points = {p["date"]: p for p in load_points(path)}
    prev = points.get(d)
    if prev is not None and SOURCE_PRIORITY[src] < SOURCE_PRIORITY[prev["source"]]:
        return False
    new = {"date": d, "total_asset": float(total_asset), "source": src}
    if cash is not None:
        new["cash"] = float(cash)
    if market_value is not None:
        new["market_value"] = float(market_value)
    if prev == new:
        return False
    points[d] = new
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(points[d], ensure_ascii=False) for d in sorted(points)) + "\n",
        encoding="utf-8",
    )
    return True


def summarize(points: Iterable[dict]) -> dict:
    """算当前回撤与最大回撤。没有点就全部置 None——不猜。"""
    seq = [p for p in points if isinstance(p.get("total_asset"), (int, float))]
    if not seq:
        return {
            "n_points": 0, "first_date": None, "last_date": None,
            "current_drawdown": None, "peak_equity": None, "latest_equity": None,
            "max_drawdown": None, "max_drawdown_peak_date": None,
            "max_drawdown_trough_date": None, "live_days": 0, "reconstructed_days": 0,
        }
    peak = -1.0
    peak_date = seq[0]["date"]
    mdd = 0.0
    mdd_peak = mdd_trough = seq[0]["date"]
    for p in seq:
        v = float(p["total_asset"])
        if v > peak:
            peak, peak_date = v, p["date"]
        dd = v / peak - 1.0 if peak > 0 else 0.0
        if dd < mdd:
            mdd, mdd_peak, mdd_trough = dd, peak_date, p["date"]
    latest = float(seq[-1]["total_asset"])
    return {
        "n_points": len(seq),
        "first_date": seq[0]["date"],
        "last_date": seq[-1]["date"],
        "current_drawdown": round(latest / peak - 1.0, 4) if peak > 0 else None,
        "peak_equity": round(peak, 2),
        "latest_equity": round(latest, 2),
        "max_drawdown": round(mdd, 4),
        "max_drawdown_peak_date": mdd_peak,
        "max_drawdown_trough_date": mdd_trough,
        "live_days": sum(1 for p in seq if p.get("source") == SOURCE_LIVE),
        "reconstructed_days": sum(1 for p in seq if p.get("source") == SOURCE_RECONSTRUCTED),
    }

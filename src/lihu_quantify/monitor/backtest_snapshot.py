"""策略回测快照（供工作台「策略回测」页使用，2026-11-17，P2）。

与「量化策略」页的分工
----------------------
- 「判断质量」页评的是 **AI 结论**准不准（conclusions.jsonl → 第 5/20 交易日涨跌）
- 本模块给「策略回测」页用，讲的是 **策略代码**在历史数据上的表现
两者都带「回测」字样但对象完全不同，页面上必须写清边界，否则会被当成同一件事。

两个数据源，口径差得很远，绝不能混在一张表里
--------------------------------------------
1. ``outputs/backtest_result.json``（2026-11-16）：实盘在跑的 IntradayReversal，
   head-N 主板池 50 只 × 2 年。**总收益 −4.06%**、夏普 −0.803、最大回撤 −5.46%、胜率 33%。
   这是「当前策略长什么样」的答案。
2. ``outputs/grid_v2_*_summary.json``：参数网格寻优，最好一组 +80.31%。
   但那是**训练段**寻优结果，池子按当前上市股票构建、含**幸存者偏差**，不能当预期收益。
   若把 +80.31% 和 −4.06% 并排而不说明，等于在骗自己。

所以本模块把 ``survivorship_bias`` 原样透传，页面必须显著展示。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

BACKTEST_FILE = Path("outputs") / "backtest_result.json"
GRID_GLOB = "grid_v2_*_summary.json"


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _downsample(points: list[dict], limit: int) -> list[dict]:
    """曲线降采样：保留首末，等距抽稀，避免 508 点全塞进契约。"""
    seq = [p for p in points if isinstance(p.get("equity"), (int, float)) and p.get("date")]
    if len(seq) <= limit:
        return [{"d": str(p["date"]), "v": round(float(p["equity"]), 2)} for p in seq]
    step = (len(seq) - 1) / (limit - 1)
    idx = sorted({min(len(seq) - 1, round(i * step)) for i in range(limit)})
    return [{"d": str(seq[i]["date"]), "v": round(float(seq[i]["equity"]), 2)} for i in idx]


def load_grid(out_dir: Path) -> list[dict]:
    """网格寻优摘要。按文件名排序保证稳定；坏文件跳过。"""
    rows: list[dict] = []
    for p in sorted(out_dir.glob(GRID_GLOB)):
        d = _read_json(p)
        if not d:
            continue
        rows.append({
            "file": p.name,
            "pool_label": d.get("pool_label"),
            "pool_built_date": d.get("pool_built_date"),
            "data_segment": d.get("data_segment"),
            "n_robust": d.get("n_robust"),
            "n_total": d.get("n_total"),
            "best": d.get("best"),
            "orig_params": d.get("orig_params"),
            "orig_total_return": d.get("orig_total_return"),
            "orig_robust": d.get("orig_robust"),
            "survivorship_bias": d.get("survivorship_bias"),
        })
    return rows


def build_backtest_block(root: Path, curve_limit: int = 250) -> dict:
    """组装契约里的 backtest 块。两个来源各自独立，任一缺失只让对应子块为 None。"""
    raw = _read_json(root / BACKTEST_FILE)
    main: Optional[dict[str, Any]] = None
    if raw:
        main = {
            "generated_at": raw.get("generated_at"),
            "config_hash": raw.get("config_hash"),
            "init_capital": raw.get("init_capital"),
            "market_filter_mode": raw.get("market_filter_mode"),
            "pool": raw.get("pool_info") or None,
            "metrics": raw.get("metrics") or None,
            "rules_included": raw.get("rules_included") or None,
            "curve": _downsample(raw.get("equity_curve") or [], curve_limit),
            "curve_points_total": len(raw.get("equity_curve") or []),
        }
    return {
        "main": main,
        "grid": load_grid(root / "outputs"),
        # 一级字段透传幸存者偏差说明，消费端不必去 pool 里挖
        "survivorship_bias": ((main or {}).get("pool") or {}).get("survivorship_bias"),
    }

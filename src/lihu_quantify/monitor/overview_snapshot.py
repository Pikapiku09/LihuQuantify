"""模拟盘总览快照（供工作台「量化策略」页首屏使用，2026-11-17，P1）。

为什么并入「量化策略」页而不单开一页
------------------------------------
总览要展示的核心是「今日巡检 + 权益 + 持仓」。权益曲线与风控状态已经在量化策略页里，
再开一个「模拟盘总览」会让同一个数字出现在两处——两处一旦取值范围不同就会打架，
这正是用户最初担心的「内容重合」。所以本块作为量化策略页的**首屏**，与已有区块并排，
不新增导航项。

数据来源
--------
- ``data/last_scan.json`` → ``summary``：今日巡检的完整结果（信号/成交/拦截/市场闸门/
  持仓富字段/评审进度/今日简报）。这是唯一权威来源，不要在这里重算。
- ``data/paper_state.json`` → 账户与持仓（last_scan 缺失时的兜底，无名称/现价）
- ``data/equity_history.jsonl`` → 今日盈亏（用最近两点差）与累计收益

刻意不做的事
------------
- 不从 ``web/server.py`` 抄 ``_merge_positions`` / ``_live_metrics``：
  那层在包外、靠 sys.path 反向依赖，抄一遍就多一份要同步的实现。
  last_scan 的 summary 已经带了富字段，直接用。
- 不下发容器内绝对路径：``report`` 只给文件名，否则工作台会把 ``/app/outputs/...`` 显示出来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

LAST_SCAN = Path("data") / "last_scan.json"
PAPER_STATE = Path("data") / "paper_state.json"


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _today_pnl(equity_points: list[dict], scan: dict) -> Optional[dict]:
    """今日盈亏：优先用巡检记下的 prev_total_asset，其次用权益历史最近两点。"""
    cur = scan.get("total_asset")
    prev = scan.get("prev_total_asset")
    if not isinstance(prev, (int, float)) and len(equity_points) >= 2:
        prev = equity_points[-2].get("total_asset")
    if not isinstance(prev, (int, float)) or not isinstance(cur, (int, float)) or prev <= 0:
        return None
    return {
        "amount": round(float(cur) - float(prev), 2),
        "ratio": round(float(cur) / float(prev) - 1.0, 4),
        "basis": "prev_total_asset" if isinstance(scan.get("prev_total_asset"), (int, float)) else "equity_history",
    }


def _positions(scan: dict, state: dict) -> list[dict]:
    """持仓优先取 last_scan 的富字段（含名称/现价/市值/止损价），缺失时退回 paper_state。"""
    out: list[dict] = []
    for p in (scan.get("positions") or []):
        if not isinstance(p, dict):
            continue
        cost = p.get("cost")
        price = p.get("price")
        pnl = None
        if isinstance(cost, (int, float)) and cost > 0 and isinstance(price, (int, float)):
            pnl = round(price / cost - 1.0, 4)
        out.append({
            "code": p.get("ts_code"),
            "name": p.get("name"),
            "volume": p.get("volume"),
            "cost": cost,
            "price": price,
            "market_value": p.get("market_value"),
            "pnl_ratio": pnl,
            "stop_price": p.get("stop_price"),
        })
    if out:
        return out
    for code, p in (state.get("positions") or {}).items():
        if not isinstance(p, dict):
            continue
        out.append({
            "code": code, "name": None, "volume": p.get("volume"),
            "cost": p.get("cost"), "price": None,
            "market_value": None, "pnl_ratio": None, "stop_price": None,
        })
    return out


def build_overview_block(root: Path, equity_points: Optional[list[dict]] = None) -> Optional[dict]:
    """组装契约里的 overview 块。两个来源都缺时返回 None（调用方据此进 missing）。"""
    scan_file = _read_json(root / LAST_SCAN) or {}
    scan = scan_file.get("summary") or {}
    state = _read_json(root / PAPER_STATE) or {}
    if not scan and not state:
        return None

    asset = state.get("asset") or {}
    total = scan.get("total_asset") or asset.get("total_asset")
    cash = scan.get("cash") if scan.get("cash") is not None else state.get("cash")
    init = scan.get("init_capital") or state.get("init_capital")
    mv = asset.get("market_value")

    total_return = None
    if isinstance(total, (int, float)) and isinstance(init, (int, float)) and init > 0:
        total_return = round(float(total) / float(init) - 1.0, 4)

    report = scan.get("report")
    report_name = Path(str(report)).name if report else None

    gate = scan.get("market_gate") or {}
    return {
        "trade_date": scan.get("trade_date") or scan_file.get("trade_date"),
        "finished_at": scan_file.get("finished_at"),
        "market_state": scan.get("market_state"),
        "entry_scale": scan.get("entry_scale"),
        "market_gate": {
            "state": gate.get("state"),
            "mode": gate.get("mode"),
            "gate_open": gate.get("gate_open"),
        } if gate else None,
        "counts": {
            "signals": scan.get("signals"),
            "executed": len(scan.get("executed") or []),
            "rejected": len(scan.get("rejected") or []),
            "sells_today": len(scan.get("sells_today") or []),
            "pending_stops": len(scan.get("pending_stops") or []),
            "halted": len(scan.get("halted_codes") or {}),
            "stale_dropped": scan.get("stale_dropped"),
        },
        "account": {
            "cash": cash,
            "total_asset": total,
            "market_value": mv,
            "init_capital": init,
            "total_return": total_return,
            "today_pnl": _today_pnl(equity_points or [], scan),
        },
        "positions": _positions(scan, state),
        "review": scan.get("review") or None,
        "capital": scan.get("capital") or None,
        "alerts": scan.get("alerts") or [],
        "brief": scan.get("brief"),
        "brief_is_ai": bool(scan.get("ai_summary")),
        "ai_summary": scan.get("ai_summary"),
        "report_name": report_name,
    }

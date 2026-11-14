"""回撤观察（第 4 层，2026-11-14）：按用户风险偏好，**只记录与告警，绝不阻断**。

为什么是"观察"而不是"阻断"
--------------------------
用户 2026-11-14 明确的风险偏好：

    "如果能带来更高收益的同时要面临更大的回撤，我认为是可以多一些耐心去等待和
     接受的，毕竟有可能回撤就是机会呢？"

因此本项目对回撤采取**与胜率完全不同的处理**：

- 胜率 < 45% → ``auto_block`` **阻断告警**（因为低胜率说明策略方向错了，与回撤无关）
- 回撤触及阈值 → 本模块**只记录 + 分级告警**（因为回撤可能是待利用的机会，不是刹车信号）

用户设定的组合层 MDD 硬上限是 **25%**（2026-11-14 确认）。这个上限目前只约束
**加仓行为**（见 ``scale_in`` 的 MDD 守卫），不用于停止交易。

分级（相对上限的比例）
----------------------
==================  =========================  ==============================
当前回撤 / 25% 上限  状态                        动作
==================  =========================  ==============================
< 50%（< 12.5%）    ``ok``                      静默
50%~80%              ``watch``                   日志提示
80%~100%             ``warning``                 告警（准备，但不阻断）
>= 100%（>= 25%）    ``breach``                  告警 + 建议人工复核（仍不阻断）
==================  =========================  ==============================

数据来源
--------
账本 ``paper_state*.json`` 的两个字段：
- ``asset``          当前总权益
- ``high_water_mark`` 历史最高权益（已在账本里维护）

回撤 = 1 − asset / high_water_mark。若 ``high_water_mark`` 缺失或小于 ``asset``，
退回用传入权益曲线推导（见 :func:`drawdown_from_curve`）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger

# 项目根：src/lihu_quantify/monitor/drawdown_watch.py 向上 3 级
_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_MDD_LIMIT = 0.25        # 用户 2026-11-14 确认的组合层 MDD 硬上限
DEFAULT_OUTPUT_PATH = _ROOT / "outputs" / "drawdown_watch.json"

# 状态
STATE_OK = "ok"
STATE_WATCH = "watch"
STATE_WARNING = "warning"
STATE_BREACH = "breach"

# 分级阈值（相对 MDD 上限的比例）
RATIO_WATCH = 0.50
RATIO_WARNING = 0.80
RATIO_BREACH = 1.00

# 硬边界：本模块没有任何执行能力
POLICY = {
    "action": "只记录 + 分级告警；回撤不作为停止交易的依据（用户风险偏好）",
    "blocks_trading": False,
    "auto_close_position": False,
    "mdd_limit_scope": "仅约束加仓（scale_in 的 MDD 守卫），不约束开仓/平仓",
}


def drawdown_from_curve(equity: Iterable[float]) -> dict:
    """从权益曲线推导回撤指标。返回 current/max/peak 等。

    空序列或全非正值时返回全 None 的字典（不抛异常，不阻断巡检）。
    """
    vals = [float(x) for x in (equity or []) if x is not None]
    vals = [v for v in vals if v == v]  # 去 NaN
    if not vals:
        return {"current": None, "max": None, "peak": None, "trough_from_peak": None,
                "n": 0}
    peak = vals[0]
    max_dd = 0.0
    cur_dd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = 1.0 - v / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        cur_dd = dd
    return {
        "current": round(cur_dd, 6),
        "max": round(max_dd, 6),
        "peak": round(peak, 4),
        "last": round(vals[-1], 4),
        "n": len(vals),
    }


def _classify(drawdown: Optional[float], limit: float) -> str:
    """按回撤/上限比例分级。drawdown 为 None → ok（数据缺失不告警）。"""
    if drawdown is None or limit <= 0:
        return STATE_OK
    ratio = drawdown / limit
    if ratio >= RATIO_BREACH:
        return STATE_BREACH
    if ratio >= RATIO_WARNING:
        return STATE_WARNING
    if ratio >= RATIO_WATCH:
        return STATE_WATCH
    return STATE_OK


def _ledger_drawdown(entry: dict) -> dict:
    """单个账本 → 回撤明细。

    优先用 ``asset`` / ``high_water_mark``；缺失时尝试 ``equity_curve``。
    """
    name = str(entry.get("name") or "main")
    asset = entry.get("asset")
    hwm = entry.get("high_water_mark")
    src = "hwm"

    dd = None
    if (isinstance(asset, (int, float)) and isinstance(hwm, (int, float))
            and hwm and hwm > 0 and not isinstance(asset, bool) and not isinstance(hwm, bool)):
        # hwm 至少要不小于 asset，否则数据可疑 → 退回曲线
        if hwm >= asset:
            dd = max(0.0, 1.0 - float(asset) / float(hwm))
        else:
            src = "hwm_invalid"
    else:
        src = "missing"

    curve_dd = None
    curve_max = None
    curve = entry.get("equity_curve")
    if curve:
        info = drawdown_from_curve(curve)
        curve_dd = info["current"]
        curve_max = info["max"]
        if dd is None:
            dd = curve_dd
            src = "curve"

    return {
        "name": name,
        "asset": float(asset) if isinstance(asset, (int, float)) and not isinstance(asset, bool) else None,
        "high_water_mark": float(hwm) if isinstance(hwm, (int, float)) and not isinstance(hwm, bool) else None,
        "drawdown": round(float(dd), 6) if dd is not None else None,
        "drawdown_pct": f"{dd:.2%}" if dd is not None else "-",
        "curve_max_drawdown": round(float(curve_max), 6) if curve_max is not None else None,
        "curve_current_drawdown": round(float(curve_dd), 6) if curve_dd is not None else None,
        "source": src,
    }


@dataclass
class DrawdownReport:
    """一次回撤观察的结论（纯数据，可 JSON 化）。"""

    limit: float = DEFAULT_MDD_LIMIT
    worst: Optional[float] = None          # 各账本中最深的当前回撤
    worst_ledger: Optional[str] = None
    state: str = STATE_OK
    headroom: Optional[float] = None       # 距上限的剩余空间（占上限比例）
    ledgers: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    observed_at: Optional[str] = None
    output_path: str = ""

    def brief_line(self) -> str:
        """给日报用的一行（ok 状态 → 空串，调用方无需再判断）。"""
        if self.state == STATE_OK or self.worst is None:
            return ""
        tag = {"watch": "观察", "warning": "警告", "breach": "触及上限"}.get(self.state, self.state)
        room = f"，距 {self.limit:.0%} 上限余 {self.headroom:.0%}" if self.headroom is not None else ""
        return (f"【回撤{tag}】{self.worst_ledger} 当前回撤 {self.worst:.2%}{room}"
                f"（仅记录，不阻断交易——回撤可能是机会）")

    def as_dict(self) -> dict:
        return {
            "limit": self.limit,
            "worst": self.worst,
            "worst_ledger": self.worst_ledger,
            "state": self.state,
            "headroom": self.headroom,
            "ledgers": self.ledgers,
            "alerts": self.alerts,
            "observed_at": self.observed_at,
            "output_path": self.output_path,
            "policy": dict(POLICY),
        }


def _write(path: Path, payload: dict) -> bool:
    """原子写；失败只告警（观察模块绝不影响巡检）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"[回撤观察] 写入失败: {e}")
        return False


def watch_drawdown(
    ledgers: Iterable[dict],
    *,
    limit: float = DEFAULT_MDD_LIMIT,
    output_path=None,
    now: Optional[datetime] = None,
    notify=None,
) -> DrawdownReport:
    """观察各账本回撤并分级告警。**只读，不碰账户，不阻断任何交易。**

    Args:
        ledgers: 形如 ``[{"name": "main", "asset": 88600.0,
                  "high_water_mark": 100000.0}]``；也接受 ``equity_curve`` 列表。
        limit: MDD 上限（占净值比例），用户设定 0.25。
        output_path: 审计文件路径，默认 ``outputs/drawdown_watch.json``。
        now: 注入时间（测试可复现）。
        notify: 可选回调 ``(subject, detail)``；None → 只记 loguru。

    Returns:
        DrawdownReport。``state`` 为最差账本的分级。
    """
    limit = float(limit) if limit and limit > 0 else DEFAULT_MDD_LIMIT
    out_path = Path(output_path) if output_path is not None else DEFAULT_OUTPUT_PATH
    entries = [e for e in (ledgers or []) if isinstance(e, dict)]

    rows = [_ledger_drawdown(e) for e in entries]
    scored = [r for r in rows if r["drawdown"] is not None]
    worst_row = max(scored, key=lambda r: r["drawdown"]) if scored else None
    worst = worst_row["drawdown"] if worst_row else None
    state = _classify(worst, limit)
    headroom = round(max(0.0, 1.0 - worst / limit), 4) if worst is not None else None

    observed_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    alerts: list[dict] = []
    if state != STATE_OK and worst_row is not None:
        detail = (
            f"账本 {worst_row['name']} 当前回撤 {worst_row['drawdown_pct']}"
            f"（净值 {worst_row['asset']} / 历史高点 {worst_row['high_water_mark']}），"
            f"占 {limit:.0%} 上限的 {worst / limit:.0%}。"
        )
        subject = f"回撤{state}：{worst_row['name']} {worst_row['drawdown_pct']}"
        alerts.append({"ledger": worst_row["name"], "state": state,
                       "drawdown": worst, "subject": subject, "detail": detail})
        if notify is not None:
            try:
                notify(subject, detail)
            except Exception as e:  # noqa: BLE001 - 告警失败不得影响巡检
                logger.warning(f"[回撤观察] notify 失败: {e}")
        logger.warning(f"[回撤观察] {subject} | {detail}")

    report = DrawdownReport(
        limit=limit,
        worst=worst,
        worst_ledger=worst_row["name"] if worst_row else None,
        state=state,
        headroom=headroom,
        ledgers=rows,
        alerts=alerts,
        observed_at=observed_at,
        output_path=str(out_path),
    )
    _write(out_path, report.as_dict())
    return report

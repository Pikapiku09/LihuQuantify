"""数据健康体检（第 4 层，2026-11-14）：把本次数据事故变成每次巡检的常规检查。

为什么需要它
------------
2026-11-14 审计发现两类此前无人察觉的数据缺陷：

1. **伪交易日**：``index_daily`` 混入 ``2026-09-12``（星期六，``pct_chg=+4.73%``）。
   闸门用 ``pct_change(20)`` 按**行数**滚动、不感知日历，这行把 ``2026-10-19`` 的
   ``ret_20d`` 由 −4.04% 扭曲成 −8.38%，把"震荡"硬判成"下跌"。
2. **数据管线断裂**：``daily_quotes`` 停在 2026-08-31（缺 46 个交易日）、
   ``daily_basic`` / ``moneyflow`` / ``trade_cal`` 是**空表**，而系统一直在"正常"运行。

这两类问题都不会让程序崩溃——它们让程序**安静地算错**。所以必须主动体检。

检查项
------
======================  ==================================================  ==========
检查                    判据                                                fail 级
======================  ==================================================  ==========
伪交易日                 index_daily 中不在 trade_cal 开市日的日期            有任意 1 条
行情滞后                 index_daily 最新日 − daily_quotes 最新日 > 容忍天数    超过阈值
空表                     daily_basic / moneyflow / trade_cal 行数为 0            任一为空
交易日历覆盖              trade_cal 是否覆盖 index_daily 最新日                  未覆盖
======================  ==================================================  ==========

设计原则
--------
- **只读 + 只报告**，不修数据、不改配置。修复由人工或 ``backfill_market_data.py`` 执行。
- 任何单项检查异常都**不会**让体检本身崩掉（返回 ``unknown`` 而不是抛异常）。
- 输出走 ASCII 符号（Windows GBK 控制台），中文只出现在数据值里。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_PATH = _ROOT / "outputs" / "data_health.json"
DEFAULT_MAX_LAG_DAYS = 5          # 行情落后超过这个自然日数就报 fail

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_FAIL = "fail"
STATE_UNKNOWN = "unknown"

# 必须有数据的表（空表 = 数据管线某段没跑）
REQUIRED_TABLES = ("daily_quotes", "daily_basic", "moneyflow", "trade_cal")

POLICY = {
    "action": "只读体检 + 分级报告；不自动修复、不改配置",
    "auto_repair": False,
    "repair_hint": "scripts/backfill_market_data.py",
}


def _safe(fn, default):
    """执行单项检查；任何异常降级为 default，绝不让体检本身崩掉。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[数据体检] 单项检查异常，降级: {e}")
        return default


def _table_stats(store, table: str) -> dict:
    """单表行数与最新日期。"""
    cols = {"daily_quotes": "trade_date", "daily_basic": "trade_date",
            "moneyflow": "trade_date", "trade_cal": "cal_date",
            "index_daily": "trade_date"}
    col = cols.get(table, "trade_date")
    row = store.query(
        f"SELECT COUNT(*) AS n, MAX({col}) AS mx FROM {table}"
    ).iloc[0]
    n = int(row["n"])
    mx = row["mx"]
    return {
        "table": table,
        "rows": n,
        "latest": str(pd.Timestamp(mx).date()) if pd.notna(mx) else None,
        "empty": n == 0,
    }


def _lag_days(later: Optional[str], earlier: Optional[str]) -> Optional[int]:
    """两个 ISO 日期之间的自然日差（later − earlier）。任一为空 → None。"""
    if not later or not earlier:
        return None
    try:
        d1 = date.fromisoformat(str(later)[:10])
        d2 = date.fromisoformat(str(earlier)[:10])
    except ValueError:
        return None
    return (d1 - d2).days


@dataclass
class HealthReport:
    """一次数据体检的结论。"""

    state: str = STATE_OK
    checks: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    index_latest: Optional[str] = None
    checked_at: Optional[str] = None
    output_path: str = ""

    def failed(self) -> list[dict]:
        return [c for c in self.checks if c.get("state") == STATE_FAIL]

    def warned(self) -> list[dict]:
        return [c for c in self.checks if c.get("state") == STATE_WARN]

    def brief_line(self) -> str:
        """给日报用的一行（ok 且无 warn → 空串）。"""
        if self.state == STATE_OK and not self.warned():
            return ""
        bad = self.failed()
        if bad:
            items = "；".join(f"{c['name']}: {c['detail']}" for c in bad[:3])
            return f"【数据体检 FAIL】{len(bad)} 项：{items}"
        items = "；".join(f"{c['name']}: {c['detail']}" for c in self.warned()[:3])
        return f"【数据体检 WARN】{items}"

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "checks": self.checks,
            "tables": self.tables,
            "index_latest": self.index_latest,
            "checked_at": self.checked_at,
            "output_path": self.output_path,
            "policy": dict(POLICY),
        }

    def render(self) -> str:
        """ASCII 安全的多行文本（可直接 print 到 GBK 控制台）。"""
        lines = ["=" * 64, "数据健康体检", "=" * 64,
                 f"  overall      : {self.state}",
                 f"  index latest : {self.index_latest}",
                 f"  checked at   : {self.checked_at}"]
        for t in self.tables:
            flag = "[!]" if t["empty"] else "[ok]"
            lines.append(f"  {flag} {t['table']:<14} rows={t['rows']:>9}  latest={t['latest']}")
        for c in self.checks:
            tag = {"ok": "[ok]", "warn": "[!]", "fail": "[X]",
                   "unknown": "[?]"}.get(c["state"], "[?]")
            lines.append(f"  {tag} {c['name']:<12} {c['detail']}")
        lines.append("=" * 64)
        return "\n".join(lines)


def check_data_health(
    store,
    *,
    max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
    output_path=None,
    now: Optional[datetime] = None,
) -> HealthReport:
    """执行数据体检。**只读**，不修数据。

    Args:
        store: ``DuckDBStore``（read_only 即可）。
        max_lag_days: 行情落后超过该自然日数 → fail。
        output_path: 审计文件路径，默认 ``outputs/data_health.json``。
        now: 注入时间（测试可复现）。
    """
    out_path = Path(output_path) if output_path is not None else DEFAULT_OUTPUT_PATH
    checks: list[dict] = []

    # --- 1. 各表状态 ---
    tables = []
    for t in ("index_daily",) + REQUIRED_TABLES:
        stats = _safe(lambda: _table_stats(store, t),
                      {"table": t, "rows": 0, "latest": None, "empty": True})
        tables.append(stats)

    idx = next((t for t in tables if t["table"] == "index_daily"), {})
    index_latest = idx.get("latest")

    # --- 2. 空表检查 ---
    empty = [t["table"] for t in tables if t["table"] in REQUIRED_TABLES and t["empty"]]
    checks.append({
        "name": "空表",
        "state": STATE_FAIL if empty else STATE_OK,
        "detail": f"以下表无数据: {empty}" if empty else f"{len(REQUIRED_TABLES)} 张必需表均有数据",
    })

    # --- 3. 行情滞后 ---
    quotes = next((t for t in tables if t["table"] == "daily_quotes"), {})
    lag = _lag_days(index_latest, quotes.get("latest"))
    if lag is None:
        checks.append({"name": "行情滞后", "state": STATE_UNKNOWN,
                       "detail": "无法计算（index_daily 或 daily_quotes 最新日期缺失）"})
    else:
        st = STATE_FAIL if lag > max_lag_days else (
            STATE_WARN if lag > 0 else STATE_OK)
        checks.append({
            "name": "行情滞后",
            "state": st,
            "detail": (f"daily_quotes 落后指数 {lag} 个自然日"
                       f"（指数 {index_latest} / 行情 {quotes.get('latest')}，容忍 {max_lag_days}）"),
            "lag_days": lag,
        })

    # --- 4. 伪交易日（复用 integrity） ---
    cal = next((t for t in tables if t["table"] == "trade_cal"), {})
    cal_empty = bool(cal.get("empty", True))

    def _bad_days():
        from lihu_quantify.data.integrity import find_non_trading_days

        return find_non_trading_days(store, table="index_daily")

    bad = _safe(_bad_days, None)
    if cal_empty:
        # 关键：trade_cal 为空时 find_non_trading_days 会返回 []（表示"跳过校验"），
        # 若直接按空列表处理会被误读成"没有伪交易日"。必须显式降级为 unknown。
        checks.append({"name": "伪交易日", "state": STATE_UNKNOWN,
                       "detail": "trade_cal 为空，无法校验伪交易日（先回填交易日历）"})
    elif bad is None:
        checks.append({"name": "伪交易日", "state": STATE_UNKNOWN,
                       "detail": "校验失败或 trade_cal 不可用"})
    else:
        checks.append({
            "name": "伪交易日",
            "state": STATE_FAIL if bad else STATE_OK,
            "detail": (f"index_daily 含 {len(bad)} 条非开市日: "
                       f"{[str(d) for d in bad[:5]]}") if bad else "未发现伪交易日",
            "bad_days": [str(d) for d in bad],
        })

    # --- 5. 交易日历覆盖 ---
    cal_latest = cal.get("latest")
    if not cal_latest or not index_latest:
        checks.append({"name": "日历覆盖", "state": STATE_UNKNOWN,
                       "detail": "trade_cal 或 index_daily 最新日期缺失"})
    else:
        covered = str(cal_latest)[:10] >= str(index_latest)[:10]
        checks.append({
            "name": "日历覆盖",
            "state": STATE_OK if covered else STATE_WARN,
            "detail": (f"trade_cal 至 {cal_latest}，index_daily 至 {index_latest}"
                       + ("（已覆盖）" if covered else "（未覆盖，伪日校验会漏报）")),
        })

    # --- 汇总 ---
    states = [c["state"] for c in checks]
    if STATE_FAIL in states:
        overall = STATE_FAIL
    elif STATE_UNKNOWN in states:
        overall = STATE_WARN
    elif STATE_WARN in states:
        overall = STATE_WARN
    else:
        overall = STATE_OK

    report = HealthReport(
        state=overall,
        checks=checks,
        tables=tables,
        index_latest=index_latest,
        checked_at=(now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        output_path=str(out_path),
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, out_path)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"[数据体检] 写入失败: {e}")

    if report.state == STATE_FAIL:
        logger.warning(f"[数据体检] FAIL: {report.brief_line()}")
    elif report.state == STATE_WARN:
        logger.info(f"[数据体检] WARN: {report.brief_line()}")
    return report


if __name__ == "__main__":  # pragma: no cover - 手动入口
    import sys

    _root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(_root / "src"))
    from lihu_quantify.data.duckdb_store import DuckDBStore

    _store = DuckDBStore(_root / "data" / "lihu_quant.duckdb", read_only=True)
    try:
        _r = check_data_health(_store)
        print(_r.render())
    finally:
        _store.close()

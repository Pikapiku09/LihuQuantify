"""数据完整性校验：交易日对齐、伪交易日检测与过滤。

背景（2026-11-14 数据审计）
---------------------------
本地 ``index_daily``（000001.SH）中混入一条**伪交易日**：

===================  ==========================================
日期                 2026-09-12（**星期六**，trade_cal is_open=0）
close                4072.07
pct_chg              **+4.7313%**
前一交易日 2026-09-11  3888.11
后一交易日 2026-09-14  3885.33
===================  ==========================================

即"周六凭空涨 4.73%，周一又跌 4.59% 回去"的脏数据尖峰。

为什么必须处理
--------------
``market.classify_market_state`` 用 ``pct_change(window)`` 与 ``rolling(window).mean()``，
两者都**按行数**滚动，不感知日历。多出这一行会导致：

1. ``ret_20d`` 在 2026-09-12 之后第 20 个交易日（= ``2026-10-19``）以 4072.07 为基准，
   实测该日 ret_20d 由真实约 −3.9% 被扭曲成 **−8.38%**，把"震荡"硬判成"下跌"。
2. ``MA20`` 在 2026-09-12 ~ 2026-10-16 区间被 4072 这个高点抬升。
3. ``MA5`` / ``ret_5d`` 在 2026-09-12 ~ 2026-09-18 被尖峰污染，可能误触发"反转确认"。

处理原则
--------
**不修改原始行情表**（保留证据，便于回溯），而是提供"只读校验 + 显式过滤"：

- :func:`find_non_trading_days` —— 找出表里不在交易日历中的日期
- :func:`validate_and_filter` —— 过滤 DataFrame 后再喂给闸门/指标计算
- :func:`audit_index_daily` —— 一次性体检，返回可直接打印的诊断结果

用法::

    from lihu_quantify.data.integrity import audit_index_daily, validate_and_filter

    report = audit_index_daily(store)          # 体检
    df = store.get_index_daily(...)
    df, dropped = validate_and_filter(store, df)   # 过滤后再算指标
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from loguru import logger

# 需要交易日校验的行情表 → 日期列
VALIDATED_TABLES: dict[str, str] = {
    "index_daily": "trade_date",
    "daily_quotes": "trade_date",
    "daily_basic": "trade_date",
    "moneyflow": "trade_date",
    "weekly_quotes": "trade_date",
    "adj_factors": "trade_date",
}


def _calendar_bounds(store) -> tuple[date | None, date | None]:
    """trade_cal 的覆盖范围（只有范围内的日期才能判定是否交易日）。"""
    try:
        row = store.query(
            "SELECT MIN(cal_date) AS mn, MAX(cal_date) AS mx FROM trade_cal"
        ).iloc[0]
    except Exception:
        return None, None
    mn, mx = row["mn"], row["mx"]
    if pd.isna(mn) or pd.isna(mx):
        return None, None
    return pd.Timestamp(mn).date(), pd.Timestamp(mx).date()


def load_trading_days(store) -> set[date]:
    """读取 trade_cal 中所有开市日。trade_cal 为空时返回空集合。"""
    try:
        df = store.query("SELECT cal_date FROM trade_cal WHERE is_open = 1")
    except Exception as exc:
        logger.warning(f"读取 trade_cal 失败: {exc}")
        return set()
    if df.empty:
        return set()
    return {pd.Timestamp(d).date() for d in df["cal_date"].tolist()}


def find_non_trading_days(
    store,
    table: str = "index_daily",
    date_col: str | None = None,
    trading_days: set[date] | None = None,
) -> list[date]:
    """找出表中落在 trade_cal 覆盖范围内、却不是交易日的日期（升序）。

    trade_cal 为空或覆盖范围不足时返回空列表并告警——**宁可漏报也不误删数据**。
    """
    date_col = date_col or VALIDATED_TABLES.get(table, "trade_date")
    trading_days = trading_days if trading_days is not None else load_trading_days(store)
    if not trading_days:
        logger.warning("trade_cal 为空，跳过交易日校验（先跑 scripts/backfill_market_data.py --tables trade_cal）")
        return []

    lo, hi = _calendar_bounds(store)
    if lo is None:
        return []

    df = store.query(
        f"SELECT DISTINCT {date_col} AS d FROM {table} "
        f"WHERE {date_col} BETWEEN ? AND ? ORDER BY {date_col}",
        [lo, hi],
    )
    if df.empty:
        return []
    bad = [pd.Timestamp(d).date() for d in df["d"].tolist()]
    return [d for d in bad if d not in trading_days]


def filter_non_trading_days(
    df: pd.DataFrame,
    trading_days: set[date] | Iterable[date],
    date_col: str = "trade_date",
) -> tuple[pd.DataFrame, list[date]]:
    """**纯函数**版过滤：只依赖传入的交易日集合，不碰数据库。

    给 ``scheduler._market_state``（数据刚从 Tushare 拉、尚未落库）和
    ``web/server.py``（只有 read_only 连接）这类调用方用。

    ``trading_days`` 为空 → 原样返回（宁可漏报也不误删）。

    **只在日历覆盖区间内判定**：``trade_cal`` 往往只回填了最近几年，而调用方
    （如 ``scheduler._market_state``）传入的是全历史 8761 行。范围外的日期
    无法判定是否为交易日，一律**保留**——否则会把 1990~2024 的历史行情全部误删。
    """
    if df is None or df.empty or date_col not in df.columns:
        return df, []
    day_set = set(trading_days or ())
    if not day_set:
        return df, []
    dates = pd.to_datetime(df[date_col]).dt.date
    lo, hi = min(day_set), max(day_set)
    in_range = (dates >= lo) & (dates <= hi)
    # 范围外 → 保留（无法判定）；范围内 → 必须落在开市日集合里
    mask = (~in_range) | dates.isin(day_set)
    if mask.all():
        return df, []
    dropped = sorted(set(dates[~mask]))
    if dropped:
        logger.warning(
            f"剔除 {len(dropped)} 条非交易日数据 {[str(d) for d in dropped]}"
            f"（trade_cal 判定为非开市日，日历覆盖 {lo}~{hi}，疑似数据源伪 bar）"
        )
    return df.loc[mask].copy(), dropped


def validate_and_filter(
    store,
    df: pd.DataFrame,
    date_col: str = "trade_date",
    table: str = "index_daily",
) -> tuple[pd.DataFrame, list[date]]:
    """过滤掉非交易日行，返回 ``(清洗后 df, 被剔除的日期列表)``。

    DataFrame 为空或 trade_cal 不可用时原样返回（不阻断下游）。
    """
    if df is None or df.empty or date_col not in df.columns:
        return df, []
    bad = set(find_non_trading_days(store, table=table, date_col=date_col))
    if not bad:
        return df, []
    mask = ~pd.to_datetime(df[date_col]).dt.date.isin(bad)
    dropped = sorted(set(pd.to_datetime(df.loc[~mask, date_col]).dt.date))
    if dropped:
        logger.warning(
            f"{table}: 剔除 {len(dropped)} 条非交易日数据 {[str(d) for d in dropped]}"
        )
    return df.loc[mask].copy(), dropped


def audit_index_daily(store) -> dict:
    """指数日线体检，返回可直接打印/落盘的诊断字典。

    检查项：
    1. 伪交易日（不在 trade_cal 开市日中）
    2. 与交易日历的缺口（trade_cal 有开市、index_daily 无数据）
    3. 极端单日涨跌幅（|pct_chg| > 7%，指数层面罕见，用于人工复核）
    """
    result: dict = {
        "table": "index_daily",
        "trade_cal_days": 0,
        "index_days": 0,
        "non_trading_days": [],
        "missing_trading_days": [],
        "extreme_moves": [],
    }
    trading_days = load_trading_days(store)
    result["trade_cal_days"] = len(trading_days)
    if not trading_days:
        result["note"] = "trade_cal 为空，无法校验；先回填交易日历"
        return result

    lo, hi = _calendar_bounds(store)
    idx = store.query(
        "SELECT DISTINCT trade_date FROM index_daily WHERE trade_date BETWEEN ? AND ?",
        [lo, hi],
    )
    idx_days = {pd.Timestamp(d).date() for d in idx["trade_date"].tolist()}
    result["index_days"] = len(idx_days)
    result["non_trading_days"] = [
        str(d) for d in sorted(idx_days - trading_days)
    ]
    cal_in_range = {d for d in trading_days if lo <= d <= hi}
    result["missing_trading_days"] = [
        str(d) for d in sorted(cal_in_range - idx_days)
    ]

    ext = store.query(
        "SELECT trade_date, close, pct_chg FROM index_daily "
        "WHERE trade_date BETWEEN ? AND ? AND ABS(pct_chg) > 7 ORDER BY trade_date",
        [lo, hi],
    )
    result["extreme_moves"] = [
        {
            "trade_date": str(pd.Timestamp(r.trade_date).date()),
            "close": float(r.close),
            "pct_chg": float(r.pct_chg),
        }
        for r in ext.itertuples()
    ]
    result["coverage"] = [str(lo), str(hi)]
    return result


def format_audit(report: dict) -> str:
    """把 :func:`audit_index_daily` 的结果格式化成可打印文本。

    注意：本函数输出会打到 **stdout**，而 Windows 控制台默认 GBK，
    非 ASCII 符号（✗ / ⚠ 等）会触发 ``UnicodeEncodeError``。故此处一律用
    ASCII 标记（``[X]`` / ``[OK]`` / ``[!]``）。loguru 走 stderr 无此风险。
    """
    lines = [
        "=" * 62,
        "index_daily 数据完整性体检",
        "=" * 62,
        f"  交易日历天数  : {report.get('trade_cal_days', 0)}",
        f"  日历覆盖范围  : {report.get('coverage')}",
        f"  指数有数据天数: {report.get('index_days', 0)}",
    ]
    bad = report.get("non_trading_days") or []
    tag_bad = "[X]" if bad else "[OK]"
    lines.append(f"  {tag_bad} 伪交易日    : {len(bad)}" + (f"  {bad}" if bad else ""))
    miss = report.get("missing_trading_days") or []
    tag_miss = "[X]" if miss else "[OK]"
    if len(miss) > 10:
        miss_s = f"  {miss[:10]}..."
    else:
        miss_s = f"  {miss}" if miss else ""
    lines.append(f"  {tag_miss} 缺失交易日  : {len(miss)}" + miss_s)
    ext = report.get("extreme_moves") or []
    lines.append(f"  [!] 极端涨跌(>7%): {len(ext)}")
    for e in ext:
        lines.append(f"      {e['trade_date']}  close={e['close']:.2f}  pct_chg={e['pct_chg']:+.2f}%")
    if report.get("note"):
        lines.append(f"  注: {report['note']}")
    lines.append("=" * 62)
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - 手动体检入口
    import sys
    from pathlib import Path

    _root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(_root / "src"))
    from lihu_quantify.data.duckdb_store import DuckDBStore

    _store = DuckDBStore(_root / "data" / "lihu_quant.duckdb", read_only=True)
    try:
        print(format_audit(audit_index_daily(_store)))
    finally:
        _store.close()

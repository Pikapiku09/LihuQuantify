"""补数脚本：按交易日批量回填 DuckDB 行情 / 基本面 / 资金流 / 交易日历。

背景（2026-11-14 诊断）
-----------------------
本地 ``data/lihu_quant.duckdb`` 的实际状态：

- ``daily_quotes`` : 仅 200 只股票，最大 ``trade_date`` = 2026-08-31 → 缺 46 个交易日
  （2026-09-01 ~ 2026-11-10，恰好是实盘亏损最重的区间，导致策略级回测无法覆盖）
- ``daily_basic`` / ``moneyflow`` / ``trade_cal`` / ``limit_list_d`` / ``sw_daily`` : 空表
- ``index_daily`` : 完整（000001.SH，1990-12-19 ~ 2026-11-10）
- ``adj_factors`` : 完整到 2026-11-10

同时确认 Tushare token **有效**（经 ``Settings.resolved_tushare_token()`` 解析，
五个接口全部 ``code=0``）。此前"token 失效"的结论是误诊——那是把整个 MCP JSON
配置文件当作 token 字符串传给接口所致。

设计要点
--------
1. **按交易日拉取**而非按股票：单次 ``daily(trade_date=...)`` 返回全市场约 5500 行，
   比逐股调用（200 次）省 200 倍请求。
2. **只保留已在库的股票池**（默认开启）：避免把 5500+ 只全市场数据灌进来把 23MB 的库撑大，
   也保证与既有回测口径（200 只）一致。
3. **幂等**：全部走 ``INSERT OR REPLACE``，可重复运行。
4. **断点友好**：逐日提交，中断后重跑只补缺失日。

用法::

    # 全量补齐 9~11 月（默认三张行情表 + 交易日历）
    .venv\\Scripts\\python.exe scripts\\backfill_market_data.py

    # 指定区间
    .venv\\Scripts\\python.exe scripts\\backfill_market_data.py --start 2026-09-01 --end 2026-11-10

    # 只补交易日历
    .venv\\Scripts\\python.exe scripts\\backfill_market_data.py --tables trade_cal

    # 保留全市场（覆盖所有 5500+ 只）
    .venv\\Scripts\\python.exe scripts\\backfill_market_data.py --full-universe
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.config import get_settings  # noqa: E402
from lihu_quantify.data.duckdb_store import DuckDBStore  # noqa: E402
from lihu_quantify.data.tushare_client import TushareClient  # noqa: E402

DB_PATH = ROOT / "data" / "lihu_quant.duckdb"
CACHE_DIR = ROOT / "data" / "cache"

# 需要按 trade_date 全市场拉取的接口 → (Tushare 接口名, 目标表, 字段重命名映射)
BY_DATE_TABLES: dict[str, tuple[str, str, dict[str, str]]] = {
    "daily": ("daily", "daily_quotes", {}),
    "daily_basic": ("daily_basic", "daily_basic", {}),
    # Tushare moneyflow 返回 net_mf_amount，本仓库表结构叫 net_amount，需改名
    "moneyflow": ("moneyflow", "moneyflow", {"net_mf_amount": "net_amount"}),
    "limit_list_d": ("limit_list_d", "limit_list_d", {}),
}

DEFAULT_TABLES = "trade_cal,daily,daily_basic,moneyflow"


def build_client() -> TushareClient:
    """按项目的官方解析路径取 token（兼容纯文本与 MCP JSON 两种格式）。"""
    settings = get_settings()
    token = settings.resolved_tushare_token()
    if not token:
        raise SystemExit("Tushare token 解析为空，请检查 config/settings.yaml 的 token / token_file")
    return TushareClient(token=token, cache_dir=CACHE_DIR)


def target_trading_days(store: DuckDBStore, start: date, end: date) -> list[date]:
    """目标交易日列表。

    优先用本地 ``index_daily`` 的日期（已完整到 2026-11-10），
    这样可避免依赖空的 ``trade_cal`` 表，且与闸门判定同源。
    """
    df = store.query(
        "SELECT DISTINCT trade_date FROM index_daily "
        "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end],
    )
    if not df.empty:
        return [pd.Timestamp(d).date() for d in df["trade_date"].tolist()]

    logger.warning("index_daily 在该区间无数据，回落到 Tushare trade_cal")
    return []


def backfill_trade_cal(client: TushareClient, store: DuckDBStore, start: date, end: date) -> int:
    """回填交易日历（SSE）。"""
    df = client.query(
        "trade_cal",
        {
            "exchange": "SSE",
            "start_date": start.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
        },
    )
    if df.empty:
        logger.warning("trade_cal 返回空")
        return 0
    # 表结构：cal_date, is_open, pretrade_date（主键 cal_date）
    keep = [c for c in ("cal_date", "is_open", "pretrade_date") if c in df.columns]
    df = df[keep]
    n = store.upsert("trade_cal", df, date_cols=("cal_date", "pretrade_date"))
    logger.info(f"trade_cal 写入 {n} 行（{start} ~ {end}）")
    return n


def backfill_by_date(
    client: TushareClient,
    store: DuckDBStore,
    api: str,
    table: str,
    rename: dict[str, str],
    days: list[date],
    universe: set[str] | None,
    sleep_s: float,
) -> dict:
    """对每个交易日拉一次全市场数据，过滤股票池后 upsert。返回统计。"""
    total_rows = 0
    ok_days = 0
    failed: list[str] = []
    for i, d in enumerate(days, 1):
        ds = d.strftime("%Y%m%d")
        try:
            df = client.query(api, {"trade_date": ds})
        except Exception as exc:  # 单日失败不应中断整体回填
            logger.error(f"{api} {ds} 拉取失败: {type(exc).__name__}: {exc}")
            failed.append(ds)
            continue
        if df is None or df.empty:
            logger.warning(f"{api} {ds} 返回空（可能非交易日）")
            continue
        if rename:
            df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if universe is not None and "ts_code" in df.columns:
            before = len(df)
            df = df[df["ts_code"].isin(universe)]
            logger.debug(f"{api} {ds} 全市场 {before} → 池内 {len(df)}")
        if df.empty:
            continue
        n = store.upsert(table, df)
        total_rows += n
        ok_days += 1
        if i % 10 == 0 or i == len(days):
            logger.info(f"{api} 进度 {i}/{len(days)}  累计 {total_rows} 行  最后 {ds}")
        time.sleep(sleep_s)
    return {"table": table, "rows": total_rows, "days_ok": ok_days, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按交易日批量回填 DuckDB")
    parser.add_argument("--start", default="2026-09-01", help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-11-10", help="结束交易日 YYYY-MM-DD")
    parser.add_argument(
        "--tables",
        default=DEFAULT_TABLES,
        help=f"逗号分隔，可选：trade_cal,{','.join(BY_DATE_TABLES)}",
    )
    parser.add_argument(
        "--full-universe",
        action="store_true",
        help="保留全市场约 5500 只（默认只保留库中已有股票池，防库膨胀）",
    )
    parser.add_argument("--sleep", type=float, default=0.35, help="每日请求间隔秒（默认 0.35）")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = [t for t in tables if t != "trade_cal" and t not in BY_DATE_TABLES]
    if unknown:
        print(f"未知表: {unknown}，可选: trade_cal, {list(BY_DATE_TABLES)}", file=sys.stderr)
        return 2

    logger.info(f"目标库: {DB_PATH}")
    logger.info(f"区间: {start} ~ {end}   表: {tables}")

    client = build_client()
    store = DuckDBStore(DB_PATH)

    try:
        # 交易日列表（start ~ end 之外也顺带补日历，日历本身范围放宽到整年）
        if "trade_cal" in tables:
            backfill_trade_cal(client, store, date(start.year, 1, 1), date(end.year, 12, 31))

        days = target_trading_days(store, start, end)
        if not days:
            logger.error("没有可用交易日（index_daily 在区间内为空）")
            return 1
        logger.info(f"待回填交易日 {len(days)} 个：{days[0]} ~ {days[-1]}")

        universe: set[str] | None = None
        if not args.full_universe:
            uni_df = store.query("SELECT DISTINCT ts_code FROM daily_quotes")
            universe = set(uni_df["ts_code"].tolist())
            logger.info(f"股票池锁定为库中已有 {len(universe)} 只（--full-universe 可放开）")

        results = []
        for t in tables:
            if t == "trade_cal":
                continue
            api, table, rename = BY_DATE_TABLES[t]
            logger.info(f"===== 回填 {table}（{api}）=====")
            results.append(
                backfill_by_date(client, store, api, table, rename, days, universe, args.sleep)
            )

        print("\n" + "=" * 62)
        print("回填汇总")
        print("=" * 62)
        for r in results:
            flag = f"  ⚠ 失败 {len(r['failed'])} 天: {r['failed'][:5]}" if r["failed"] else ""
            print(f"  {r['table']:14s} {r['rows']:>8d} 行   成功 {r['days_ok']} 天{flag}")
        print("\n复查（query() 返回 DataFrame，取 iloc[0] 而非 fetchone）：")
        checks = [(t[1], "trade_date") for t in BY_DATE_TABLES.values()]
        checks.append(("trade_cal", "cal_date"))
        for tbl, col in checks:
            try:
                row = store.query(
                    f"SELECT COUNT(*) AS n, MIN({col}) AS mn, MAX({col}) AS mx FROM {tbl}"
                ).iloc[0]
                print(f"  {tbl:14s} n={int(row['n']):>8d}  {row['mn']} ~ {row['mx']}")
            except Exception as exc:
                print(f"  {tbl:14s} 复查失败: {str(exc)[:80]}")
        print("=" * 62)
        return 0 if not any(r["failed"] for r in results) else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

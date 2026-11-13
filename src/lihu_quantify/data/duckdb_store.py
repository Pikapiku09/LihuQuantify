"""DuckDB 存储：建表 + 增量 upsert + 查询助手。

Schema 设计见 docs/ARCHITECTURE.md §4.2。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd
from loguru import logger

# 全部建表 SQL（IF NOT EXISTS 保证幂等）
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_quotes (
    ts_code VARCHAR, trade_date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
    change DOUBLE, pct_chg DOUBLE, vol DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code VARCHAR, trade_date DATE,
    pe DOUBLE, pe_ttm DOUBLE, pb DOUBLE, total_mv DOUBLE, circ_mv DOUBLE,
    turnover_rate DOUBLE, PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS weekly_quotes (
    ts_code VARCHAR, trade_date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
    change DOUBLE, pct_chg DOUBLE, vol DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS index_daily (
    ts_code VARCHAR, trade_date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
    change DOUBLE, pct_chg DOUBLE, vol DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS sw_daily (
    ts_code VARCHAR, trade_date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
    change DOUBLE, pct_chg DOUBLE, vol DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS limit_list_d (
    trade_date DATE, ts_code VARCHAR, name VARCHAR, close DOUBLE,
    pct_chg DOUBLE, amp DOUBLE, fc DOUBLE, fp DOUBLE, flp DOUBLE,
    "limit" VARCHAR, PRIMARY KEY (trade_date, ts_code)
);
CREATE TABLE IF NOT EXISTS moneyflow (
    ts_code VARCHAR, trade_date DATE,
    buy_sm_amount DOUBLE, sell_sm_amount DOUBLE,
    buy_md_amount DOUBLE, sell_md_amount DOUBLE,
    buy_lg_amount DOUBLE, sell_lg_amount DOUBLE,
    buy_elg_amount DOUBLE, sell_elg_amount DOUBLE,
    net_amount DOUBLE, PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS moneyflow_dc (
    ts_code VARCHAR, trade_date DATE,
    buy_sm_amount DOUBLE, sell_sm_amount DOUBLE,
    buy_md_amount DOUBLE, sell_md_amount DOUBLE,
    buy_lg_amount DOUBLE, sell_lg_amount DOUBLE,
    buy_elg_amount DOUBLE, sell_elg_amount DOUBLE,
    net_amount DOUBLE, PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS income (
    ts_code VARCHAR, end_date DATE, ann_date DATE,
    revenue DOUBLE, n_income DOUBLE, PRIMARY KEY (ts_code, end_date)
);
CREATE TABLE IF NOT EXISTS fina_indicator (
    ts_code VARCHAR, end_date DATE, ann_date DATE,
    roe DOUBLE, grossprofit_margin DOUBLE, PRIMARY KEY (ts_code, end_date)
);
CREATE TABLE IF NOT EXISTS forecast (
    ts_code VARCHAR, end_date DATE, ann_date DATE, type VARCHAR,
    p_change_min DOUBLE, p_change_max DOUBLE,
    profit_min DOUBLE, profit_max DOUBLE
);
CREATE TABLE IF NOT EXISTS express (
    ts_code VARCHAR, end_date DATE, revenue DOUBLE,
    n_income DOUBLE, yoy_net_profit DOUBLE
);
CREATE TABLE IF NOT EXISTS major_news (
    trade_date DATE, ts_code VARCHAR, title VARCHAR,
    content VARCHAR, pub_time TIMESTAMP
);
CREATE TABLE IF NOT EXISTS trade_cal (
    cal_date DATE, is_open INT, pretrade_date DATE, PRIMARY KEY (cal_date)
);
CREATE TABLE IF NOT EXISTS fund_daily (
    ts_code VARCHAR, trade_date DATE,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, pre_close DOUBLE,
    change DOUBLE, pct_chg DOUBLE, vol DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code VARCHAR PRIMARY KEY, name VARCHAR, area VARCHAR, industry VARCHAR,
    list_date DATE, delist_date DATE
);
CREATE TABLE IF NOT EXISTS adj_factors (
    ts_code VARCHAR, trade_date DATE,
    adj_factor DOUBLE, PRIMARY KEY (ts_code, trade_date)
);
"""


def _parse_trade_date(val) -> date | None:
    """trade_date 兼容 '20260818' / '2026-08-18' / date → date。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    if s.isdigit() and len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    # 尝试 ISO
    try:
        return pd.to_datetime(s).date()
    except (ValueError, TypeError):
        return None


class DuckDBStore:
    """DuckDB 存储。

    read_only（第十一轮 P2-4）：web/只读侧用单例连接 str；
    支持 with 语法（__enter__/__exit__），退出自动 close，防连接泄漏。
    """

    def __init__(self, db_path: Path | str = "./data/lihu_quant.duckdb",
                 read_only: bool = False):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(self.db_path), read_only=read_only)
        self._read_only = read_only
        if not read_only:
            self._init_schema()
        else:
            # 只读连接不写库，但 schema 由写侧保证；此处尝试预读以触发锁（可忽略）
            try:
                self._con.execute("SELECT 1")
            except Exception as e:  # 锁冲突/文件缺失 → 上层据连接异常降级
                logger.warning(f"DuckDB 只读连接失败: {e}")
        self._register_date_parser()

    def __enter__(self) -> "DuckDBStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._con.execute(SCHEMA_SQL)
        logger.debug(f"DuckDB schema 初始化完成: {self.db_path}")

    def _register_date_parser(self) -> None:
        """注册 trade_date 字符串 → date 的转换的提示。"""
        # DuckDB 自带 CAST，但 pandas to_sql 写入会用 object 列；
        # 在 upsert 前由 _normalize 处理。

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        return self._con

    def normalize_df(self, df: pd.DataFrame, date_cols: Iterable[str] = ("trade_date",)) -> pd.DataFrame:
        """把 trade_date 等字符串列规范为 date 类型。"""
        if df.empty:
            return df
        df = df.copy()
        for col in date_cols:
            if col in df.columns:
                df[col] = df[col].apply(_parse_trade_date)
        return df

    def upsert(self, table: str, df: pd.DataFrame, date_cols: tuple = ("trade_date",)) -> int:
        """INSERT OR REPLACE 增量写入。返回写入行数。"""
        if df is None or df.empty:
            return 0
        df = self.normalize_df(df, date_cols)
        # 只保留表中已有的列（避免 Tushare 返回多列时 Binder 报错）
        table_cols = self._columns(table)
        if table_cols:
            df = df[[c for c in df.columns if c in table_cols]]
        if df.empty or len(df.columns) == 0:
            return 0
        # 显式列出列名，避免列数不匹配（df 列数 ≤ 表列数时也能正确插入）
        col_list = ", ".join(f'"{c}"' for c in df.columns)
        try:
            self._con.register("_upsert_df", df)
            self._con.execute(
                f"INSERT OR REPLACE INTO {table} ({col_list}) "
                f"SELECT {col_list} FROM _upsert_df"
            )
            self._con.unregister("_upsert_df")
            logger.debug(f"{table} upsert {len(df)} 行")
            return len(df)
        except Exception as e:
            self._con.unregister("_upsert_df")
            logger.error(f"upsert {table} 失败: {e}")
            raise

    def query(self, sql: str, params: dict | None = None) -> pd.DataFrame:
        """执行 SQL 查询，返回 DataFrame。"""
        return self._con.execute(sql, params or {}).df()

    def get_latest_trade_date(self, ts_code: str = "000001.SH") -> date | None:
        """从 index_daily 取最大 trade_date（交易日锚定铁律的本地版本）。"""
        df = self._con.execute(
            "SELECT max(trade_date) AS latest FROM index_daily WHERE ts_code = ?",
            [ts_code],
        ).df()
        if df.empty or df["latest"].iloc[0] is None:
            return None
        val = df["latest"].iloc[0]
        if pd.isna(val):
            return None
        return _parse_trade_date(val)

    def get_daily(
        self, ts_code: str, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        """取日线行情，按 trade_date 升序。"""
        sql = "SELECT * FROM daily_quotes WHERE ts_code = ?"
        params: list = [ts_code]
        if start is not None:
            sql += " AND trade_date >= ?"
            params.append(start)
        if end is not None:
            sql += " AND trade_date <= ?"
            params.append(end)
        sql += " ORDER BY trade_date ASC"
        df = self._con.execute(sql, params).df()
        return df

    def get_latest_n(self, ts_code: str, n: int, table: str = "daily_quotes") -> pd.DataFrame:
        """取最近 n 根日线（升序返回，便于指标计算）。"""
        sql = (
            f"SELECT * FROM (SELECT * FROM {table} WHERE ts_code = ? "
            f"ORDER BY trade_date DESC LIMIT ?) ORDER BY trade_date ASC"
        )
        return self._con.execute(sql, [ts_code, n]).df()

    def count_rows(self, table: str, ts_code: str | None = None) -> int:
        sql = f"SELECT count(*) AS c FROM {table}"
        params: list = []
        if ts_code is not None and "ts_code" in self._columns(table):
            sql += " WHERE ts_code = ?"
            params.append(ts_code)
        df = self._con.execute(sql, params).df()
        return int(df["c"].iloc[0])

    def _columns(self, table: str) -> set[str]:
        df = self._con.execute(f"DESCRIBE {table}").df()
        return set(df["column_name"].tolist())

    def close(self) -> None:
        self._con.close()

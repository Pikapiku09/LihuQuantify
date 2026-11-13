"""数据管理器：统一入口，先查 DuckDB 未命中则拉 Tushare 增量。

实现"数据覆盖铁律"：分析对象必须实际取到真实行情后才能给具体价格。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

import pandas as pd
from loguru import logger

from .duckdb_store import DuckDBStore
from .tushare_client import TushareClient


class DataManager:
    """对外统一数据入口。"""

    def __init__(self, client: TushareClient, store: DuckDBStore):
        self.client = client
        self.store = store

    # ===== 交易日锚定 =====

    def get_latest_trade_date(self) -> date:
        """交易日锚定铁律：优先 DuckDB，未命中则 Tushare index_daily。"""
        cached = self.store.get_latest_trade_date("000001.SH")
        if cached is not None:
            return cached
        # 远程拉取并落库（第四轮清单3 补充修复：固定参数的永久缓存会卡旧交易日，直连）
        logger.info("DuckDB 无 000001.SH 行情，从 Tushare 拉取")
        df = self.client.query(
            "index_daily", {"ts_code": "000001.SH", "end_date": "20301231"},
            use_cache=False,
        )
        if not df.empty:
            self.store.upsert("index_daily", df)
        latest = self.store.get_latest_trade_date("000001.SH")
        if latest is None:
            raise RuntimeError("无法获取真实最新交易日")
        logger.info(f"真实最新交易日: {latest}")
        return latest

    # ===== 日线行情 =====

    def ensure_daily(
        self,
        ts_code: str,
        days: int = 120,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """确保本地有 ts_code 最近 days 个自然日的日线，不足则增量拉取。"""
        if end_date is None:
            end_date = self.get_latest_trade_date()
        start_date = end_date - timedelta(days=days)

        local = self.store.get_daily(ts_code, start=start_date, end=end_date)
        if not local.empty and pd.Timestamp(local["trade_date"].max()).date() >= end_date:
            return local

        logger.info(f"增量拉取 {ts_code} daily {start_date}~{end_date}")
        df = self.client.query(
            "daily",
            {
                "ts_code": ts_code,
                "start_date": start_date.strftime("%Y%m%d"),
                "end_date": end_date.strftime("%Y%m%d"),
            },
        )
        if not df.empty:
            self.store.upsert("daily_quotes", df)
        return self.store.get_daily(ts_code, start=start_date, end=end_date)

    def get_daily(self, ts_code: str, start: date, end: date) -> pd.DataFrame:
        """直接按区间取日线（要求已 ensure 过）。"""
        return self.store.get_daily(ts_code, start=start, end=end)

    def get_daily_adjusted(self, ts_code: str, start: date, end: date) -> pd.DataFrame:
        """取日线并做前复权（第十一轮 P1-1）。

        先取本地日线；若本地缺复权因子则拉取入库，返回前复权后的 DataFrame。
        复权失败（如接口无权限）时原样返回未复权数据，不影响下游使用。
        """
        from .adjustment import apply_forward_adjustment

        df = self.store.get_daily(ts_code, start=start, end=end)
        if df.empty:
            return df
        # 本地复权因子覆盖检查
        local_adj = self.store.query(
            "SELECT trade_date, adj_factor FROM adj_factors "
            "WHERE ts_code = ? AND trade_date BETWEEN ? AND ?",
            [ts_code, start, end],
        )
        if (local_adj.empty or local_adj["adj_factor"].isna().all()):
            logger.info(f"拉取 {ts_code} 复权因子 {start.strftime('%Y%m%d')}~{end.strftime('%Y%m%d')}")
            adj = self.client.query(
                "adj_factor",
                {
                    "ts_code": ts_code,
                    "start_date": start.strftime("%Y%m%d"),
                    "end_date": end.strftime("%Y%m%d"),
                },
            )
            if not adj.empty:
                self.store.upsert("adj_factors", adj)
                local_adj = adj.copy()
        return apply_forward_adjustment(df, local_adj)

    # ===== 辅助接口 =====

    def ensure_daily_basic(self, ts_code: str, days: int = 120) -> pd.DataFrame:
        """确保本地有最近 days 天 daily_basic，不足则增量拉取（P2-5 本地优先）。

        复制 ensure_daily 模式：先查本地 max(trade_date)>=end_date 则跳过，
        避免每次调用空发 200 次请求顶积分限流。
        """
        end_date = self.get_latest_trade_date()
        start_date = end_date - timedelta(days=days)

        local = self.store.query(
            "SELECT * FROM daily_basic WHERE ts_code = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date ASC",
            [ts_code, start_date, end_date],
        )
        _local_max = local["trade_date"].max()
        if not local.empty and pd.Timestamp(_local_max).date() >= end_date:
            return local

        logger.info(f"增量拉取 {ts_code} daily_basic {start_date}~{end_date}")
        df = self.client.query(
            "daily_basic",
            {
                "ts_code": ts_code,
                "start_date": start_date.strftime("%Y%m%d"),
                "end_date": end_date.strftime("%Y%m%d"),
            },
        )
        if not df.empty:
            self.store.upsert("daily_basic", df)
        return self.store.query(
            "SELECT * FROM daily_basic WHERE ts_code = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date ASC",
            [ts_code, start_date, end_date],
        )

    def ensure_moneyflow(self, ts_code: str, days: int = 60) -> pd.DataFrame:
        """确保本地有最近 days 天 moneyflow，不足则增量拉取（P2-5 本地优先）。"""
        end_date = self.get_latest_trade_date()
        start_date = end_date - timedelta(days=days)

        local = self.store.query(
            "SELECT * FROM moneyflow WHERE ts_code = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date ASC",
            [ts_code, start_date, end_date],
        )
        _local_max = local["trade_date"].max()
        if not local.empty and pd.Timestamp(_local_max).date() >= end_date:
            return local

        logger.info(f"增量拉取 {ts_code} moneyflow {start_date}~{end_date}")
        df = self.client.query(
            "moneyflow",
            {
                "ts_code": ts_code,
                "start_date": start_date.strftime("%Y%m%d"),
                "end_date": end_date.strftime("%Y%m%d"),
            },
        )
        if not df.empty:
            self.store.upsert("moneyflow", df)
        return self.store.query(
            "SELECT * FROM moneyflow WHERE ts_code = ? AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date ASC",
            [ts_code, start_date, end_date],
        )

    def fetch_limit_list_d(self, trade_date: date | None = None) -> pd.DataFrame:
        """涨跌停列表（按 trade_date 全量拉取）。"""
        if trade_date is None:
            trade_date = self.get_latest_trade_date()
        df = self.client.query("limit_list_d", {"trade_date": trade_date.strftime("%Y%m%d")})
        if not df.empty:
            self.store.upsert("limit_list_d", df, date_cols=("trade_date",))
        return df

    def fetch_sw_daily(self, trade_date: date | None = None) -> pd.DataFrame:
        if trade_date is None:
            trade_date = self.get_latest_trade_date()
        df = self.client.query("sw_daily", {"trade_date": trade_date.strftime("%Y%m%d")})
        if not df.empty:
            self.store.upsert("sw_daily", df)
        return df

    def fetch_income(self, ts_code: str) -> pd.DataFrame:
        df = self.client.query("income", {"ts_code": ts_code})
        if not df.empty:
            self.store.upsert("income", df, date_cols=("end_date", "ann_date"))
        return df

    def fetch_fina_indicator(self, ts_code: str) -> pd.DataFrame:
        df = self.client.query("fina_indicator", {"ts_code": ts_code})
        if not df.empty:
            self.store.upsert("fina_indicator", df, date_cols=("end_date", "ann_date"))
        return df

    def refresh_universe(self) -> pd.DataFrame:
        """刷新全市场股票池（stock_basic）。"""
        df = self.client.query("stock_basic", {"list_status": "L"})
        if not df.empty:
            self.store.upsert("stock_basic", df, date_cols=("list_date", "delist_date"))
        return df

    def get_universe(self) -> list[str]:
        """获取符合过滤规则的股票池代码列表。"""
        df = self.store.query(
            """
            SELECT ts_code FROM stock_basic
            WHERE delist_date IS NULL
              AND ts_code NOT LIKE '688%' AND ts_code NOT LIKE '300%'
              AND ts_code NOT LIKE '301%'
              AND name NOT LIKE '%ST%'
            """
        )
        return df["ts_code"].tolist() if not df.empty else []

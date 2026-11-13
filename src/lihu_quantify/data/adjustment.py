"""前复权处理（第十一轮 P1-1）。

背景：此前回测全链路用 Tushare `daily`（未复权价），除权日 MA/MACD/金叉
计算错误，成本价止损与 MA10 破位在除权后假触发，卖出 pnl 按除权后价格对
除权前成本计算——指标与盈亏双重失真。

修复：
    1. data 层落 DuckDB 表 `adj_factors`（Tushare `adj_factor` 接口，走既有 upsert）。
    2. 数据准备阶段前复权：price_adj = price × adj_factor / 最新 adj_factor
       （open/high/low/close 全套；vol 除以复权因子，保持换手量纲一致）。
    3. 指标计算与撮合统一用前复权价（引擎无需感知）；回测报告注明"前复权口径"。

前复权锚点：以 adj_factor 序列中最新 trade_date 的因子为基准，当日价格不动，
历史价格按比例下移，保证指标与盈亏在不同时点可比。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from loguru import logger


def _to_date(val) -> date | None:
    if val is None:
        return None
    if isinstance(val, date) and not isinstance(val, pd.Timestamp):
        return val
    if isinstance(val, pd.Timestamp):
        return val.date()
    s = str(val).strip()
    if not s:
        return None
    if s.isdigit() and len(s) == 8:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    try:
        return pd.to_datetime(s).date()
    except (ValueError, TypeError):
        return None


def apply_forward_adjustment(df: pd.DataFrame, adj_factor: pd.DataFrame) -> pd.DataFrame:
    """对日线 df 做前复权，返回新 DataFrame（不改输入）。

    df: Tushare daily（须含 ts_code/trade_date/open/high/low/close/vol）
    adj_factor: DataFrame(trade_date, adj_factor [, ts_code])；
        若含 ts_code 则先按当只过滤。
    """
    if df is None or df.empty:
        return df
    if adj_factor is None or adj_factor.empty:
        logger.warning("adj_factor 为空，跳过复权（数据可能缺复权因子接口权限）")
        return df.copy()

    out = df.copy()
    adj = adj_factor.copy()

    # 若带 ts_code 列则按当只筛选
    if "ts_code" in adj.columns:
        code = out["ts_code"].iloc[0] if "ts_code" in out.columns else None
        if code is not None:
            adj = adj[adj["ts_code"].astype(str) == str(code)]
    if adj.empty:
        logger.warning(f"{out['ts_code'].iloc[0] if 'ts_code' in out.columns else ''} adj_factor 无匹配，跳过复权")
        return out

    adj["_d"] = adj["trade_date"].map(_to_date)
    # 最新基准因子
    anchor_date = adj["_d"].max()
    anchor = adj.loc[adj["_d"] == anchor_date, "adj_factor"].iloc[0]
    if anchor is None or pd.isna(anchor) or float(anchor) <= 0:
        logger.warning("adj_factor 锚点无效，跳过复权")
        return out
    anchor = float(anchor)

    # 合并复权因子到日线（按 trade_date 对齐）
    out["_d"] = out["trade_date"].map(_to_date)
    adj_map = adj.drop_duplicates("_d").set_index("_d")["adj_factor"]
    out["_adj"] = out["_d"].map(adj_map)
    # 缺失因子的行（若早期无因子数据）：用首个出现的因子回填，避免 NaN 破坏指标
    if out["_adj"].isna().any():
        out["_adj"] = out["_adj"].ffill().bfill()
    if out["_adj"].isna().any():
        logger.warning(f"日线部分日期无复权因子，无法完整复权（{out['_d'].isna().sum()} 行）")
        out["_adj"] = out["_adj"].fillna(anchor)

    ratio = out["_adj"] / anchor
    for c in ("open", "high", "low", "close", "pre_close"):
        if c in out.columns:
            out[c] = out[c] * ratio
    if "vol" in out.columns:
        # 换手量纲：前复权后量除复权因子（价格上X倍则量下X倍，保持金额一致）
        out["vol"] = out["vol"] / ratio.where(ratio != 0)

    out["adj_factor"] = out["_adj"]
    out = out.drop(columns=["_d", "_adj"])
    logger.debug(f"前复权完成：{out['trade_date'].iloc[0]}~{out['trade_date'].iloc[-1]}，"
                 f"锚点 {anchor_date}（factor={anchor:.4f}）")
    return out


def adjust_from_store(client, store, ts_code: str, df: pd.DataFrame,
                      start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """对已拉取的日线 df 做前复权：从本地 adj_factors 读取因子，缺失则拉取入库。

    回测脚本（run_backtest / run_full_backtest / grid_search_v2）共用，
    消除三处重复的复权接线。复权失败时原样返回未复权 df。
    """
    if df is None or df.empty:
        return df
    if start is None or end is None:
        dates = df["trade_date"].tolist()
        if not dates:
            return df
        start = min(_to_date(d) for d in dates if _to_date(d) is not None) or start
        end = max(_to_date(d) for d in dates if _to_date(d) is not None) or end
        if start is None or end is None:
            return df

    local = None
    try:
        local = store.query(
            "SELECT trade_date, adj_factor FROM adj_factors "
            "WHERE ts_code = ? AND trade_date BETWEEN ? AND ?",
            [ts_code, start, end],
        )
    except Exception:  # DuckDB 查询失败不阻断回测
        local = None

    if local is None or local.empty:
        try:
            adj = client.query(
                "adj_factor",
                {
                    "ts_code": ts_code,
                    "start_date": start.strftime("%Y%m%d"),
                    "end_date": end.strftime("%Y%m%d"),
                },
            )
            if not adj.empty:
                store.upsert("adj_factors", adj)
                local = adj.copy()
        except Exception as e:  # 接口无权限/网络异常 → 跳过复权
            logger.warning(f"{ts_code} 复权因子拉取失败，使用未复权价: {e}")
            return df.copy()

    if local is None or local.empty:
        return df.copy()
    return apply_forward_adjustment(df, local)
"""全市场（抽样）回测：修复3——扩大股票池解决样本量问题。

用法：
    python run_full_backtest.py                 # 默认 50 只主板股票 × 2 年
    python run_full_backtest.py --n 100 --years 3   # 100 只 × 3 年

修复C(第三轮)：结果持久化到 outputs/backtest_result.json
    {equity_curve, metrics, config_hash, market_filter_mode, pool_info}
    看板 /api/equity 读取该文件画权益曲线。
修复E(第三轮)：板块映射传入 engine.run()，板块 40% 铁律在回测中生效。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine

# 复用 run_backtest 的输出逻辑
from run_backtest import print_result


def fetch_universe(client: TushareClient, store: DuckDBStore, max_n: int = 50) -> list[str]:
    """修复3：用 stock_basic 拉主板池，排除 688/300/301/ST。"""
    df = client.query("stock_basic", {"list_status": "L"})
    if df.empty:
        return []
    store.upsert("stock_basic", df, date_cols=("list_date", "delist_date"))
    # 过滤
    df = df[df["ts_code"].notna()]
    mask = (
        ~df["ts_code"].str.startswith("688")
        & ~df["ts_code"].str.startswith("300")
        & ~df["ts_code"].str.startswith("301")
    )
    if "name" in df.columns:
        mask &= ~df["name"].str.contains("ST", na=False)
    df = df[mask].sort_values("ts_code").reset_index(drop=True)
    codes = df["ts_code"].head(max_n).tolist()
    logger.info(f"主板股票池过滤后取前 {len(codes)} 只（要求 {max_n}）")
    return codes


def build_sector_map(client: TushareClient, store: DuckDBStore) -> dict[str, str]:
    """修复E(第三轮)：从 stock_basic 构建 {ts_code: industry} 板块映射。"""
    basic = client.query("stock_basic", {"list_status": "L"})
    if basic.empty:
        return {}
    store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    sector_map: dict[str, str] = {}
    if "industry" in basic.columns:
        for _, row in basic.iterrows():
            ind = row.get("industry")
            ind = str(ind).strip() if pd.notna(ind) and str(ind).strip() else "未分类"
            sector_map[row["ts_code"]] = ind
    logger.info(f"[板块映射] 构建完成：{len(sector_map)} 只（修复E 接线）")
    return sector_map


def _config_hash(settings) -> str:
    """配置摘要（回测结果可追溯：参数+费率+过滤模式）。"""
    s, r, b = settings.strategy, settings.risk, settings.backtest
    payload = json.dumps({
        "ma_periods": s.ma_periods,
        "freshness": s.golden_cross_max_freshness_days,
        "volume_ratio": s.volume_ratio_threshold,
        "entity_ratio": s.entity_ratio_threshold,
        "ma5_dev": s.close_to_ma5_max_dev,
        "chasing": s.chasing_high_threshold,
        "max_single": r.max_single_position,
        "stop_loss_force": r.stop_loss_force,
        "commission": b.commission, "stamp_tax": b.stamp_tax, "slippage": b.slippage,
        "market_filter": s.market_filter, "market_filter_mode": s.market_filter_mode,
    }, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()[:12]


def fetch_data(client: TushareClient, store: DuckDBStore, codes: list[str], years: int):
    """拉取股票池日线 + 指数。返回 (data_dict, idx_df)。"""
    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    latest = store.get_latest_trade_date()
    start = latest - timedelta(days=years * 365 + 30)
    logger.info(f"回测区间 {start} ~ {latest}（约 {years} 年）")

    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    idx_df = idx_df[(idx_df["trade_date"] >= start) & (idx_df["trade_date"] <= latest)].sort_values("trade_date").reset_index(drop=True)

    data = {}
    for i, code in enumerate(codes):
        try:
            df = client.query("daily", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": latest.strftime("%Y%m%d"),
            })
        except Exception as e:
            logger.warning(f"{code} 拉取失败: {e}")
            continue
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        data[code] = df.sort_values("trade_date").reset_index(drop=True)
        if (i + 1) % 10 == 0:
            logger.info(f"已拉取 {i+1}/{len(codes)} 只，{code}: {len(df)} 根")
    return data, idx_df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50, help="股票数量")
    parser.add_argument("--years", type=int, default=2, help="回测年数")
    args = parser.parse_args()

    logger.info("加载配置...")
    settings = get_settings("config/settings.yaml")
    token = settings.resolved_tushare_token()
    logger.info(f"Token: {token[:8]}...{token[-4:]}")

    client = TushareClient(token=token, cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    codes = fetch_universe(client, store, max_n=args.n)
    if not codes:
        logger.error("股票池为空，退出")
        sys.exit(1)

    data, idx_df = fetch_data(client, store, codes, years=args.years)
    logger.info(f"实际取到数据 {len(data)} 只股票")
    if not data:
        logger.error("无可用数据，退出")
        sys.exit(1)

    # 修复E(第三轮)：板块映射（训练口径与实盘一致）
    sector_map = build_sector_map(client, store)

    # 修复5a：settings 接线
    s = settings.strategy
    r = settings.risk
    b = settings.backtest
    strategy = CherryClaw(
        ma_periods=tuple(s.ma_periods),
        golden_cross_max_freshness=s.golden_cross_max_freshness_days,
        volume_ratio_threshold=s.volume_ratio_threshold,
        entity_ratio_threshold=s.entity_ratio_threshold,
        close_to_ma5_max_dev=s.close_to_ma5_max_dev,
        max_position_pct=r.max_single_position,
        stop_loss_force_pct=r.stop_loss_force,
    )
    broker = SimulatedBroker(
        commission_rate=b.commission, stamp_tax_rate=b.stamp_tax, slippage=b.slippage,
    )
    # 市场状态参考信号（修复A：默认 reduce 减仓模式）
    from run_backtest import classify_market_state
    market_states = classify_market_state(idx_df) if s.market_filter else None
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker, max_single=r.max_single_position,
        market_states=market_states, market_filter_on=s.market_filter,
        market_filter_mode=s.market_filter_mode,
    )
    logger.info(f"市场信号: {'开启（' + s.market_filter_mode + '）' if s.market_filter else '关闭'}")

    logger.info(f"启动事件驱动回测：{len(data)} 只股票 × {args.years} 年...")
    result = engine.run(data, init_capital=settings.init_capital,
                        sector_by_code=sector_map)
    print_result(result, list(data.keys()), idx_df)

    # ===== 修复C(第三轮)：结果持久化（看板 /api/equity 读取） =====
    out_dir = Path(__file__).parent / "outputs"
    out_dir.mkdir(exist_ok=True)
    backtest_data = latest if (latest := store.get_latest_trade_date()) else None
    equity_curve = [
        {"date": str(d), "equity": round(float(v), 2)}
        for d, v in result.equity.items()
    ]
    payload = {
        "generated_at": str(date.today()),
        "config_hash": _config_hash(settings),
        "market_filter_mode": s.market_filter_mode if s.market_filter else "off",
        "pool_info": {
            "type": "head-N 主板池",
            "n_requested": args.n,
            "n_with_data": len(data),
            "years": args.years,
            "end_date": str(backtest_data),
            "survivorship_bias": "按当前上市股票构建，历史回测收益系统性偏高（乐观口径）",
        },
        "rules_included": {
            "halt": "连亏3笔停手30天（铁律F）",
            "sector_40pct": "板块≤40%（修复E）",
            "t_plus_1": True,
            "stop_loss_force_8pct": True,
            "trailing_stop_3pct": True,
        },
        "init_capital": settings.init_capital,
        "equity_curve": equity_curve,
        "metrics": {k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in result.metrics.items()},
    }
    result_path = out_dir / "backtest_result.json"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(f"回测结果已持久化 → {result_path}")


if __name__ == "__main__":
    main()

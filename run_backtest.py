"""回测验证脚本：用真实 Tushare 数据跑 CherryClaw 策略回测。

修复清单对接：
  5a：settings.yaml 参数真正生效（策略/风控/撮合）
  5c：止损执行率按 reason 如实统计；费用占比
  6：市场状态分段统计（上涨/震荡/下跌）

用法：
    python run_backtest.py                       # 默认主板大票池
    python run_backtest.py 600519.SH 600036.SH    # 自定义股票池
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.market import classify_market_state  # P2-9-4：迁入包内，scheduler 复用
from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine

DEFAULT_STOCKS = ["600584.SH", "600519.SH", "601318.SH", "600036.SH"]


def fetch_data(client: TushareClient, store: DuckDBStore, codes: list[str], days: int = 180):
    """拉取股票池日线 + 000001.SH 指数（用于市场分段）。返回 (data_dict, index_df)。"""
    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    latest = store.get_latest_trade_date()
    start = latest - timedelta(days=days)
    logger.info(f"回测区间 {start} ~ {latest}（{days} 自然日）")

    # 指数数据（用于修复6 市场分段）
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    idx_df = idx_df[(idx_df["trade_date"] >= start) & (idx_df["trade_date"] <= latest)].sort_values("trade_date").reset_index(drop=True)

    data = {}
    for code in codes:
        df = client.query("daily", {
            "ts_code": code,
            "start_date": start.strftime("%Y%m%d"),
            "end_date": latest.strftime("%Y%m%d"),
        })
        if df.empty:
            logger.warning(f"{code} 无数据，跳过")
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        # 第十一轮 P1-1：前复权（除权日 MA/止损/盈亏口径修正）
        from lihu_quantify.data.adjustment import adjust_from_store
        df = adjust_from_store(client, store, code, df, start, latest)
        data[code] = df.sort_values("trade_date").reset_index(drop=True)
        logger.info(f"{code}: {len(df)} 根日线（前复权）")
    return data, idx_df


def classify_market_state(idx_df: pd.DataFrame, window: int = 20) -> dict:
    """薄引用：实现迁至 lihu_quantify.market（见 P2-9-4）。"""
    from lihu_quantify.market import classify_market_state as _impl

    return _impl(idx_df, window)


def print_result(result, codes, idx_df):
    """打印回测结果（含修复5c 止损率 + 修复6 分段统计）。"""
    print("\n" + "=" * 70)
    print("回测结果")
    print("=" * 70)
    print(f"\n股票池: {', '.join(codes)}")
    print(f"信号产生: {result.signals_generated} 个买入信号")
    print(f"Checklist 拒绝: {result.orders_rejected} 个（风控闸门拦截）")

    print(f"\n--- 交易明细 ({len(result.trades)} 笔) ---")
    if result.trades:
        trade_df = pd.DataFrame([
            {
                "date": t.trade_date, "code": t.ts_code, "side": t.side,
                "price": round(t.price, 3), "volume": t.volume,
                "pnl": round(t.pnl, 2), "reason": t.reason[:20] if t.reason else "",
            }
            for t in result.trades
        ])
        print(trade_df.to_string(index=False))
    else:
        print("  无成交")

    # 权益曲线
    eq = result.equity
    print(f"\n--- 权益曲线 ---")
    print(f"  起始权益: {eq.iloc[0]:,.0f}")
    print(f"  结束权益: {eq.iloc[-1]:,.0f}")
    print(f"  最高权益: {eq.max():,.0f}")
    print(f"  最低权益: {eq.min():,.0f}")
    print(f"  交易日数: {len(eq)}")

    # 绩效
    m = result.metrics
    print(f"\n--- 绩效指标 ---")
    print(f"  总收益率:   {m['total_return']:.2%}")
    print(f"  年化收益率: {m['annual_return']:.2%}")
    print(f"  夏普比率:   {m['sharpe']:.2f}")
    print(f"  最大回撤:   {m['max_drawdown']:.2%}")
    print(f"  卡玛比率:   {m['calmar']:.2f}")
    print(f"  胜率:       {m['win_rate']:.2%}（按买入-卖出轮次）")
    print(f"  盈亏比:     {m['profit_loss_ratio']:.2f}")
    print(f"  平均持仓:   {m['avg_holding_days']:.1f} 天")
    print(f"  月均交易:   {m['monthly_trade_count']:.1f} 轮")
    print(f"  总交易轮次: {m['total_trades']}")
    print(f"  费用占比:   {m['avg_cost_ratio']:.3%}（合计 {m['total_fees']:.0f} 元）")

    # 修复5c：止损执行率按 reason 如实统计
    print(f"\n--- 止损执行率（修复5c：按 reason 如实统计）---")
    sells = [t for t in result.trades if t.side == "sell"]
    loss_sells = [t for t in sells if t.pnl < 0]
    # 按 reason 分类
    reason_counts: dict[str, int] = {}
    for t in sells:
        # 从 reason 提取止损类型关键词
        r = t.reason
        if "成本-8%" in r or "force_stop" in r:
            cat = "强制止损(-8%)"
        elif "跌破 10 日线" in r or "ma_break" in r:
            cat = "破10日线"
        elif "成本-5%" in r or "execute" in r:
            cat = "执行止损(-5%)"
        elif "移动止盈" in r or "trailing_stop" in r:
            cat = "移动止盈"
        elif "三层过滤" in r or "CherryClaw" in r:
            cat = "策略卖出"
        else:
            cat = "其他"
        reason_counts[cat] = reason_counts.get(cat, 0) + 1
    if reason_counts:
        for cat, n in sorted(reason_counts.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {n} 笔")
    stop_triggered = sum(n for c, n in reason_counts.items() if c != "策略卖出" and c != "其他")
    should_stop = len(loss_sells)  # 应止损的亏损单
    print(f"  止损类触发: {stop_triggered} 笔 / 亏损单 {should_stop} 笔")

    # 修复6：市场分段统计
    print(f"\n--- 市场状态分段统计（修复6）---")
    states = classify_market_state(idx_df)
    if states and sells:
        seg_stats: dict[str, dict] = {}
        for t in result.trades:
            if t.side != "sell":
                continue
            st = states.get(t.trade_date, "未知")
            if st not in seg_stats:
                seg_stats[st] = {"trades": 0, "wins": 0, "pnl_sum": 0.0}
            seg_stats[st]["trades"] += 1
            seg_stats[st]["pnl_sum"] += t.pnl
            if t.pnl > 0:
                seg_stats[st]["wins"] += 1
        print(f"  {'状态':<6} {'笔数':>4} {'胜率':>8} {'合计盈亏':>12}")
        for st in ["上涨", "震荡", "下跌", "未知"]:
            if st in seg_stats:
                s = seg_stats[st]
                wr = s["wins"] / s["trades"] if s["trades"] else 0
                print(f"  {st:<6} {s['trades']:>4} {wr:>7.1%} {s['pnl_sum']:>12.0f}")
    else:
        print("  无足够数据分段")

    # 铁律自检
    print("\n" + "=" * 70)
    print("铁律自检（来自 docs/月度复盘模板.md）")
    print("=" * 70)
    print(f"  盈亏比目标: >1.0（实际 {m['profit_loss_ratio']:.2f}）")
    print(f"  单票最大仓位: ≤25%（引擎强制）")
    print(f"  费用摩擦: {m['avg_cost_ratio']:.3%}（吃掉收益的部分）")
    print("\n  以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")


if __name__ == "__main__":
    logger.info("加载配置...")
    settings = get_settings("config/settings.yaml")
    token = settings.resolved_tushare_token()
    logger.info(f"Token: {token[:8]}...{token[-4:]}")

    codes = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_STOCKS
    logger.info(f"股票池: {codes}")

    client = TushareClient(token=token, cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    data, idx_df = fetch_data(client, store, codes, days=180)
    if not data:
        logger.error("无可用数据，退出")
        sys.exit(1)

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
        commission_rate=b.commission,
        stamp_tax_rate=b.stamp_tax,
        slippage=b.slippage,
    )
    # 市场状态参考信号（修复A：默认 reduce 减仓模式）
    market_states = classify_market_state(idx_df) if s.market_filter else None
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker,
        max_single=r.max_single_position,
        market_states=market_states,
        market_filter_on=s.market_filter,
        market_filter_mode=s.market_filter_mode,
    )
    logger.info(f"策略参数: close_to_ma5={s.close_to_ma5_max_dev}, freshness={s.golden_cross_max_freshness_days}")
    logger.info(f"费率: 佣金{b.commission} 印花税{b.stamp_tax} 滑点{b.slippage}")
    logger.info(f"市场信号: {'开启（' + s.market_filter_mode + '：非上涨段仓位缩放）' if s.market_filter else '关闭'}")

    result = engine.run(data, init_capital=settings.init_capital)
    print_result(result, list(data.keys()), idx_df)

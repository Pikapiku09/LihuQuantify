"""实盘/模拟盘入口：收盘后扫描 → 信号 → Checklist 闸门 → OMS 下单 → 止损监控。

灰度路径（docs/ARCHITECTURE.md §9.3）：
    模拟盘（默认）→ 小资金实盘 → 逐步放量

用法：
    python run_live.py                      # 模拟盘（默认，安全）
    python run_live.py --mode paper         # 同上
    python run_live.py --mode live          # 实盘（需 QMT 运行 + PYTHONPATH 配好）
    python run_live.py --mode live --rebuild # 实盘 + 从持仓重建止损（崩溃恢复）
"""
from __future__ import annotations

import argparse
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
from lihu_quantify.risk.checklist import ChecklistGate, CheckContext
from lihu_quantify.execution.paper_trade import PaperBroker
from lihu_quantify.execution.oms import OrderManagementSystem
from lihu_quantify.indicators.standard import add_all_standard
from run_backtest import classify_market_state


def scan_universe(client: TushareClient, store: DuckDBStore, n: int = 50, days: int = 120):
    """收盘后扫描股票池，返回 (带信号标的列表, market_states, idx_df)。"""
    basic = client.query("stock_basic", {"list_status": "L"})
    if basic.empty:
        return [], {}, None
    store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    dfb = basic.copy()
    mask = (
        ~dfb["ts_code"].str.startswith("688")
        & ~dfb["ts_code"].str.startswith("300")
        & ~dfb["ts_code"].str.startswith("301")
    )
    if "name" in dfb.columns:
        mask &= ~dfb["name"].str.contains("ST", na=False)
    codes = dfb[mask].sort_values("ts_code")["ts_code"].head(n).tolist()

    idx = client.query("index_daily", {"ts_code": "000001.SH", "end_date": "20301231"})
    store.upsert("index_daily", idx)
    latest = store.get_latest_trade_date()
    start = latest - timedelta(days=days)
    logger.info(f"扫描基准日（真实最新交易日）: {latest}")

    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    idx_df = idx_df[idx_df["trade_date"] <= latest].sort_values("trade_date").reset_index(drop=True)
    market_states = classify_market_state(idx_df)
    state_today = market_states.get(latest, "未知")
    logger.info(f"当前市场状态: {state_today}（过滤={'放行' if state_today == '上涨' else '拦截新开仓'}）")

    strategy = CherryClaw()  # 参数由调用方传入
    signals = []
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
        if df.empty or len(df) < 30:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        sig = strategy.latest_signal(df)
        if sig is not None:
            # 附带最新指标行（供 Checklist 追高检查）
            df_ind = add_all_standard(df.sort_values("trade_date").reset_index(drop=True))
            sig.reason = sig.reason or ""
            signals.append((sig, df_ind.iloc[-1]))
        if (i + 1) % 20 == 0:
            logger.info(f"扫描进度 {i+1}/{len(codes)}，当前信号 {len(signals)} 个")
    return signals, market_states, idx_df


def main():
    parser = argparse.ArgumentParser(description="LihuQuantify 实盘/模拟盘入口")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--n", type=int, default=50, help="扫描股票数")
    parser.add_argument("--rebuild", action="store_true", help="从持仓重建止损登记（崩溃恢复）")
    parser.add_argument("--monitor-seconds", type=float, default=0, help="止损监控时长（秒，0=不监控）")
    args = parser.parse_args()

    settings = get_settings("config/settings.yaml")
    s, r = settings.strategy, settings.risk

    # ===== 1. 建 Broker + OMS =====
    if args.mode == "live":
        if not settings.qmt.enabled:
            logger.error("实盘模式需要 settings.yaml qmt.enabled=true（安全开关）")
            sys.exit(1)
        from lihu_quantify.execution.xtquant_client import MiniQMTClient
        broker = MiniQMTClient(qmt_path=settings.qmt.path, account_id=settings.qmt.account)
        if not broker.connect():
            logger.error("MiniQMT 连接失败：请确认 QMT 极简模式已启动并登录")
            sys.exit(1)
        mode_tag = "实盘"
    else:
        client = TushareClient(
            token=settings.resolved_tushare_token(),
            cache_dir=settings.resolved_cache_dir(),
        )
        store = DuckDBStore(settings.resolved_duckdb_path())
        broker = PaperBroker(init_capital=settings.init_capital, tushare_client=client)
        broker.connect()
        mode_tag = "模拟盘"

    oms = OrderManagementSystem(broker)
    logger.info(f"[{mode_tag}] OMS 就绪")

    # 崩溃恢复：从持仓重建止损
    if args.rebuild:
        n_rebuilt = oms.rebuild_stops_from_positions()
        logger.info(f"止损登记重建：{n_rebuilt} 个")

    # ===== 2. 收盘后扫描 =====
    if args.mode == "paper":
        signals, market_states, _ = scan_universe(
            client, store, n=args.n,
        )
    else:
        # 实盘：同样用 Tushare 扫描（数据源独立于交易通道）
        tc = TushareClient(token=settings.resolved_tushare_token(),
                           cache_dir=settings.resolved_cache_dir())
        st = DuckDBStore(settings.resolved_duckdb_path())
        signals, market_states, _ = scan_universe(tc, st, n=args.n)

    latest_state = market_states.get(max(market_states) if market_states else None, "未知")

    # ===== 3. 信号 → Checklist → OMS =====
    asset = broker.query_asset()
    total_asset = asset.get("total_asset", 0)
    gate = ChecklistGate(chasing_high_threshold=s.chasing_high_threshold)
    strategy = CherryClaw(close_to_ma5_max_dev=s.close_to_ma5_max_dev)

    print("\n" + "=" * 70)
    print(f"[{mode_tag}] 交易时段报告")
    print("=" * 70)
    print(f"账户总资产: {total_asset:,.0f}（现金 {asset.get('cash', 0):,.0f}）")
    print(f"市场状态: {latest_state}")
    print(f"扫描信号: {len(signals)} 个")

    if latest_state != "上涨" and s.market_filter:
        print("\n市场过滤：当前非上涨段 → 不开新仓（持仓止损照常监控）")
    else:
        executed = 0
        for sig, last_bar in signals:
            # 仓位计算（单票 ≤25%）
            invest = min(sig.suggested_position_pct, r.max_single_position) * total_asset
            price = sig.suggested_price or float(last_bar["close"])
            volume = int(invest / price / 100) * 100
            if volume < 100:
                continue
            # Checklist 8 项闸门
            account_snapshot = broker_snapshot(broker)
            check_ctx = CheckContext(
                current_price=price,
                ma10=float(last_bar.get("ma10", 0)) if pd.notna(last_bar.get("ma10")) else 0.0,
                invest_amount=invest,
            )
            result = gate.check(sig, account_snapshot, check_ctx)
            if not result.approved:
                rejected = ", ".join(i.name for i in result.rejected_items())
                print(f"  [拒绝] {sig.ts_code}: {rejected}")
                continue
            # OMS：买入+止损同时挂（铁律1/2 在 OMS 内再校验）
            buy_result, stop = oms.place_buy_with_stop(sig, volume, price)
            if buy_result.success:
                executed += 1
                print(f"  [买入] {sig.ts_code} {volume}股 @ {price:.2f}，止损线 {stop.stop_price:.2f}")
            else:
                print(f"  [失败] {sig.ts_code}: {buy_result.msg}")
        print(f"\n执行: {executed}/{len(signals)}")

    # ===== 4. 止损监控 =====
    pending = oms.pending_stops()
    print(f"止损监控: {len(pending)} 个持仓")
    for p in pending:
        print(f"  {p.ts_code} {p.volume}股 止损线 {p.stop_price:.2f}（{p.reason}）")

    if args.monitor_seconds > 0:
        logger.info(f"启动止损监控循环 {args.monitor_seconds}s ...")
        oms.monitor_loop(max_seconds=args.monitor_seconds)

    # 模拟盘资产行情
    if args.mode == "paper":
        final = broker.query_asset()
        print(f"\n[{mode_tag}] 期末: 总资产 {final['total_asset']:,.0f}，"
              f"现金 {final['cash']:,.0f}，持仓市值 {final['market_value']:,.0f}")
        print(f"成交笔数: {len(broker.trades)}")

    print("\n以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")


def broker_snapshot(broker) -> "AccountSnapshot":
    """从 Broker 查询构造 AccountSnapshot（供 Checklist）。"""
    from lihu_quantify.types import AccountSnapshot, Position
    positions = [
        Position(ts_code=p.ts_code, volume=p.volume, cost=p.cost,
                 current_price=p.market_value / p.volume if p.volume else 0)
        for p in broker.query_positions()
    ]
    asset = broker.query_asset()
    return AccountSnapshot(
        total_asset=asset.get("total_asset", 0),
        cash=asset.get("cash", 0),
        positions=positions,
    )


if __name__ == "__main__":
    main()

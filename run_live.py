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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.risk.checklist import ChecklistGate, CheckContext
from lihu_quantify.execution.paper_trade import PaperBroker
from lihu_quantify.execution.oms import OrderManagementSystem


def scan_universe(n: int = 50, days: int = 120):
    """收盘后扫描股票池：复用 DailyScanner.collect_signals（P2-9-6）。

    返回 (signals, market_states, None)。signals=[(sig, last_ind), ...]，
    market_states 仅含 {latest: 当日市场状态}（供市场过滤判断用）。
    此前的重复 scan_universe（股票池/参数/信号生成）已收敛到 DailyScanner 单一实现。
    """
    from lihu_quantify.monitor.scheduler import DailyScanner

    settings = get_settings("config/settings.yaml")
    sc = DailyScanner(settings)
    res = sc.collect_signals(n=n, days=days)
    return res["signals"], {res["latest"]: res["market_state"]}, None


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
    # paper / live 均用同一扫描路径（数据源独立于交易通道）
    signals, market_states, _ = scan_universe(n=args.n)

    latest_state = market_states.get(max(market_states) if market_states else None, "未知")

    # ===== 3. 信号 → Checklist → OMS =====
    asset = broker.query_asset()
    total_asset = asset.get("total_asset", 0)
    gate = ChecklistGate(chasing_high_threshold=s.chasing_high_threshold)

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
    """从 Broker 查询构造 AccountSnapshot（供 Checklist）。

    P0-5（第十一轮）：委托 scheduler.build_account_snapshot——旧实现缺
    trades/halted_until/psychology_alert，导致 8 项闸门只生效 5 项。
    """
    from lihu_quantify.monitor.scheduler import build_account_snapshot

    return build_account_snapshot(broker)


if __name__ == "__main__":
    main()

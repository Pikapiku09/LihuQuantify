"""修复A：市场过滤的独立 holdout 验证 + 阈值敏感性分析。

统计纪律：
    - holdout 段（2023-01 ~ 2024-06，未曾参与网格训练/验证）只跑
      "过滤 ON vs OFF"两组对照 + 阈值敏感性，**只输出结论，不据此调参**；
    - 阈值敏感性：20日涨幅阈值 3% → 2%/4%；窗口 20日 → 60日。
      若结论随阈值小幅变化而剧变 → 过滤规则脆弱，降级为"参考信号"。

用法：
    python scripts/validate_market_filter.py --n 50
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.data.duckdb_store import DuckDBStore
from lihu_quantify.strategy.cherry_claw import CherryClaw
from lihu_quantify.backtest.broker import SimulatedBroker
from lihu_quantify.backtest.engine import EventDrivenEngine

# holdout 段：从未参与训练/验证
HOLDOUT_START = date(2023, 1, 1)
HOLDOUT_END = date(2024, 6, 30)

# 敏感性变体（修复A.2）
VARIANTS = [
    {"label": "ON-3%-20d", "threshold": 3.0, "window": 20},
    {"label": "ON-2%-20d", "threshold": 2.0, "window": 20},
    {"label": "ON-4%-20d", "threshold": 4.0, "window": 20},
    {"label": "ON-3%-60d", "threshold": 3.0, "window": 60},
]


def classify(idx_df: pd.DataFrame, threshold: float, window: int) -> dict:
    """按变体参数分类市场状态。返回 {trade_date: state}。"""
    df = idx_df.sort_values("trade_date").reset_index(drop=True).copy()
    df["ret"] = df["close"].pct_change(window) * 100
    states = {}
    for _, row in df.iterrows():
        if pd.isna(row["ret"]):
            states[row["trade_date"]] = "未知"
        elif row["ret"] >= threshold:
            states[row["trade_date"]] = "上涨"
        elif row["ret"] <= -threshold:
            states[row["trade_date"]] = "下跌"
        else:
            states[row["trade_date"]] = "震荡"
    return states


def fetch_data(client: TushareClient, store: DuckDBStore, n: int):
    """拉取股票池 + 指数（覆盖 holdout 段，多取 window 预热）。"""
    basic = client.query("stock_basic", {"list_status": "L"})
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
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date

    # 数据起点比 holdout 再往前 90 天（指标预热）
    fetch_start = HOLDOUT_START - timedelta(days=90)
    data = {}
    for code in codes:
        try:
            df = client.query("daily", {
                "ts_code": code,
                "start_date": fetch_start.strftime("%Y%m%d"),
                "end_date": HOLDOUT_END.strftime("%Y%m%d"),
            })
        except Exception as e:
            logger.warning(f"{code} 拉取失败: {e}")
            continue
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        data[code] = df.sort_values("trade_date").reset_index(drop=True)
    logger.info(f"取到 {len(data)} 只股票（{fetch_start} ~ {HOLDOUT_END}）")
    return data, idx_df


def run_one(data: dict, settings, market_states: dict | None, label: str,
            sector_by_code: dict[str, str] | None = None) -> dict:
    """跑一组（过滤 ON/OFF）。返回指标摘要。

    修复A/E(第三轮)：含连亏停手 + 板块40%口径（engine 内部生效），
    sector_by_code 传入使训练口径与实盘一致。
    """
    s, r, b = settings.strategy, settings.risk, settings.backtest
    strategy = CherryClaw(
        ma_periods=tuple(s.ma_periods),
        golden_cross_max_freshness=s.golden_cross_max_freshness_days,
        volume_ratio_threshold=s.volume_ratio_threshold,
        entity_ratio_threshold=s.entity_ratio_threshold,
        close_to_ma5_max_dev=s.close_to_ma5_max_dev,
        max_position_pct=r.max_single_position,
        stop_loss_force_pct=r.stop_loss_force,
    )
    broker = SimulatedBroker(commission_rate=b.commission, stamp_tax_rate=b.stamp_tax, slippage=b.slippage)
    engine = EventDrivenEngine(
        strategy=strategy, broker=broker,
        market_states=market_states,
        market_filter_on=market_states is not None,
        max_single=r.max_single_position,
    )
    result = engine.run(
        data, init_capital=settings.init_capital,
        start=HOLDOUT_START, end=HOLDOUT_END,
        sector_by_code=sector_by_code,
    )
    m = result.metrics
    # 空仓天数（权益无持仓的日子：用信号被过滤比例近似——直接统计 states 在区间内非上涨占比）
    return {
        "label": label,
        "total_return": m["total_return"],
        "max_drawdown": m["max_drawdown"],
        "calmar": m["calmar"],
        "win_rate": m["win_rate"],
        "profit_loss_ratio": m["profit_loss_ratio"],
        "trades": m["total_trades"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    args = parser.parse_args()

    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    client = TushareClient(token=settings.resolved_tushare_token(),
                           cache_dir=settings.resolved_cache_dir())
    store = DuckDBStore(settings.resolved_duckdb_path())

    data, idx_df = fetch_data(client, store, args.n)
    if not data:
        logger.error("无数据")
        sys.exit(1)

    # 修复E(第三轮)：板块映射（训练口径与实盘一致）
    from grid_search_v2 import build_sector_map
    sector_map = build_sector_map(client, store)

    # holdout 区间内的指数（分类用）
    idx_hold = idx_df[(idx_df["trade_date"] >= HOLDOUT_START - timedelta(days=90))
                      & (idx_df["trade_date"] <= HOLDOUT_END)].reset_index(drop=True)

    # ===== 对照组：OFF =====
    off = run_one(data, settings, None, "OFF", sector_by_code=sector_map)
    # ===== 主组：ON（3% / 20d，当前默认） =====
    states_default = classify(idx_hold, threshold=3.0, window=20)
    on = run_one(data, settings, states_default, "ON-3%-20d", sector_by_code=sector_map)
    # ===== 敏感性变体 =====
    variants = [on]
    for v in VARIANTS[1:]:
        states_v = classify(idx_hold, threshold=v["threshold"], window=v["window"])
        variants.append(run_one(data, settings, states_v, v["label"], sector_by_code=sector_map))

    # ===== 输出 =====
    rows = [off] + variants
    df = pd.DataFrame(rows)
    print("\n" + "=" * 78)
    print(f"市场过滤 holdout 验证（{HOLDOUT_START} ~ {HOLDOUT_END}，"
          f"未被训练/验证消费的独立数据段；含连亏停手+板块40%口径，修复A第三轮重跑）")
    print("=" * 78)
    print(df.to_string(index=False,
                       formatters={"total_return": "{:.2%}".format,
                                   "max_drawdown": "{:.2%}".format,
                                   "calmar": "{:.2f}".format,
                                   "win_rate": "{:.1%}".format,
                                   "profit_loss_ratio": "{:.2f}".format}))
    df.to_csv(ROOT / "outputs" / "market_filter_holdout_halt.csv",
              index=False, encoding="utf-8-sig")

    # 空仓占比（各变体）
    print("\n--- 各过滤变体的'非上涨日'占比（近似空仓倾向）---")
    in_range = [d for d in states_default if HOLDOUT_START <= d <= HOLDOUT_END]
    for v in VARIANTS:
        states_v = classify(idx_hold, v["threshold"], v["window"])
        non_up = sum(1 for d in in_range if states_v.get(d) != "上涨")
        print(f"  {v['label']}: 非上涨 {non_up}/{len(in_range)} = {non_up/max(1,len(in_range)):.1%}")

    # ===== 结论判定 =====
    print("\n" + "=" * 78)
    print("结论判定")
    print("=" * 78)
    on_ret, off_ret = on["total_return"], off["total_return"]
    print(f"ON vs OFF 收益差: {on_ret:.2%} vs {off_ret:.2%} → "
          f"{'ON 有效' if on_ret > off_ret else 'ON 无效'}")

    # 脆弱性：各变体相对 ON 主组的收益差
    rets = [v["total_return"] for v in variants]
    spread = max(rets) - min(rets)
    print(f"过滤变体收益极差（3%±1%、20d↔60d）: {spread:.2%}")
    fragile = spread > 0.10 or not all(r > off_ret for r in rets)
    if fragile:
        print("⚠️ 结论：市场过滤对阈值/窗口敏感（脆弱）——")
        print("   按修复A验收标准：不能当铁律，降级为参考信号。")
        print("   建议：market_filter 改为'降仓信号'（非上涨段仓位减半）或默认关闭，")
        print("   并在决策日志记录本次 holdout 已消费。")
    else:
        print("✓ 结论：市场过滤在 holdout 段稳健（各变体方向一致且均优于 OFF）——")
        print("   可保留为正式规则（结果已记录至决策日志）。")

    print("\n注意：本脚本只做验证不调参；holdout 段已消费，永失验收资格（见 docs/决策日志.md）。")
    print("\n以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。")


if __name__ == "__main__":
    main()

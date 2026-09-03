"""补跑缺失交易日的巡检（本机未开调度器期间的空窗，如 8/28-8/31）。

场景：调度器只在 NAS/本机常驻时才每日 16:30 巡检；机器关机/未部署的
交易日会缺报告。本脚本从 data/last_scan.json 的上一巡检日开始，逐个
补跑到最新完整交易日——每日流程与正常巡检完全一致（信号→闸门→OMS→
止损→报告→last_scan 推进），prev_total_asset 链条连续，幂等保护天然
防重复补跑。

用法（项目根目录）：
    .venv/Scripts/python.exe scripts/backfill_scans.py           # 自动补缺
    .venv/Scripts/python.exe scripts/backfill_scans.py --n 50    # 每日扫描数
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows GBK 控制台防崩：任何来源的 stdout 特殊字符（✓/⚠/emoji）降级为替换符
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

# 计划任务无窗口运行：日志落 data/logs/scheduler_YYYYMMDD.log（30 天滚动，
# 与常驻调度器同文件名，运维口径统一）
try:
    from lihu_quantify.monitor.log_setup import setup_file_logging

    setup_file_logging("scheduler")
except Exception:
    pass

import pandas as pd
from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.market import classify_market_state
from lihu_quantify.monitor.scheduler import DailyScanner


def main():
    ap = argparse.ArgumentParser(description="补跑缺失交易日的巡检报告")
    ap.add_argument("--n", type=int, default=50, help="每日扫描股票数（与调度器一致）")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = ap.parse_args()

    settings = get_settings(str(ROOT / "config" / "settings.yaml"))
    scanner = DailyScanner(settings, mode=args.mode)
    scanner._settings_path = str(ROOT / "config" / "settings.yaml")

    # 交易日状态序列（直连取最新 index_daily，与 _market_state 同口径）
    idx = scanner.client.query(
        "index_daily", {"ts_code": "000001.SH", "end_date": "20301231"},
        use_cache=False,
    )
    scanner.store.upsert("index_daily", idx)
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(
        idx_df["trade_date"], format="%Y%m%d").dt.date
    idx_df = idx_df.sort_values("trade_date").reset_index(drop=True)
    states = classify_market_state(idx_df)
    latest = scanner.store.get_latest_trade_date()

    # 缺失区间 = (上次巡检日, 最新完整交易日] 内的交易日
    last = scanner._read_last_scan()
    last_raw = (last or {}).get("trade_date")
    last_date = None
    if last_raw:
        try:
            last_date = date.fromisoformat(str(last_raw)[:10])
        except ValueError:
            last_date = None
    missing = [d for d in idx_df["trade_date"].tolist()
               if (last_date is None or d > last_date) and d <= latest]
    if not missing:
        logger.info(f"[补跑] 无缺失交易日（上次巡检 {last_date}，最新 {latest}）")
        return
    logger.info(f"[补跑] 缺失交易日：{[str(d) for d in missing]}（逐日执行，请稍候）")

    orig_market_state = scanner._market_state
    try:
        for d in missing:
            logger.info(f"[补跑] ===== {d}（{states.get(d, '未知')}）=====")
            # 基准日锚定到历史日（scan/_scan_impl 内全部 latest 均来自此方法）
            scanner._market_state = lambda d=d: (d, states.get(d, "未知"))
            summary = scanner.scan(n=args.n)   # 幂等：last_scan 随每次补跑推进
            logger.info(
                f"[补跑] {d} 完成：{summary['signals']} 信号 / "
                f"{len(summary['executed'])} 执行 / {len(summary['rejected'])} 拦截 → "
                f"{summary['report']}"
            )
    finally:
        scanner._market_state = orig_market_state
    logger.info("[补跑] 全部完成；看板巡检报告页已可查看新报告。")


if __name__ == "__main__":
    main()

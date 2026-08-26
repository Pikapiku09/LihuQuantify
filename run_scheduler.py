"""调度入口：常驻进程，按 cron 定时巡检。

用法：
    python run_scheduler.py                     # 模拟盘调度（默认）
    python run_scheduler.py --mode live         # 实盘调度（需 qmt.enabled=true）
    python run_scheduler.py --run-now           # 立即跑一次巡检再进入调度（测试/首启）
    python run_scheduler.py --run-now --force   # 强制重跑当日巡检（幂等保护的人工补跑口）
    python run_scheduler.py --n 100             # 扫描 100 只

生产部署：
    - NAS（DS918+）：docker-compose 启动（见 docs/DEPLOY_NAS.md），restart=unless-stopped
    - Windows 备用：任务计划程序开机自启（见 docs/DEPLOY_WINDOWS.md）

日志（第四轮清单7）：data/logs/scheduler_YYYYMMDD.log，每天轮转，保留 30 天。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.monitor.log_setup import setup_file_logging
from lihu_quantify.monitor.scheduler import DailyScanner, setup_scheduler


def main():
    parser = argparse.ArgumentParser(description="LihuQuantify 定时巡检调度器")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--n", type=int, default=50, help="每日扫描股票数")
    parser.add_argument("--run-now", action="store_true", help="启动时立即执行一次巡检")
    parser.add_argument("--force", action="store_true",
                        help="跳过幂等保护强制重跑当日巡检（人工补跑用；仅对 --run-now 生效）")
    args = parser.parse_args()

    # 第四轮清单7：文件日志（30 天滚动）；cron 触发的巡检同样落盘
    setup_file_logging("scheduler")

    settings = get_settings("config/settings.yaml")
    logger.info(f"调度器启动：mode={args.mode}, n={args.n}, "
                f"cron={settings.scheduler.daily_scan_cron} ({settings.scheduler.timezone})")

    if args.mode == "live" and not settings.qmt.enabled:
        logger.error("实盘模式需要 settings.yaml qmt.enabled=true")
        sys.exit(1)

    if args.run_now:
        logger.info(f"[--run-now] 立即执行一次巡检（force={args.force}）")
        scanner = DailyScanner(settings, mode=args.mode)
        summary = scanner.scan(n=args.n, force=args.force)
        print(f"\n巡检摘要: 基准日 {summary['trade_date']}，"
              f"市场 {summary['market_state']}，信号 {summary['signals']}，"
              f"执行 {len(summary['executed'])}，拦截 {len(summary['rejected'])}")
        print(f"报告: {summary['report']}")

    sched = setup_scheduler(settings, mode=args.mode, n=args.n)
    logger.info("进入调度等待（Ctrl+C 退出）...")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("调度器退出")


if __name__ == "__main__":
    main()

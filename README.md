# LihuQuantify

A 股日线级量化交易系统：Tushare 数据 → CherryClaw 策略 → 八项 Checklist 风控闸门 → 回测验证 → 模拟盘/MiniQMT 实盘 → Web 监控看板。

## 快速开始

```bash
# 安装（Python >= 3.10）
pip install -e ".[web,dev]"

# 配置
#   1) Tushare token：tushareMcp.json 或 .env 的 LIHU_TUSHARE_TOKEN
#   2) AI 总结 key（可选）：设置页填入或 .env 的 LIHU_AI_SUMMARY__API_KEY
```

| 场景 | 命令 |
|---|---|
| 常驻调度（每日 16:30 自动巡检） | `python run_scheduler.py` |
| 单次巡检（立即跑一次） | `python run_scheduler.py --run-now` |
| 手动实盘/模拟盘单次流程 | `python run_live.py --mode paper` |
| 回测验证 | `python run_backtest.py` |
| 全市场（抽样）回测 | `python run_full_backtest.py` |
| 补跑缺失交易日的巡检 | `python scripts/backfill_scans.py` |
| 看板（Windows 双击即可） | `start_dashboard.bat` → http://127.0.0.1:8000 |

## 文档

- 用户指南：[docs/USER_GUIDE.md](docs/USER_GUIDE.md)
- 架构设计：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 部署：[docs/DEPLOY_NAS.md](docs/DEPLOY_NAS.md) / [docs/DEPLOY_WINDOWS.md](docs/DEPLOY_WINDOWS.md)

## 免责声明

本项目仅供学习研究，不构成任何投资建议。投资有风险，入市需谨慎。

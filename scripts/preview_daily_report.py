"""临时预览脚本：用真实 paper_state.json 渲染日报 HTML（第五轮验收用，可删）。"""
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.scheduler import _build_daily_summary  # noqa: E402

state = json.loads((ROOT / "data" / "paper_state.json").read_text(encoding="utf-8"))

# ---- 用状态文件组装 broker stub（只读，不连 DuckDB/Tushare） ----
from lihu_quantify.execution.base import PositionInfo  # noqa: E402

broker = MagicMock()
broker.init_capital = state.get("init_capital", 100000.0)
broker.cash = state.get("cash", 0.0)
positions = []
for code, p in (state.get("positions") or {}).items():
    positions.append(PositionInfo(
        ts_code=code, volume=p.get("available", 0),
        frozen=p.get("volume", 0) - p.get("available", 0),
        cost=p.get("cost", 0.0), market_value=p.get("volume", 0) * p.get("cost", 0.0),
    ))
broker.query_positions.return_value = positions
# 现价：用 asset 快照反推不现实，演示用成本价上浮/下浮（仅预览样式）
price_demo = {}
for i, code in enumerate(state.get("positions") or {}):
    price_demo[code] = list(state["positions"].values())[i]["cost"] * (1.0 + 0.02 * ((-1) ** i))
broker.get_price.side_effect = lambda c: price_demo.get(c, 0.0)
asset_snapshot = state.get("asset") or {}
total_asset = asset_snapshot.get("total_asset", broker.cash)
broker.query_asset.return_value = {
    "cash": broker.cash, "total_asset": total_asset,
    "market_value": total_asset - broker.cash,
}
broker.trades = state.get("trades", [])
broker.halted_codes.return_value = {}

summary = _build_daily_summary(
    broker=broker,
    latest=date(2026, 8, 27),
    market_state="震荡",
    signals=69,
    entry_scale=0.5,
    executed=[{"ts_code": "600036.SH", "name": "招商银行", "volume": 600,
               "price": 38.5, "stop": 35.4}],
    rejected=[
        {"ts_code": "601127.SH", "name": "赛力斯", "price": 85.0,
         "reasons": "铁律1：止损价 89.00 不低于买入价 85.00，拒绝下单"},
        {"ts_code": "002415.SZ", "name": "海康威视", "price": 29.8,
         "reasons": "仓位预算(占比 30% 超过 25%)"},
    ],
    executed_stops=[],
    pending_stops=[{"ts_code": "000333.SZ", "volume": 300, "stop_price": 66.5,
                    "reason": "price_stop"}],
    positions=positions,
    stop_registry={},
    name_map={code: f"示例股票{i}" for i, code in enumerate(state.get("positions") or {})},
    prev_total_asset=total_asset - 753.0,
    report_path="outputs/reports/2026-08-27.md",
    mode="paper",
    alerts=[{"level": "warn", "title": "风控拦截: 601127.SH",
             "detail": "铁律1：止损价 89.00 不低于买入价 85.00，拒绝下单"}],
)

from lihu_quantify.monitor.daily_report import build_daily_report_email  # noqa: E402

subject, html = build_daily_report_email(summary)
out = ROOT / "outputs" / "preview_daily_report.html"
out.write_text(html, encoding="utf-8")
print("written:", out)
print("subject saved to outputs/preview_subject.txt")
(out.parent / "preview_subject.txt").write_text(subject, encoding="utf-8")

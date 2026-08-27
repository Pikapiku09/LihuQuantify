"""LihuQuantify 监控看板后端（FastAPI）。

只读聚合现有状态文件 + DuckDB，不改变任何运行时行为：
    - /api/dashboard      仪表盘聚合数据
    - /api/reports        报告列表（outputs/reports/*.md）
    - /api/reports/{date} 单份报告内容
    - /api/state          模拟盘状态（paper_state.json）
    - /api/grid           网格搜索结果（outputs/grid_*.csv）
    - /api/equity         最新回测权益曲线（回测需先跑过）

启动：
    uvicorn web.server:app --host 127.0.0.1 --port 8000 --reload
    或 python -m web.server
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "outputs" / "reports"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

# 第四轮清单7：文件日志（web 进程独立前缀，与 scheduler 分文件）
sys.path.insert(0, str(ROOT / "src"))
try:
    from lihu_quantify.monitor.log_setup import setup_file_logging

    setup_file_logging("web")
except Exception:  # 日志失败不阻断看板
    pass

app = FastAPI(title="LihuQuantify Monitor", version="0.1.0")

# 静态文件（web/static/）
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


# ============================================================
# 数据助手
# ============================================================

def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_paper_state() -> dict:
    return _read_json(DATA_DIR / "paper_state.json")


def _read_last_scan() -> dict:
    """第六轮清单2/3：最近一次巡检摘要（rich 数据源）。"""
    return _read_json(DATA_DIR / "last_scan.json")


def _merge_positions(state: dict, registry: dict, scan_summary: dict) -> list[dict]:
    """第六轮清单2：持仓合并——paper_state 的 volume/cost 为准，
    last_scan.summary.positions 提供 name/price/market_value/float_pnl/
    float_pnl_pct/weight/stop_price（无 last_scan 时回退旧字段）。
    """
    rich = {p.get("ts_code"): p
            for p in (scan_summary.get("positions") or []) if p.get("ts_code")}
    positions = []
    for code, p in (state.get("positions") or {}).items():
        reg = registry.get(code, {})
        rp = rich.get(code, {})
        positions.append({
            "ts_code": code,
            "name": rp.get("name", ""),
            "volume": p.get("volume", 0),
            "cost": p.get("cost", 0.0),
            "price": rp.get("price"),
            "market_value": rp.get("market_value"),
            "float_pnl": rp.get("float_pnl"),
            "float_pnl_pct": rp.get("float_pnl_pct"),
            "weight": rp.get("weight"),
            "stop_price": reg.get("stop_price") if reg.get("stop_price") is not None
                          else rp.get("stop_price"),
            "triggered": reg.get("triggered", False),
        })
    return positions


def _live_metrics(state: dict, scan_summary: dict, total_asset: float) -> dict:
    """第六轮清单3：真实监控指标（非回测预测值）。

    - cumulative_return：(total_asset − init_capital) / init_capital
    - day_pnl：total_asset − prev_total_asset（上次巡检为空 → None 不展示）
    - floating_pnl：Σ last_scan.summary.positions.float_pnl
    - realized_today：Σ last_scan.summary.sells_today.pnl
    """
    init_capital = state.get("init_capital") or 0
    prev = scan_summary.get("prev_total_asset")
    positions = scan_summary.get("positions") or []
    sells = scan_summary.get("sells_today") or []
    return {
        "cumulative_return": (
            (total_asset - init_capital) / init_capital if init_capital else None
        ),
        "day_pnl": (total_asset - prev) if (prev is not None and prev > 0) else None,
        "floating_pnl": sum(p.get("float_pnl") or 0 for p in positions),
        "realized_today": sum(s.get("pnl") or 0 for s in sells),
        "init_capital": init_capital,
    }


def _read_stop_registry() -> dict:
    return _read_json(DATA_DIR / "stop_registry.json")


def _read_pending_stops() -> list:
    p = DATA_DIR / "pending_stops.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _market_state() -> dict:
    """市场状态（live：调度器运行时 DuckDB 被锁，优先读 filter_stats.json）。"""
    # 1) filter_stats.json（修复G 每日记录，调度器与 web 共享）
    stats = _read_json(DATA_DIR / "filter_stats.json")
    if isinstance(stats, list) and stats:
        latest = sorted(stats, key=lambda r: r.get("date", ""))[-1]
        if latest.get("market_state"):
            return {
                "state": latest["market_state"],
                "latest": latest.get("date"),
                "ret20d": None,
            }
    # 2) DuckDB 直查（调度器未运行时）
    db = DATA_DIR / "lihu_quant.duckdb"
    if not db.exists():
        return {"state": "未知", "latest": None, "ret20d": None}
    try:
        import duckdb

        con = duckdb.connect(str(db), read_only=True)
        df = con.execute(
            "SELECT trade_date, close FROM index_daily WHERE ts_code='000001.SH' "
            "ORDER BY trade_date DESC LIMIT 25"
        ).df()
        con.close()
        if len(df) < 21:
            return {"state": "未知", "latest": None, "ret20d": None}
        df = df.sort_values("trade_date").reset_index(drop=True)
        df["ret20"] = df["close"].pct_change(20) * 100
        last = df.iloc[-1]
        ret = last["ret20"]
        state = "上涨" if ret >= 3 else ("下跌" if ret <= -3 else "震荡")
        return {
            "state": state,
            "latest": str(last["trade_date"])[:10],
            "ret20d": round(float(ret), 2),
        }
    except Exception:
        return {"state": "未知", "latest": None, "ret20d": None}


# ============================================================
# API
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = WEB_DIR / "static" / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>LihuQuantify Monitor</h1><p>index.html not found</p>", status_code=404)
    return index_path.read_text(encoding="utf-8")


@app.get("/api/dashboard")
async def dashboard():
    """仪表盘聚合数据（修复C：总资产读 asset 快照，非 init_capital）。

    第六轮清单2/3：持仓合并 last_scan 的 rich 字段（名称/现价/市值/浮盈亏），
    新增 live 块（真实监控指标：累计收益率/今日盈亏/浮动盈亏/今日已实现）。
    """
    state = _read_paper_state()
    registry = _read_stop_registry()
    pending = _read_pending_stops()
    market = _market_state()
    last_scan = _read_last_scan()
    scan_summary = (last_scan.get("summary") or {}) if last_scan else {}

    positions = _merge_positions(state, registry, scan_summary)

    trades = state.get("trades", [])
    # 近 10 笔交易（倒序）
    recent_trades = list(reversed(trades[-10:])) if trades else []

    # 修复C(第三轮)：优先读 asset 快照（cash+市值）；旧状态文件回退 init_capital
    asset_snap = state.get("asset") or {}
    total_asset = asset_snap.get("total_asset") or state.get("init_capital", 100000)

    return {
        "market": market,
        "account": {
            "cash": state.get("cash", 0),
            "total_asset": total_asset,
            "market_value": asset_snap.get("market_value", 0),
            "init_capital": state.get("init_capital", 100000),
            "asset_source": "snapshot" if asset_snap else "legacy_init_capital",
            "positions": positions,
            "halt_map": state.get("halt_map", {}),
        },
        "live": _live_metrics(state, scan_summary, total_asset),
        "stop_registry": registry,
        "pending_stops": pending,
        "recent_trades": recent_trades,
        # 修复C(第三轮)：网格"最优格"降级为训练段参考，不再作为头条绩效
        "grid_training_reference": _grid_training_reference(),
        "backtest": _read_backtest_summary(),
    }


def _grid_training_reference() -> dict:
    """网格最优格（训练段参考，弱化展示用）。"""
    out = {}
    for summary_file in sorted((ROOT / "outputs").glob("grid_v2_*_summary.json")):
        try:
            d = _read_json(summary_file)
            out[d.get("pool_label") or summary_file.stem] = {
                "best": d.get("best"),
                "n_robust": d.get("n_robust"),
                "n_total": d.get("n_total"),
                "orig_robust": d.get("orig_robust"),
                "pool_built_date": d.get("pool_built_date"),
                "label": "训练段参考（含幸存者偏差，非实盘口径）",
            }
        except Exception:
            continue
    return out


def _read_backtest_summary() -> dict:
    """最近一次全量回测摘要（修复C：当前实盘配置口径）。"""
    d = _read_json(ROOT / "outputs" / "backtest_result.json")
    if not d:
        return {"available": False}
    curve = d.get("equity_curve") or []
    return {
        "available": True,
        "generated_at": d.get("generated_at"),
        "config_hash": d.get("config_hash"),
        "market_filter_mode": d.get("market_filter_mode"),
        "pool_info": d.get("pool_info"),
        "metrics": d.get("metrics"),
        "equity_points": len(curve),
    }


@app.get("/api/equity")
async def equity():
    """权益曲线（修复C：读 outputs/backtest_result.json）。"""
    d = _read_json(ROOT / "outputs" / "backtest_result.json")
    if not d:
        raise HTTPException(status_code=404, detail="no backtest result; run run_full_backtest.py first")
    curve = d.get("equity_curve") or []
    # 最大回撤区间（修复C：前端叠加阴影）
    peak = -1.0
    dd_start = dd_end = dd_low = None
    max_dd = 0.0
    cur_start = None
    for pt in curve:
        v = pt["equity"]
        if v > peak:
            if max_dd > 0 and (peak - dd_low) / peak >= max_dd:
                pass
            peak = v
            cur_start = pt["date"]
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            dd_start = cur_start
            dd_end = pt["date"]
            dd_low = v
    return {
        "generated_at": d.get("generated_at"),
        "config_hash": d.get("config_hash"),
        "market_filter_mode": d.get("market_filter_mode"),
        "pool_info": d.get("pool_info"),
        "metrics": d.get("metrics"),
        "equity_curve": curve,
        "max_drawdown_range": {"start": dd_start, "end": dd_end, "depth": round(max_dd, 4)},
    }


@app.get("/api/reports")
async def list_reports():
    """报告列表（按日期降序）。"""
    if not REPORTS_DIR.exists():
        return {"reports": []}
    files = sorted(REPORTS_DIR.glob("*.md"), reverse=True)
    reports = []
    for f in files:
        # 统计：提取标题首行
        try:
            first_line = f.read_text(encoding="utf-8").split("\n", 1)[0]
        except Exception:
            first_line = f.stem
        reports.append({
            "name": f.stem,
            "date": f.stem,
            "size": f.stat().st_size,
            "title": first_line.replace("# ", "").strip(),
        })
    return {"reports": reports}


@app.get("/api/reports/{report_name}")
async def get_report(report_name: str):
    """单份报告 Markdown 内容。"""
    # 防止路径遍历
    if ".." in report_name or "/" in report_name or "\\" in report_name:
        raise HTTPException(status_code=400, detail="invalid report name")
    path = REPORTS_DIR / f"{report_name}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="report not found")
    return {"name": report_name, "content": path.read_text(encoding="utf-8")}


@app.get("/api/state")
async def state():
    """模拟盘完整状态。"""
    return _read_paper_state()


@app.get("/api/grid")
async def grid():
    """网格搜索结果摘要（修复H.2：读 grid_v2_*_summary.json 结构化数据）。

    所有收益数字均为【训练段参考·含幸存者偏差】（修复B/C：弱化展示）。
    """
    out_dir = ROOT / "outputs"
    results = {}
    # 新口径（含停手+板块，修复A/E）：summary JSON
    for summary_file in sorted(out_dir.glob("grid_v2_*_summary.json")):
        try:
            d = _read_json(summary_file)
            if not d:
                continue
            results[summary_file.stem.replace("_summary", "")] = {
                "best": d.get("best"),
                "count": d.get("n_total"),
                "robust_points": d.get("n_robust"),
                "orig_params": d.get("orig_params"),
                "orig_total_return": d.get("orig_total_return"),
                "orig_robust": d.get("orig_robust"),
                "pool_built_date": d.get("pool_built_date"),
                "survivorship_bias": True,
                "label": "训练段参考（含幸存者偏差，非实盘口径）",
            }
        except Exception:
            continue
    # 旧口径 CSV（无停手，仅对照保留；标 legacy）
    for csv in sorted(out_dir.glob("grid_v2_*.csv")):
        name = csv.stem
        if name.endswith("_halt") or name in results:
            continue
        try:
            df = pd.read_csv(csv)
            if df.empty:
                continue
            best = df.sort_values("calmar", ascending=False).iloc[0]
            results[name] = {
                "best": {
                    "ma5_dev": float(best["ma5_dev"]),
                    "freshness": int(best["freshness"]),
                    "chasing": float(best["chasing"]),
                    "total_return": float(best["total_return"]),
                    "calmar": float(best["calmar"]),
                    "trades": int(best["trades"]),
                },
                "count": len(df),
                "robust_points": None,
                "label": "legacy（无连亏停手口径，已被 _halt 版取代）",
                "survivorship_bias": True,
            }
        except Exception:
            continue
    return {"grids": results}


def main():
    """本机直跑默认 127.0.0.1（安全边界，第四轮清单9）。

    Docker 容器内需绑 0.0.0.0 才能被端口映射——compose 已设 LIHU_WEB_HOST=0.0.0.0；
    宿主机侧安全由 compose ports 绑定内网 IP / 不做公网映射控制。
    """
    import os

    import uvicorn

    host = os.environ.get("LIHU_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("LIHU_WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

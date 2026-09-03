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
from fastapi.middleware.gzip import GZipMiddleware
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

# P2-7（第十一轮）：gzip 压缩（echarts.min.js ~1MB 未压缩传输的问题）。
# minimum_size 默认 1000，1KB 以下不压缩（更小反而负收益）。
app.add_middleware(GZipMiddleware, minimum_size=1024)

# 静态文件（web/static/）
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


# ============================================================
# P0-7（第十一轮）：最小 API 鉴权（Bearer Token）
#
# 环境变量 LIHU_WEB_TOKEN 非空时，所有 /api/* 请求必须带
# Authorization: Bearer <token>，否则 401。空值 = 不启用（本机
# 127.0.0.1 直跑零配置行为不变）。
# 豁免：看板页面本身（/ 与 /static/*）——页面可打开，API 401 时
# 前端弹令牌输入框（一次存储 localStorage），便于手机/局域网首次访问。
# ============================================================

@app.middleware("http")
async def _bearer_auth(request, call_next):
    import os

    token = os.environ.get("LIHU_WEB_TOKEN", "")
    if token and request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {token}":
            return JSONResponse({"detail": "未授权：缺少或错误的 Bearer Token"
                                         "（LIHU_WEB_TOKEN）"}, status_code=401)
    return await call_next(request)


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


def _dashboard_review(state: dict) -> dict:
    """评审进度（100 笔 live 验收）：直接读 paper_state.trades 现算
    （比 last_scan 更新更实时）。字段结构见 review_progress.review_stats。"""
    try:
        from lihu_quantify.monitor.review_progress import review_stats

        return review_stats(state.get("trades") or [])
    except Exception:
        return {"closed_rounds": 0, "target": 100, "remaining": 100,
                "win_rate": None, "pl_ratio": None, "stage": "accumulating"}


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


@app.get("/api/health")
async def health():
    """问题4（第九轮）：健康检查（start_lihu.bat 先探测，通了才开浏览器）。"""
    return {"ok": True}


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
        # 评审进度（100 笔 live 验收；口径=配对轮次，见 review_progress.py）
        "review": _dashboard_review(state),
        # 问题1（第九轮）：AI 收盘总结上看板（纯展示，前端 textContent 渲染；
        # 未配置 key/生成失败 → None → 前端显示"暂无"，不报错）
        "ai_summary": scan_summary.get("ai_summary"),
        "ai_summary_date": scan_summary.get("trade_date"),
        # 需求5（第十轮）：今日简报（AI 版优先，失败自动回退规则版，永不为空；
        # 旧 last_scan 无该字段 → 前端回退显示 ai_summary）
        "brief": scan_summary.get("brief"),
        "brief_is_ai": bool(scan_summary.get("ai_summary")),
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


# ============================================================
# 第八轮需求2：热力图（日级刷新）
# 问题3（第九轮）：数据源改为 scheduler 写的 heatmap_snapshot.json，
# 彻底切断 web 对 DuckDB 的依赖（调度器常驻持有写锁 → 只读连接必败
# → 名称/行业静默降级为代码/"未知"，即用户反馈的"只显示代码"根因）。
# ============================================================

def _pct_bucket(p: float) -> str:
    if p is None:
        return "无数据"
    if p < -5:
        return "① 大跌 <-5%"
    if p < -2:
        return "② 下跌 -5%~-2%"
    if p < 0:
        return "③ 微跌 -2%~0"
    if p < 2:
        return "④ 微涨 0~2%"
    if p < 5:
        return "⑤ 上涨 2~5%"
    return "⑥ 大涨 ≥5%"


def _amount_bucket(a: float) -> str:
    """amount 单位：千元（Tushare daily 口径）。"""
    if a is None:
        return "无数据"
    if a < 200_000:
        return "① 冷清 <2亿"
    if a < 500_000:
        return "② 温和 2~5亿"
    if a < 1_000_000:
        return "③ 活跃 5~10亿"
    return "④ 放量 ≥10亿"


# 进程内缓存：看板每 30s 轮询，快照文件未变（size/mtime）→ 直接复用上次
# 解析结果，巡检重写快照后自动失效重算（问题3：单文件，无需全目录扫描）
_hm_cache: dict = {"sig": None, "rows": []}


def _heatmap_rows() -> list[dict]:
    """问题3（第九轮）：读 data/heatmap_snapshot.json（scheduler 巡检写入）。

    快照由 scheduler 用 name_map/sector_map 生成（UTF-8、ensure_ascii=False），
    编码链路单一可控（问题2 乱码根治）；web 不再读 DuckDB（锁库降级根因）。
    读失败/不存在 → 抛异常（端点转 503 提示等待巡检）。
    """
    import json as _json

    path = DATA_DIR / "heatmap_snapshot.json"
    if not path.exists():
        raise FileNotFoundError("热力图快照未生成（等待首次巡检写入）")
    sig = (str(path), path.stat().st_size, path.stat().st_mtime)
    if _hm_cache["sig"] == sig:
        return _hm_cache["rows"]
    try:
        rows = _json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise ValueError(f"热力图快照读取失败: {e}")
    if not isinstance(rows, list) or not rows:
        raise ValueError("热力图快照为空")
    _hm_cache["sig"] = sig
    _hm_cache["rows"] = rows
    return rows


@app.get("/api/heatmap")
async def heatmap(dim: str = "industry"):
    """热力图 treemap 数据（ECharts children 结构，红涨绿跌由前端着色）。

    维度：industry（行业）| pct_bucket（涨跌幅档）| amount_bucket（成交额档）。
    问题3（第九轮）：数据 = data/heatmap_snapshot.json（scheduler 巡检写入，
    含名称/行业，无 DuckDB 依赖）；未生成/读取失败 → 503（前端提示稍后重试）。
    """
    if dim not in ("industry", "pct_bucket", "amount_bucket"):
        raise HTTPException(400, "dim 须为 industry | pct_bucket | amount_bucket")
    try:
        rows = _heatmap_rows()
    except Exception as e:
        raise HTTPException(503, f"{e}（每日 16:30 巡检后生成）")
    if not rows:
        raise HTTPException(503, "热力图快照为空（等待巡检生成）")

    def group_key(r: dict) -> str:
        if dim == "pct_bucket":
            return _pct_bucket(r.get("pct_chg"))
        if dim == "amount_bucket":
            return _amount_bucket(r.get("amount"))
        return str(r.get("industry") or "未知")

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(group_key(r), []).append(r)

    children = []
    for gname, items in sorted(groups.items(), key=lambda kv: -sum(i.get("amount") or 0 for i in kv[1])):
        leaves = [{
            "name": f"{i['name']}",
            "code": i["ts_code"],
            "industry": i["industry"],
            "value": round(float(i.get("amount") or 0), 1),   # 面积=成交额（千元）
            "pct_chg": round(float(i["pct_chg"]), 2) if i.get("pct_chg") is not None else None,
            "close": round(float(i["close"]), 2) if i.get("close") is not None else None,
        } for i in items]
        pcts = [l["pct_chg"] for l in leaves if l["pct_chg"] is not None]
        children.append({
            "name": gname,
            "value": round(sum(l["value"] for l in leaves), 1),
            "avg_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
            "children": leaves,
        })
    return {
        "dim": dim,
        "trade_date": max((r["trade_date"] for r in rows), default=None),
        "dims": [
            {"key": "industry", "label": "按行业"},
            {"key": "pct_bucket", "label": "按涨跌幅"},
            {"key": "amount_bucket", "label": "按成交额"},
        ],
        "count": len(rows),
        "children": children,
    }


@app.get("/api/heatmap/detail")
async def heatmap_detail(code: str):
    """热力图点击 → 个股详情（缓存日线近 10 日 + 基础信息）。

    P2-9-12（第十一轮）：code 入参改为正则白名单 `^\\d{6}\\.(SH|SZ)$`，
    防止任意字符串拼接到 glob 路径（旧逻辑仅查 "." in code）。
    """
    import re

    if not code or not re.fullmatch(r"\d{6}\.(SH|SZ)", code):
        raise HTTPException(400, "code 须为 ts_code 格式（如 600000.SH）")
    import json as _json

    safe_code = code.replace(".", "_")
    files = sorted((DATA_DIR / "cache").glob(f"daily_{safe_code}_*.json"))
    if not files:
        raise HTTPException(404, f"{code} 无本地行情（不在巡检覆盖范围）")
    # 取最近一次拉取的缓存（按文件 mtime；文件名解析不可靠——
    # 交易所段/日期段位置随 ts_code 变化，且同票可能存在多个日期段缓存）
    fp = max(files, key=lambda p: p.stat().st_mtime)
    try:
        resp = _json.loads(fp.read_text(encoding="utf-8"))
        fields = (resp.get("data") or {}).get("fields") or []
        items = (resp.get("data") or {}).get("items") or []
    except (json.JSONDecodeError, OSError):
        raise HTTPException(503, "行情缓存读取失败")
    if not items:
        raise HTTPException(404, f"{code} 无行情数据")
    idx = {f: i for i, f in enumerate(fields)}
    items.sort(key=lambda r: str(r[idx["trade_date"]]))
    bars = [{
        "date": str(r[idx["trade_date"]]),
        "open": r[idx["open"]] if "open" in idx else None,
        "high": r[idx["high"]] if "high" in idx else None,
        "low": r[idx["low"]] if "low" in idx else None,
        "close": r[idx["close"]] if "close" in idx else None,
        "pct_chg": r[idx["pct_chg"]] if "pct_chg" in idx else None,
        "amount": r[idx["amount"]] if "amount" in idx else None,
    } for r in items[-10:]]
    # 名称/行业：读热力图快照（问题3：不再依赖 DuckDB——调度器锁库时旧实现
    # 必降级为代码/"未知"；快照由 scheduler 侧 name_map/sector_map 生成）
    info = {"ts_code": code, "name": code, "industry": "未知"}
    try:
        for r in _heatmap_rows():
            if r.get("ts_code") == code:
                info = {"ts_code": code,
                        "name": r.get("name") or code,
                        "industry": r.get("industry") or "未知"}
                break
    except Exception:
        pass
    return {"info": info, "bars": bars}


# ============================================================
# 第八轮：配置展示（只读，脱敏——不含任何 token/密钥）
# ============================================================

@app.get("/api/config")
async def config_view():
    """看板配置页数据（只读展示，修改请编辑 config/settings.yaml 后重启调度器）。"""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from lihu_quantify.config import get_settings

        st = get_settings(str(ROOT / "config" / "settings.yaml"))
        cg, hm, sc, stg, rk = (st.capital_guard, st.heatmap,
                               st.scheduler, st.strategy, st.risk)
        return {
            "capital_guard": cg.model_dump(), "heatmap": hm.model_dump(),
            "scheduler": sc.model_dump(),
            "strategy": stg.model_dump(), "risk": rk.model_dump(),
            "init_capital": st.init_capital,
            "note": "只读展示；修改 config/settings.yaml 后需重启调度器生效。",
            "log_files": "data/logs/scheduler_YYYYMMDD.log（保留 30 天）",
        }
    except Exception as e:
        return {"error": f"配置读取失败：{e}"}


# ============================================================
# 第十轮需求1/2/3：系统设置读写（元数据 + 冻结期锁 + 确认 + 原子写 + 审计）
# ============================================================

def _settings_yaml() -> Path:
    return ROOT / "config" / "settings.yaml"


def _fresh_settings():
    """读当前配置（不走 lru_cache，保证设置页即时反映文件内容）。"""
    sys.path.insert(0, str(ROOT / "src"))
    from lihu_quantify.config import load_yaml_config, Settings

    return Settings(**load_yaml_config(str(_settings_yaml())))


# 可编辑键（白名单）。strategy.*/risk.*/universe.* 冻结期锁定（硬约束）。
_EDITABLE_KEYS: dict[str, type] = {
    "ai_summary.enabled": bool,
    "heatmap.enabled": bool,
    "alert.email.enabled": bool,
    "capital_guard.enabled": bool,        # ⚠️ 可改但强警告
    "capital_guard.top_n_enabled": bool,  # ⚠️ 可改但强警告
    "heartbeat.healthchecks_url": str,
}

# 元数据：label 中文名 / description 说明 / recommendation 建议 /
# badge（ok=建议开启 | warn=冻结期谨慎 | lock=冻结期锁定）/ editable / group
_SETTINGS_META: dict[str, dict] = {
    "alert.email.enabled": {
        "label": "邮件通知", "group": "通知", "badge": "ok", "editable": True,
        "description": "事件与每日综合日报邮件（需配置发件邮箱与授权码）",
        "recommendation": "✅ 建议开启（配置授权码后，事件+日报通知）",
    },
    "heartbeat.healthchecks_url": {
        "label": "缺席心跳（healthchecks.io）", "group": "通知", "badge": "ok",
        "editable": True, "type": "str",
        "description": "进程死亡/巡检缺席时外部告警；留空 = 不启用",
        "recommendation": "✅ 建议配置（进程死亡缺席告警，免费）",
    },
    "ai_summary.enabled": {
        "label": "AI 收盘总结", "group": "AI 总结", "badge": "ok", "editable": True,
        "description": "纯展示层，不影响交易决策；每日一次 LLM 调用生成今日简报",
        "recommendation": "✅ 建议开启（纯展示层，不影响交易，每日约 0.002 元）",
    },
    "heatmap.enabled": {
        "label": "市场热力图", "group": "热力图", "badge": "ok", "editable": True,
        "description": "看板热力图页（日级刷新，基于巡检快照，无副作用）",
        "recommendation": "✅ 建议开启（纯展示，无副作用）",
    },
    "capital_guard.enabled": {
        "label": "资金守卫", "group": "资金控制", "badge": "warn", "editable": True,
        "description": "可用现金低于阈值时跳过全部买入尝试（汇总一条告警）",
        "recommendation": "⚠️ 冻结期建议关闭（改变开仓节奏，100 笔评审后再评估）",
    },
    "capital_guard.top_n_enabled": {
        "label": "Top-N 信号筛选", "group": "资金控制", "badge": "warn", "editable": True,
        "description": "资金紧张时按信号评分只尝试前 N 只（会改变买入选择）",
        "recommendation": "⚠️ 冻结期建议关闭（改变选择逻辑，100 笔评审后再评估）",
    },
    "strategy": {
        "label": "策略参数", "group": "策略与风控", "badge": "lock", "editable": False,
        "description": "CherryClaw 三层过滤参数",
        "recommendation": "🔒 冻结期锁定，只读（100 笔评审后解锁）",
    },
    "risk": {
        "label": "风控参数", "group": "策略与风控", "badge": "lock", "editable": False,
        "description": "止损/仓位/停手铁律",
        "recommendation": "🔒 冻结期锁定，只读（100 笔评审后解锁）",
    },
    "universe": {
        "label": "股票池参数", "group": "策略与风控", "badge": "lock", "editable": False,
        "description": "分层抽样池构建参数",
        "recommendation": "🔒 冻结期锁定，只读（100 笔评审后解锁）",
    },
}

_SECRET_KEYS = ("ai_summary_api_key", "email_auth_code")


def _secrets_file() -> Path:
    return DATA_DIR / "secrets.json"


def _read_secrets() -> dict:
    return _read_json(_secrets_file())


def _mask(v) -> str:
    v = str(v or "")
    if not v:
        return ""
    return (v[:3] + "*" * 6 + v[-4:]) if len(v) > 8 else "*" * len(v)


def _yaml_fmt(v) -> str:
    import json as _json

    if isinstance(v, bool):
        return "true" if v else "false"
    return _json.dumps(v, ensure_ascii=False)   # str → 带引号转义


def _yaml_get(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _yaml_set_value(text: str, dotted: str, value) -> tuple[str, bool]:
    """按点路径修改 yaml 已有标量键（行级手术，保留全部注释与缩进）。

    只改值不改结构：未命中键（不存在/非标量行）→ 返回 (原文, False)。
    """
    import re as _re

    parts = dotted.split(".")
    stack: list[tuple[int, str]] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _re.match(
            r"^(\s*)([A-Za-z_][\w\-]*):(\s*)([^#]*?)(\s*#.*)?$", line)
        if not m:
            continue   # 列表项/空行/文本行不动栈
        indent, name = len(m.group(1)), m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, name))
        if [s[1] for s in stack] == parts and m.group(4).strip():
            lines[i] = (f"{m.group(1)}{name}:{m.group(3)}"
                        f"{_yaml_fmt(value)}{m.group(5) or ''}")
            return "\n".join(lines), True
    return text, False


def _atomic_write(path: Path, content: str) -> None:
    """原子写：临时文件 + os.replace（防写一半崩溃留下损坏配置）。"""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _audit(items: list[dict]) -> None:
    """追加审计记录到 data/settings_history.jsonl（时间/改动项/旧值→新值）。"""
    from datetime import datetime

    path = DATA_DIR / "settings_history.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for it in items:
                it = {"ts": datetime.now().isoformat(timespec="seconds"), **it}
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    except OSError as e:
        raise HTTPException(500, f"审计记录写入失败：{e}")


@app.get("/api/settings")
async def settings_view():
    """设置页数据：当前值 + 元数据（中文名/说明/建议/可改/分组）+ 密钥掩码。"""
    try:
        st = _fresh_settings()
    except Exception as e:
        return {"error": f"配置读取失败：{e}"}

    values = {
        "ai_summary.enabled": st.ai_summary.enabled,
        "heatmap.enabled": st.heatmap.enabled,
        "alert.email.enabled": st.alert.email.enabled,
        "heartbeat.healthchecks_url": st.heartbeat.healthchecks_url,
        "capital_guard.enabled": st.capital_guard.enabled,
        "capital_guard.top_n_enabled": st.capital_guard.top_n_enabled,
    }
    sec = _read_secrets()
    return {
        "settings": values,
        # 冻结期锁定的参数组（只读展示）
        "locked": {
            "strategy": st.strategy.model_dump(),
            "risk": st.risk.model_dump(),
            "universe": st.universe.model_dump(),
        },
        "metadata": _SETTINGS_META,
        "secrets": {
            "ai_summary_api_key": _mask(sec.get("ai_summary_api_key")),
            "email_auth_code": _mask(sec.get("email_auth_code")),
        },
        "note": ("保存后于下次巡检生效（每日 16:30 或手动 --run-now），无需重启容器。"
                 "策略/风控/股票池参数冻结期锁定（100 笔纸面样本评审后解锁）。"),
    }


@app.post("/api/settings")
async def settings_update(payload: dict):
    """保存设置。硬约束：

    - 必须带 "confirm": true（防误触，前端"预览变更→确认应用"两步）；
    - 仅接受白名单键（_EDITABLE_KEYS）；strategy/risk/universe 一律 400；
    - settings.yaml 行级手术修改 + 原子写（保留注释）；
    - 密钥写 data/secrets.json（两容器共享卷）；
    - 全量审计到 data/settings_history.jsonl。
    """
    if not isinstance(payload, dict) or payload.get("confirm") is not True:
        raise HTTPException(400, '缺少 "confirm": true（防误触，请走"预览变更→确认应用"流程）')

    changes = payload.get("changes") or {}
    secrets = payload.get("secrets") or {}
    if not isinstance(changes, dict) or not isinstance(secrets, dict):
        raise HTTPException(400, "changes/secrets 须为对象")
    if not changes and not secrets:
        raise HTTPException(400, "无变更内容")

    # 白名单 + 类型校验（冻结期锁：锁键一律拒绝）
    for key, val in changes.items():
        if key not in _EDITABLE_KEYS:
            raise HTTPException(400, f"{key} 冻结期锁定或不存在，禁止修改"
                                     f"（strategy.*/risk.*/universe.* 只读）")
        want = _EDITABLE_KEYS[key]
        if want is bool and not isinstance(val, bool):
            raise HTTPException(400, f"{key} 须为布尔值")
        if want is str and not isinstance(val, str):
            raise HTTPException(400, f"{key} 须为字符串")
    for key in secrets:
        if key not in _SECRET_KEYS:
            raise HTTPException(400, f"未知密钥项 {key}")
    for key, val in secrets.items():
        if not isinstance(val, str):
            raise HTTPException(400, f"{key} 须为字符串")

    audit: list[dict] = []
    applied: list[str] = []

    # ---- settings.yaml：行级手术 + 原子写 ----
    if changes:
        import yaml as _yaml

        path = _settings_yaml()
        if not path.exists():
            raise HTTPException(500, f"settings.yaml 不存在：{path}")
        text = path.read_text(encoding="utf-8")
        data = _yaml.safe_load(text) or {}
        for key, val in changes.items():
            old = _yaml_get(data, key)
            new_text, hit = _yaml_set_value(text, key, val)
            if not hit:
                raise HTTPException(400, f"{key} 在 settings.yaml 中未命中（结构变更请联系管理员）")
            text = new_text
            audit.append({"item": key, "old": old, "new": val})
            applied.append(key)
        _atomic_write(path, text)

    # ---- secrets.json：合并写入（原子） ----
    if secrets:
        merged = _read_secrets()
        for key, val in secrets.items():
            merged[key] = val
            # 审计只记掩码占位，绝不落明文
            audit.append({"item": f"secrets.{key}", "old": "***",
                          "new": "***" if val else "（清空）"})
        _atomic_write(_secrets_file(), json.dumps(merged, ensure_ascii=False, indent=1))
        applied.extend(f"secrets.{k}" for k in secrets)

    _audit(audit)

    # web 自身也用最新配置（/api/config 等即时反映）
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from lihu_quantify.config import get_settings

        get_settings.cache_clear()
    except Exception:
        pass

    return {"ok": True, "applied": applied,
            "effective": "已保存，将于下次巡检生效（16:30 或 --run-now），无需重启容器"}


@app.post("/api/settings/test_ai_key")
def settings_test_ai_key(payload: dict):
    """需求3（第十轮）：测试 AI key 有效性（一次极小请求：GET /models）。

    P2-7（第十一轮）：原 async def 内同步 requests.get(timeout=10) 网络不通时
    阻塞整个事件循环 10s → 改同步 def，FastAPI 自动丢线程池，不阻塞其他请求。
    """
    import requests

    key = str((payload or {}).get("api_key") or "").strip()
    if not key:
        key = str(_read_secrets().get("ai_summary_api_key") or "").strip()
    if not key:
        return {"ok": False, "detail": "未提供 key（且 secrets.json 无存储）"}
    try:
        st = _fresh_settings()
        base = str(st.ai_summary.api_base).rstrip("/")
        r = requests.get(f"{base}/models",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code == 200:
            return {"ok": True, "detail": "key 有效（服务可达）"}
        return {"ok": False, "detail": f"HTTP {r.status_code}：key 无效或无权限"}
    except Exception as e:
        return {"ok": False, "detail": f"连接失败：{e}"}


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

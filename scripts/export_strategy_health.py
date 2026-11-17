"""导出「策略健康」快照（只读契约，供 dsh-invest 投研工作台消费）。

为什么需要这个文件
------------------
用户要把 LihuQuantify 当作 **dsh-invest 的补充**：dsh-invest 是主体入口，Lihu 负责
跑模拟盘验证收益。合并方式定为「dsh-invest 原生视图 + 只读数据契约」，
而不是嵌 iframe、也不共用账本——**两边账本各自独立，Lihu 永远是 paper（模拟盘）**。

设计约束（每条都有来历）
------------------------
1. **必须带 schema_version**：Lihu 原有状态 JSON 是裸读、解析失败返回 ``{}``，
   字段一改消费端就静默空白（见 merge-survey/C_data_conflicts.md）。
   消费端必须能判断"这份数据我认识不认识"。
2. **必须标 account_type="paper"**：dsh-invest 的 ``.dsh-invest/持仓.json`` 是实盘
   （用户与 AI 确认过的），两者**严禁混账**（host.js:79 有硬规则）。快照里显式标注，
   消费端据此在 UI 上打「模拟盘」标签。
3. **每个分块独立容错**：任一数据源缺失只让该块为 null 并在 ``missing`` 里留痕，
   不允许整份快照失败——否则工作台会一块坏数据全白屏。
4. **只读**：本脚本不写任何业务数据，只产出 outputs/strategy_health.json。

用法
----
    python scripts/export_strategy_health.py
    python scripts/export_strategy_health.py --out outputs/strategy_health.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCHEMA_VERSION = 3
OUT_DEFAULT = ROOT / "outputs" / "strategy_health.json"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _block_equity(limit_days: int = 250) -> tuple[dict | None, list[str]]:
    """权益块（v2 新增）：模拟盘真实权益序列 + 回撤水位。

    数据来自 data/equity_history.jsonl：历史段由交割单重建（reconstructed）、
    巡检段是真实记账值（live）。两者在块里分开计数，消费端必须能看出哪段是补的。
    """
    try:
        from lihu_quantify.monitor.equity_history import (
            EQUITY_FILE, load_points, summarize,
        )
    except Exception:  # noqa: BLE001
        return None, ["equity_history"]
    try:
        points = load_points(ROOT / EQUITY_FILE)
    except Exception:  # noqa: BLE001
        return None, ["equity_history"]
    if not points:
        return None, ["equity_history"]
    s = summarize(points)
    s["points"] = [{"d": p["date"], "v": round(p["total_asset"], 2)} for p in points[-limit_days:]]
    s["truncated_days"] = max(0, len(points) - limit_days)
    return s, []


def _block_risk(settings: dict, state: dict | None, equity: dict | None = None) -> tuple[dict | None, list[str]]:
    """风控块：单票上限 / 持仓下限 / 集中度 / 回撤水位。"""
    missing: list[str] = []
    risk_cfg = (settings.get("risk") or {})
    max_single = float(risk_cfg.get("max_single_position", 0.0) or 0.0)
    min_pos = int(risk_cfg.get("min_concurrent_positions", 0) or 0)
    mdd_limit = float(risk_cfg.get("mdd_limit", 0.0) or 0.0)

    out: dict = {
        "max_single_position": max_single,
        "min_concurrent_positions": min_pos,
        "mdd_limit": mdd_limit,
        "n_positions": None,
        "max_single_weight": None,
        "current_drawdown": None,
        "status": "unknown",
    }
    if not state:
        missing.append("paper_state")
        return out, missing

    positions = state.get("positions") or {}
    total_asset = float((state.get("asset") or {}).get("total_asset") or 0.0)
    out["n_positions"] = len(positions)
    if total_asset > 0 and positions:
        weights = []
        for code, p in positions.items():
            vol = float(p.get("volume") or 0)
            cost = float(p.get("cost") or 0)
            # 用成本价近似市值（快照不另拉行情，避免本脚本依赖联网）
            weights.append((code, vol * cost / total_asset))
        if weights:
            code, w = max(weights, key=lambda x: x[1])
            out["max_single_weight"] = round(w, 4)
            out["largest_position"] = code

    # 账户层回撤：优先用 data/equity_history.jsonl 的真实序列（v2 起可用）。
    # 拿 paper_state.high_water_mark 算组合回撤是错的——那是每只股票的价格高点
    # （{"000429.SZ": 13.78}），算出来会是个看起来很像真的假数字。
    if equity and equity.get("current_drawdown") is not None:
        out["current_drawdown"] = equity["current_drawdown"]
        out["max_drawdown"] = equity.get("max_drawdown")
        out["equity_days"] = equity.get("n_points")
        out["live_days"] = equity.get("live_days")
        out["reconstructed_days"] = equity.get("reconstructed_days")
    else:
        out["current_drawdown"] = None
        out["drawdown_note"] = "尚无权益历史（data/equity_history.jsonl 为空）"

    # 状态判定：回撤触线 > 单票超限 > 集中度不足
    if (mdd_limit and out.get("current_drawdown") is not None
            and out["current_drawdown"] <= -mdd_limit):
        out["status"] = "breach"
    elif out["max_single_weight"] is not None and out["max_single_weight"] > max_single + 1e-9:
        out["status"] = "breach"
    elif out["n_positions"] < min_pos:
        out["status"] = "warn"
    else:
        out["status"] = "ok"
    return out, missing


def _block_behavior(report: dict | None) -> tuple[dict | None, list[str]]:
    if not report:
        return None, ["behavior_report"]
    keep = ("fills", "rounds", "loss_add_ratio", "turnover",
            "peak_capture", "post_sell_upside", "position_sizing", "post_entry")
    out = {k: report.get(k) for k in keep if k in report}
    out["_generated_at"] = report.get("_generated_at") or report.get("_cached_at")
    return out, []


def _block_swing() -> tuple[dict | None, list[str]]:
    """波段池块（v3 新增）：当前池子 + 三年回测 + 随机对照。"""
    try:
        from lihu_quantify.monitor.swing_snapshot import build_swing_block
    except Exception:  # noqa: BLE001
        return None, ["swing_pool"]
    try:
        block = build_swing_block(ROOT)
    except Exception:  # noqa: BLE001
        return None, ["swing_pool"]
    missing = []
    if not block.get("pool"):
        missing.append("swing_pool_md")
    if not block.get("backtest"):
        missing.append("swing_backtest")
    if not block.get("robustness"):
        missing.append("swing_montecarlo")
    return block, missing


def _block_research() -> tuple[dict | None, list[str]]:
    """研究纪律登记册：判决分布 + 最近判定。"""
    try:
        sys.path.insert(0, str(ROOT))
        from research.registry import load_registry, summarize

        entries = load_registry()
        stats = summarize(entries)
        return {
            **stats,
            "decisions": [
                {"id": e.get("id"), "title": e.get("title"), "status": e.get("status"),
                 "decided_at": e.get("decided_at")}
                for e in entries
            ],
        }, []
    except Exception:  # noqa: BLE001 - 登记册坏了不该拖垮整份快照
        return None, ["hypothesis_registry"]


def _block_strategy(settings: dict) -> dict:
    strat = settings.get("strategy") or {}
    # main_strategy 在 settings.yaml 顶层（不在 strategy: 段内），别读错层
    main = settings.get("main_strategy") or strat.get("main_strategy")
    shadows = settings.get("shadow_books") or []
    found = 0
    for s in shadows:
        name = (s or {}).get("name")
        if not name:
            continue
        if (ROOT / "data" / f"paper_state_{name}.json").exists():
            found += 1
    return {
        "main": main,
        "market_filter": strat.get("market_filter"),
        "market_filter_mode": strat.get("market_filter_mode"),
        "shadow_declared": len(shadows),
        "shadow_found": found,
        # 影子账本声明与实物不符是已知漂移（merge-survey/B 勘测发现），显式抬到台面上
        "shadow_drift": len(shadows) - found,
    }


def _findings(risk: dict | None, research: dict | None, strategy: dict,
              equity: dict | None = None, swing: dict | None = None) -> list[dict]:
    out: list[dict] = []
    if risk:
        if risk["status"] == "breach":
            out.append({"level": "error", "text": "风控触线：回撤或单票权重已越硬上限"})
        elif risk["status"] == "warn":
            out.append({"level": "warn",
                        "text": f"分散度不足：持仓 {risk['n_positions']} 只 < 下限 "
                                f"{risk['min_concurrent_positions']} 只"})
        if risk.get("max_single_weight") is not None and risk["max_single_weight"] > risk["max_single_position"]:
            out.append({"level": "error",
                        "text": f"单票权重 {risk['max_single_weight']:.1%} 超上限 "
                                f"{risk['max_single_position']:.0%}"})
    if equity:
        mdd = equity.get("max_drawdown")
        if mdd is not None and mdd <= -0.25:
            out.append({"level": "error",
                        "text": f"历史最大回撤 {mdd:.1%}（{equity.get('max_drawdown_peak_date')} → "
                                f"{equity.get('max_drawdown_trough_date')}）已越 25% 硬约束"})
        if equity.get("reconstructed_days"):
            out.append({"level": "info",
                        "text": f"权益曲线前 {equity['reconstructed_days']} 个交易日由交割单重建，"
                                f"非当日实时记账（live {equity.get('live_days', 0)} 天）"})
    if research and research.get("pending"):
        out.append({"level": "info",
                    "text": f"有 {research['pending']} 条假设仍待判定"})
    if swing and swing.get("strategy_status") == "falsified":
        out.append({"level": "error",
                    "text": "波段池策略已证伪：扩样本后为负 alpha，暂停实盘、仅纸面跟踪；"
                            "重启需预注册 + 纸面/小额 >=12 个月且 >=30 笔 + 前向 PF>=1.2 且胜率>=35%"})
    if strategy.get("shadow_drift"):
        out.append({"level": "warn",
                    "text": f"影子账本声明 {strategy['shadow_declared']} 本、实际只有 "
                            f"{strategy['shadow_found']} 本（缺 {strategy['shadow_drift']} 本）"})
    return out


def build_snapshot() -> dict:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    state = _read_json(ROOT / "data" / "paper_state.json")
    behavior_raw = _read_json(ROOT / "outputs" / "behavior_report.json")

    missing: list[str] = []
    equity, m0 = _block_equity()
    risk, m1 = _block_risk(settings, state, equity)
    behavior, m2 = _block_behavior(behavior_raw)
    research, m3 = _block_research()
    swing, m4 = _block_swing()
    missing += m0 + m1 + m2 + m3 + m4
    strategy = _block_strategy(settings)

    return {
        "schema_version": SCHEMA_VERSION,
        "source": "LihuQuantify",
        "account_type": "paper",          # 硬约束：与 dsh-invest 的实盘账本严禁混账
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategy": strategy,
        "equity": equity,
        "risk": risk,
        "behavior": behavior,
        "research": research,
        "swing": swing,
        "findings": _findings(risk, research, strategy, equity, swing),
        "missing": missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()
    snap = build_snapshot()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写出 {out}")
    print(f"  schema_version={snap['schema_version']} account_type={snap['account_type']}")
    print(f"  风控状态={ (snap['risk'] or {}).get('status') }"
          f" 持仓={(snap['risk'] or {}).get('n_positions')}"
          f" 研究={snap['research'] and (str(snap['research'].get('passed')) + ' passed / ' + str(snap['research'].get('failed')) + ' failed')}")
    print(f"  缺失块={snap['missing'] or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

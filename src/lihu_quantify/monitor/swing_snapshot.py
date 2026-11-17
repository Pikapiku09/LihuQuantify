"""波段池快照解析（供工作台「量化策略」页使用，2026-11-17，P3）。

与 ``monitor/swing_pool.py`` 的分工
----------------------------------
``swing_pool.py`` 是**周报邮件渲染器**（HTML + 陈旧护栏 + EmailAlerter），不要动它。
本模块只做一件事：把同一份 md 与回测/对照 JSON 解析成**契约块**给前端。
两者共用 ``swing_pool._pick_pool_file()`` 选文件——否则页面和邮件可能看到两份不同快照
（NAS 推送件与本地件 mtime 不同，正是 2026-11-13 那次「三周发同一封」的根因）。

三个数据源各自的坑
------------------
1. ``data/swing_pool/波段池_持仓.md``：顶部有纪律声明与「暂停实盘」决定。
   解析**必须按表头名字取列**，不能按下标——这张表历史上加过列，按下标会错位且静默出错。
2. ``outputs/swing_backtest.json``：2026-10-20 那版已是**证伪后**的数字
   （总收益 −52.02%、年化 −12.37%、MDD 62.18%）。原样呈现，不许挑好看的字段；
   自带的 7 条 disclaimers 必须透传，其中明确写着「训练段参考，非实盘口径」。
3. ``outputs/swing_montecarlo*.json``：存在 3 年与 5.7 年两个窗口，数字差别很大
   （3 年窗随机对照是正的，5.7 年窗是负的）。只看一份会以为结论不一致；
   两份其实都在说同一件事：**策略比随机还差**。所以全部呈现并标出同窗口那份。

设计原则：解析失败一律降级为「原文片段 + parse_ok=False」，不静默返回空表。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

POOL_FILE = Path("data") / "swing_pool" / "波段池_持仓.md"
BACKTEST_FILE = Path("outputs") / "swing_backtest.json"
MC_FILES = ("swing_montecarlo_2021.json", "swing_montecarlo.json")

# 「池内状态」表按表头名字取这些列；缺列则该项为 None，不错位
_WANT = {
    "代码": "code",
    "名称": "name",
    "所属池": "pool",
    "现价": "price",
    "目标价": "target",
    "止损价": "stop",
    "状态": "state",
    "下次复核": "next_check",
}


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _clean(text: str) -> str:
    """去掉 markdown 强调与多余空白，保留可读中文。"""
    out = text.replace("**", "").replace("`", "").strip()
    return " ".join(out.split())


def parse_pool_markdown(text: str) -> dict:
    """解析波段池 md：状态行 + 池内状态表。解析不到的字段留 None，不猜。"""
    lines = text.splitlines()
    statements: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith(">") and ("⚠" in s or "决定" in s or "纪律" in s or "当前状态" in s):
            statements.append(_clean(s.lstrip("> ")))

    holdings: list[dict] = []
    parse_ok = False
    header: Optional[list[str]] = None
    in_section = False
    for line in lines:
        s = line.strip()
        if s.startswith("## ") and "池内状态" in s:
            in_section = True
            continue
        if in_section and s.startswith("## "):
            break
        if not in_section or not s.startswith("|"):
            continue
        row = _cells(s)
        if header is None:
            if "代码" in row:
                header = row
            continue
        if all(set(c) <= {"-", ":", " "} for c in row if c):
            continue                       # 分隔行
        if not any(row):
            continue
        rec: dict[str, Any] = {}
        for idx, title in enumerate(header):
            key = _WANT.get(_clean(title))
            if key and idx < len(row):
                rec[key] = _clean(row[idx]) or None
        if rec.get("code"):
            holdings.append(rec)
            parse_ok = True

    as_of = None
    for line in lines:
        s = line.strip()
        if s.startswith("## ") and "池内状态" in s:
            for seg in s.split("**"):
                if "收盘" in seg:
                    as_of = _clean(seg)
                    break
    return {
        "statements": statements,
        "holdings": holdings,
        "parse_ok": parse_ok,
        "as_of": as_of,
        "raw_excerpt": "\n".join(lines[:14]),
    }


def _resolve_pool_path(path: Optional[Path]) -> Optional[Path]:
    """复用周报那边的选文件逻辑（取 mtime 最新），保证页面与邮件看同一份快照。"""
    if path is not None:
        return path if path.exists() else None
    try:
        from .swing_pool import _pick_pool_file

        return _pick_pool_file()
    except Exception:  # noqa: BLE001 - 邮件模块不可用时退回固定路径
        return POOL_FILE if POOL_FILE.exists() else None


def load_pool(path: Optional[Path] = None) -> Optional[dict]:
    target = _resolve_pool_path(path)
    if target is None or not target.exists():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return None
    data = parse_pool_markdown(text)
    data["exists"] = True
    data["updated_at"] = target.stat().st_mtime
    return data


def load_backtest(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    keep = ("generated_at", "params", "metrics", "yearly", "funnel",
            "exit_reasons", "concentration", "disclaimers")
    return {k: d.get(k) for k in keep if k in d}


def load_robustness(out_dir: Path, backtest: Optional[dict]) -> Optional[dict]:
    """随机对照，**把所有窗口都拿出来**。

    outputs 下同时有 3 年窗（2023-10 起）与 5.7 年窗（2021-01 起）两份：
      3 年窗：策略 −48.8%，同择时随机 +2.8%、完全随机 +2.9%
      5.7 年窗：策略 −52.0%，随机 −27.0% / −10.4%（策略仍在分布底部）
    只看一份会以为「随机对照结论不一致」。一并呈现，并标出与头条回测同窗口的那份。
    """
    win = list(((backtest or {}).get("params") or {}).get("window") or [])
    seen: set[tuple] = set()
    windows: list[dict] = []
    for name in MC_FILES:
        p = out_dir / name
        if not p.exists():
            continue
        try:
            mc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        w = list(mc.get("window") or [])
        key = tuple(w)
        if key in seen:
            continue
        seen.add(key)
        baseline = ((mc.get("baseline") or {}).get("metrics") or {})
        controls = {}
        for k, v in (mc.get("controls") or {}).items():
            controls[k] = {
                "mean": v.get("mean"),
                "baseline_percentile": v.get("baseline_percentile"),
                "q5": (v.get("q") or {}).get("p5"),
                "q95": (v.get("q") or {}).get("p95"),
            }
        windows.append({
            "window": w,
            "val_window_days": mc.get("val_window_days"),
            "baseline_total_return": baseline.get("total_return"),
            "controls": controls,
            "month_cap": mc.get("month_cap"),
            "matched_headline": bool(win) and w == win,
            "file": name,
        })
    if not windows:
        return None
    windows.sort(key=lambda x: (not x["matched_headline"], x["window"]))
    return {"windows": windows}


def build_swing_block(root: Path) -> dict:
    """组装契约里的 swing 块。任一分块缺失只让该块为 None，不影响其他。"""
    pool = load_pool()
    backtest = load_backtest(root / BACKTEST_FILE)
    robustness = load_robustness(root / "outputs", backtest)
    return {
        "pool": pool,
        "backtest": backtest,
        "robustness": robustness,
        # 把「是否证伪」抬成一级字段：消费端不必自己从文案里猜
        "strategy_status": "falsified" if _looks_falsified(pool, backtest) else "unknown",
    }


def _looks_falsified(pool: Optional[dict], backtest: Optional[dict]) -> bool:
    """两条独立证据任一成立即视为证伪，避免依赖单一文本描述。"""
    if pool:
        blob = " ".join(pool.get("statements") or [])
        if "证伪" in blob or "暂停实盘" in blob:
            return True
    if backtest:
        m = backtest.get("metrics") or {}
        total = m.get("total_return")
        if isinstance(total, (int, float)) and total < 0:
            return True
    return False

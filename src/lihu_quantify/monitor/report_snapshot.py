"""巡检日报快照（供工作台「量化策略」页折叠区使用，2026-11-17，P4）。

为什么只抽字段、不整篇塞进契约
------------------------------
``outputs/reports/`` 下有 55 份日报，每份约 4 KB。整篇下发是 200+ KB，而工作台要的只是
「最近几天怎么样、有没有铁律被打破」。所以只抽：

- 日期、市场状态
- 总资产、累计盈亏、评审进度
- 今日操作摘要（无买入/无卖出/无被拒信号 这类）
- **铁律自检的勾选结果**（这是最值得看的一栏：哪一条没打勾就是问题）

解析原则：某个字段缺失就返回 None，绝不猜、绝不用上一份的值补。日报格式变过几次，
硬推断会让页面显示一个不存在的「0 笔卖出」。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

REPORTS_DIR = Path("outputs") / "reports"
_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_TITLE_RE = re.compile(r"^#\s*每日巡检报告\s*(\d{4}-\d{2}-\d{2})")
_MARKET_RE = re.compile(r"市场状态[：:]\s*\*{0,2}([^\n*|]+)")
_NUM = r"([-\d,]+(?:\.\d+)?)"


def _num(text: str) -> Optional[float]:
    try:
        return float(text.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def _table_row(text: str, label: str) -> Optional[str]:
    m = re.search(rf"\|\s*{label}\s*\|\s*([^|\n]+?)\s*\|", text)
    return m.group(1).strip() if m else None


def _section(text: str, heading: str) -> str:
    """取 ``## heading`` 到下一个 ``##`` 之间的正文。"""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        s = line.strip()
        if s.startswith("## "):
            if inside:
                break
            if heading in s:
                inside = True
                continue
        if inside:
            out.append(line)
    return "\n".join(out)


def parse_report(text: str, name: str) -> dict:
    title = _TITLE_RE.search(text)
    market = _MARKET_RE.search(text)
    # 日期以**文件名**为准（它是排序索引），标题里的日期另存以便交叉核对。
    # 两者不一致通常意味着日报被复制或改名，这本身是要暴露的问题，不能悄悄取其一。
    file_date = name[:10] if _NAME_RE.match(name) else None
    title_date = title.group(1) if title else None
    date = file_date or title_date
    date_mismatch = bool(file_date and title_date and file_date != title_date)

    total = _num(_table_row(text, "总资产") or "")
    pnl_cell = _table_row(text, "累计盈亏")
    pnl_ratio = None
    if pnl_cell:
        m = re.search(r"\(\s*([-+]?\d+(?:\.\d+)?)%\s*\)", pnl_cell)
        if not m:
            m = re.search(r"（\s*([-+]?\d+(?:\.\d+)?)%\s*）", pnl_cell)
        if m:
            pnl_ratio = round(float(m.group(1)) / 100.0, 4)

    review_cell = _table_row(text, "评审进度")
    positions = None
    pos_section = _section(text, "当前持仓")
    if pos_section:
        rows = [ln for ln in pos_section.splitlines()
                if ln.strip().startswith("|") and not re.match(r"^\|[\s:|-]+\|$", ln.strip())]
        positions = max(0, len(rows) - 1) if rows else None    # 去掉表头

    ops_section = _section(text, "今日操作")
    # 「今日无买入。」这类是裸段落，不是列表项，所以两条路径都要接
    ops_parts = []
    for ln in ops_section.splitlines():
        s = ln.strip()
        if not s or s.startswith("|") or s.startswith("<"):
            continue
        s = s.lstrip("- ").strip().rstrip("。")
        if s:
            ops_parts.append(s)
    ops = " / ".join(ops_parts) or None

    checks = []
    for m in re.finditer(r"^-\s*\[([ xX])\]\s*(.+)$", text, re.MULTILINE):
        checks.append({"ok": m.group(1).lower() == "x", "text": m.group(2).strip()})

    return {
        "date": date,
        "title_date": title_date,
        "date_mismatch": date_mismatch,
        "report_name": name,
        "market_state": (market.group(1).strip() if market else None),
        "total_asset": total,
        "pnl_ratio": pnl_ratio,
        "review": review_cell,
        "positions": positions,
        "ops": ops,
        "checks": checks,
        "checks_failed": [c["text"] for c in checks if not c["ok"]],
    }


def load_reports(root: Path, limit: int = 12) -> dict:
    """返回 {total, recent}。只认严格 YYYY-MM-DD.md，非日报一律跳过。"""
    reports_dir = root / REPORTS_DIR
    if not reports_dir.exists():
        return {"total": 0, "recent": []}
    files = []
    for p in reports_dir.glob("*.md"):
        if _NAME_RE.match(p.name):
            files.append(p)
    files.sort(key=lambda p: p.name, reverse=True)
    recent = []
    for p in files[:limit]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        recent.append(parse_report(text, p.name))
    return {"total": len(files), "recent": recent}

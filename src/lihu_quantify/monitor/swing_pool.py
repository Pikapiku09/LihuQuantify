"""波段池周报邮件（2026-10-16；2026-10-23 重写渲染——沿用模拟盘日报视觉体系）。

背景：dsh-invest 侧波段池（中长周期）由 DSH 会话维护
E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/波段池_持仓.md，经 scripts/_nas_swing_push.py
推送至 NAS data/swing_pool/；本模块读推送件生成 HTML 周报，复用 monitor/alerts.py
的 EmailAlerter（授权码走容器环境 LIHU_ALERT__EMAIL__AUTH_CODE，不落代码）。

渲染（2026-10-23 重写）：与 monitor/daily_report.py 同一视觉体系——深色头部 +
蓝色分节标题 + 样式化表格（红涨绿跌、数字右对齐）+ 简报门面层 + 完整池子文件折叠；
旧版"裸表格 + 整篇 <pre> dump"废止（可看性太差）。

Markdown 解析：`##` 标题分节；首个 `##` 之前的正文为「池子总览」（主持仓表）；
`|...|` 表格；`>` 引用（⚠️/决定 → 琥珀色提示框，其余 → 元信息行）；
`- ` 列表；行内 **加粗**/`代码`。解析失败的内容原样段落呈现，不丢信息。

调度：scheduler.setup_scheduler 每周五 21:00（swing_weekly）；手动测试
python run_scheduler.py --swing-now；本地预览 python -m lihu_quantify.monitor.swing_pool --preview
"""
from __future__ import annotations

import html as _html
import re
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

# src/lihu_quantify/monitor/swing_pool.py 向上 3 级 = 项目根（与 scheduler._ROOT 同口径）
_ROOT = Path(__file__).resolve().parents[3]
POOL_PATH = _ROOT / "data" / "swing_pool" / "波段池_持仓.md"
# 本地预览兜底：NAS 挂载卷之外，DSH 会话侧的池子文件
_POOL_FALLBACKS = [POOL_PATH, Path(r"E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/波段池_持仓.md")]

# ===== 样式（与 monitor/daily_report.py 同一视觉体系，A 股配色：红涨绿跌） =====
_COLOR_PROFIT = "#dc2626"
_COLOR_LOSS = "#16a34a"
_COLOR_FLAT = "#6b7280"

_TH = ('padding:6px 8px;border:1px solid #e2e8f0;background:#f1f5f9;'
       'text-align:left;font-weight:600;white-space:nowrap;')
_TD = 'padding:6px 8px;border:1px solid #e2e8f0;'
_TD_NUM = 'padding:6px 8px;border:1px solid #e2e8f0;text-align:right;white-space:nowrap;'
_H2 = ('font-size:15px;margin:22px 0 8px;padding-left:8px;'
       'border-left:4px solid #3b82f6;color:#111827;')
_TABLE = 'width:100%;border-collapse:collapse;font-size:13px;color:#1f2937;'

_NUM_RE = re.compile(r"^[-+]?[\d,]+(\.\d+)?%?$")


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _inline(s: str) -> str:
    """行内 Markdown：**加粗** / `代码`（先转义再替换）。"""
    t = _esc(s)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+?)`", r"<code style='background:#f1f5f9;padding:1px 4px;"
                                r"border-radius:3px;font-size:12px;'>\1</code>", t)
    return t


def _cell(s: str) -> str:
    """单元格：数值/百分比右对齐 + 红涨绿跌。"""
    t = s.strip()
    if _NUM_RE.match(t):
        color = _COLOR_FLAT
        if t.endswith("%"):
            try:
                v = float(t[:-1].replace(",", ""))
                color = _COLOR_PROFIT if v > 0 else (_COLOR_LOSS if v < 0 else _COLOR_FLAT)
            except ValueError:
                pass
        return (f"<td style='{_TD_NUM}'><span style='color:{color};font-weight:600;'>"
                f"{_inline(t)}</span></td>")
    return f"<td style='{_TD}'>{_inline(t)}</td>"


# ===== Markdown 解析 =====

def _parse_md(md: str) -> dict:
    title = None
    quotes: List[str] = []
    preamble: List[str] = []          # 首个 ## 之前的正文（主持仓表所在）
    sections: List[dict] = []
    current: Optional[dict] = None
    for raw in md.splitlines():
        t = raw.rstrip()
        if t.startswith("# ") and title is None:
            title = t[2:].strip()
            continue
        if t.startswith("## "):
            current = {"heading": t[3:].strip(), "lines": []}
            sections.append(current)
            continue
        if t.strip().startswith(">"):
            q = t.strip().lstrip(">").strip()
            if q:
                quotes.append(q)
            continue
        if t.strip() in ("---", "***"):
            continue
        if current is None:
            if t.strip():
                preamble.append(t)
            continue
        current["lines"].append(t)
    return {"title": title or "波段池持仓", "quotes": quotes,
            "preamble": preamble, "sections": sections}


def _split_table(lines: List[str]) -> Optional[Tuple[List[str], List[List[str]]]]:
    rows = []
    started = False
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            if started or s:      # 表前空行跳过；表中断行/正文即终止
                break
            continue
        started = True
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append(cells)
    if len(rows) >= 2:
        return rows[0], rows[1:]
    return None


def _render_table(headers: List[str], rows: List[List[str]]) -> str:
    th = "<tr>" + "".join(f"<th style='{_TH}'>{_inline(c)}</th>" for c in headers) + "</tr>"
    body = ""
    for r in rows:
        body += "<tr>" + "".join(_cell(c) for c in r) + "</tr>"
    return f"<table style='{_TABLE}'>{th}{body}</table>"


def _render_lines(lines: List[str]) -> str:
    """段内渲染：表格 / 列表 / 段落。"""
    out, buf = [], []

    def flush_buf():
        if buf:
            out.append(f"<p style='margin:6px 0;font-size:13px;color:#1f2937;line-height:1.7'>"
                       + "<br>".join(_inline(x) for x in buf if x.strip()) + "</p>")
            buf.clear()

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("|"):
            flush_buf()
            tbl_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i])
                i += 1
            tb = _split_table(tbl_lines)
            if tb:
                out.append(_render_table(tb[0], tb[1]))
            continue
        if s.startswith("- "):
            flush_buf()
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            lis = "".join(f"<li style='margin:3px 0'>{_inline(x)}</li>" for x in items)
            out.append(f"<ul style='margin:4px 0 8px 18px;padding:0;font-size:13px;"
                       f"color:#1f2937;line-height:1.7'>{lis}</ul>")
            continue
        if s:
            buf.append(s)
        i += 1
    flush_buf()
    return "".join(out)


def _find_status_table(preamble: List[str], sections: List[dict]):
    """找带「状态」列的主表（池子总览）。"""
    for lines in [preamble] + [s["lines"] for s in sections]:
        tb = _split_table(lines)
        if not tb:
            continue
        headers, rows = tb
        if any("状态" in h for h in headers):
            return tb
    return None


def _count_status(preamble: List[str], sections: List[dict]) -> dict:
    counts = {"观察": 0, "建仓中": 0, "持有": 0, "减仓中": 0, "清仓": 0, "rows": 0}
    tb = _find_status_table(preamble, sections)
    if not tb:
        return counts
    headers, rows = tb
    idx = next(i for i, h in enumerate(headers) if "状态" in h)
    for r in rows:
        if len(r) <= idx:
            continue
        counts["rows"] += 1
        st = r[idx]
        for k in ("建仓中", "持有", "减仓中", "清仓", "观察"):
            if k in st:
                counts[k] += 1
                break
    return counts


def _weekly_action(sections: List[dict]) -> Tuple[str, int]:
    """周检记录节 →（本周动作一句话, 动作条数）。"""
    for sec in sections:
        if "周检" in sec["heading"]:
            tb = _split_table(sec["lines"])
            if not tb:
                return "", 0
            headers, rows = tb
            if not rows:
                return "", 0
            n = sum(1 for r in rows if len(r) > 0 and r[0].strip() == rows[0][0].strip())
            r = rows[0]
            act = r[1] if len(r) > 1 else ""
            code = r[2] if len(r) > 2 else ""
            desc = r[3] if len(r) > 3 else ""
            return f"{act}｜{code}——{desc[:70]}", n
    return "", 0


def build_swing_email(pool_path: Optional[Path] = None) -> Tuple[str, str]:
    """生成波段池周报 (subject, html_body)。池子文件缺失时返回提示版（不抛异常）。"""
    path = pool_path or next((p for p in _POOL_FALLBACKS if p.exists()), POOL_PATH)
    today = date.today().isoformat()
    if not path.exists():
        return (
            f"📈 【波段池周报】{today} 池子文件未同步",
            "<div style='font-family:Microsoft YaHei,sans-serif;padding:16px'>"
            "<p>data/swing_pool/波段池_持仓.md 不存在——请在 DSH 侧更新池子后运行"
            " scripts/_nas_swing_push.py 推送。</p></div>",
        )
    md = path.read_text(encoding="utf-8")
    parsed = _parse_md(md)
    counts = _count_status(parsed["preamble"], parsed["sections"])
    action, n_actions = _weekly_action(parsed["sections"])
    stage = "纸面跟踪" if "暂停实盘" in md else "跟踪中"
    active = counts["建仓中"] + counts["持有"] + counts["减仓中"]

    subject = (f"📈 【波段池周报】{today} {stage} · 池内 {counts['rows']} 只"
               + (f" · 本周 {n_actions} 条动作" if n_actions else ""))

    # 头部（与日报同款深色）
    header = (
        '<div style="background:#0f172a;color:#ffffff;padding:16px 20px;'
        'border-radius:8px 8px 0 0;">'
        '<h1 style="margin:0;font-size:18px;">📈 LihuQuantify 波段池周报</h1>'
        f'<p style="margin:6px 0 0;font-size:13px;color:#94a3b8;">'
        f'{today} · 阶段：<b style="color:#ffffff;">{stage}</b> ｜ 池内 '
        f'<b style="color:#ffffff;">{counts["rows"]}</b> 只（纸面活跃 {active} ｜观察 {counts["观察"]}）'
        '</p></div>'
    )

    # 本周简报（蓝色左框，日报"今日简报"同款）
    brief_bits = [f"池内 {counts['rows']} 只：纸面活跃 {active} 只 + 观察 {counts['观察']} 只"]
    if action:
        brief_bits.append(f"本周动作：{action}")
    brief_bits.append("阶段：暂停实盘、纸面跟踪（2026-10-20 策略证伪，前向验证中）")
    brief = (
        '<div style="background:#eff6ff;border-left:4px solid #3b82f6;'
        'padding:10px 12px;border-radius:0 6px 6px 0;margin:12px 0;">'
        + "".join(f'<p style="margin:2px 0;font-size:13px;color:#1f2937;">{_inline(b)}</p>'
                  for b in brief_bits)
        + '</div>'
    )

    # 决策横幅（⚠️/决定 引用 → 琥珀色提示框）
    warn = [q for q in parsed["quotes"] if "⚠️" in q or "决定" in q]
    callout = ""
    if warn:
        callout = (
            '<div style="background:#fffbeb;border-left:4px solid #f59e0b;'
            'padding:10px 12px;border-radius:0 6px 6px 0;margin:8px 0;font-size:12.5px;'
            'color:#92400e;line-height:1.8;">'
            + "".join(f"<p style='margin:2px 0'>{_inline(q)}</p>" for q in warn)
            + "</div>"
        )

    # 元信息（其余引用）
    meta_lines = [q for q in parsed["quotes"] if q not in warn]
    meta = ""
    if meta_lines:
        meta = ("<p style='margin:8px 0;font-size:12px;color:#6b7280;line-height:1.8'>"
                + "<br>".join(_inline(q) for q in meta_lines) + "</p>")

    # 分节（首个 ## 之前 = 池子总览主表）
    sections_html = ""
    if parsed["preamble"]:
        sections_html += f"<h2 style='{_H2}'>池子总览</h2>"
        sections_html += _render_lines(parsed["preamble"])
    for sec in parsed["sections"]:
        sections_html += f"<h2 style='{_H2}'>{_inline(sec['heading'])}</h2>"
        sections_html += _render_lines(sec["lines"])

    # 完整池子文件（折叠）
    details = (
        '<details style="margin-top:14px"><summary style="cursor:pointer;font-size:12px;'
        'color:#6b7280;">完整池子文件（markdown 原文，点击展开）</summary>'
        '<pre style="background:#f1f5f9;padding:10px;font-size:11px;white-space:pre-wrap;'
        'border-radius:6px;margin-top:6px;">' + _esc(md) + "</pre></details>"
    )

    footer = (
        '<p style="margin:16px 0 0;font-size:12px;color:#9ca3af;'
        'border-top:1px solid #e5e7eb;padding-top:10px;">'
        "本邮件由 LihuQuantify 系统自动生成（lihu-scheduler · swing_weekly）。"
        "仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。</p>"
    )

    body = (
        "<div style='font-family:Microsoft YaHei,PingFang SC,Arial,sans-serif;"
        "max-width:820px;margin:0 auto;padding:16px;'>"
        + header
        + '<div style="padding:4px 20px 16px;background:#f8fafc;'
          'border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;">'
        + brief + callout + meta + sections_html + details + footer
        + "</div></div>"
    )
    return subject, body


def send_swing_weekly(email_alerter, pool_path: Optional[Path] = None) -> bool:
    """发送波段池周报（直接走 EmailAlerter 邮件通道，不经告警级别过滤）。"""
    if email_alerter is None:
        logger.warning("[波段池周报] 邮件通道未配置（alert.email），跳过")
        return False
    subject, html_body = build_swing_email(pool_path)
    ok = email_alerter.send(subject, html_body, html=True)
    logger.info(f"[波段池周报] {'已发送' if ok else '发送失败'}: {subject}")
    return ok


if __name__ == "__main__":
    # 本地预览：python -m lihu_quantify.monitor.swing_pool --preview
    subject, body = build_swing_email()
    out = _ROOT / "outputs" / "preview_swing_weekly.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"<title>{_esc(subject)}</title>\n{body}", encoding="utf-8")
    print("subject:", subject)
    print("preview:", out)

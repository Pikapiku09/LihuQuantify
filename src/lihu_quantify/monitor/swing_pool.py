"""波段池周报邮件（2026-10-16，dsh-invest 波段池配套）。

背景：dsh-invest 侧波段池（中长周期，持有 3 周-3 个月、持仓<5 只）由 DSH 会话维护
E:\\Dsh_WorkSapce\\Dify_Agents\\.dsh-invest\\波段池_持仓.md，经 scripts/_nas_swing_push.py
推送至 NAS data/swing_pool/；本模块读推送件生成 HTML 周报，复用 monitor/alerts.py
的 EmailAlerter（授权码走容器环境 LIHU_ALERT__EMAIL__AUTH_CODE，不落代码）。

调度：scheduler.setup_scheduler 每周五 21:00（swing_weekly）；手动测试
python run_scheduler.py --swing-now。
"""
from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from loguru import logger

# src/lihu_quantify/monitor/swing_pool.py 向上 3 级 = 项目根（与 scheduler._ROOT 同口径）
_ROOT = Path(__file__).resolve().parents[3]
POOL_PATH = _ROOT / "data" / "swing_pool" / "波段池_持仓.md"


def _first_table_rows(md_text: str) -> List[List[str]]:
    """提取 Markdown 首个表格为单元格二维列表（跳过分隔行）。"""
    rows: List[List[str]] = []
    for line in md_text.splitlines():
        t = line.strip()
        if not t.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in t.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        rows.append(cells)
    return rows


def build_swing_email(pool_path: Optional[Path] = None) -> Tuple[str, str]:
    """生成波段池周报 (subject, html_body)。池子文件缺失时返回提示版（不抛异常）。"""
    path = pool_path or POOL_PATH
    today = date.today().isoformat()
    if not path.exists():
        return (
            f"【波段池周报】{today} 池子文件未同步",
            "<p>data/swing_pool/波段池_持仓.md 不存在——请在 DSH 侧更新池子后运行"
            " scripts/_nas_swing_push.py 推送。</p>",
        )
    md = path.read_text(encoding="utf-8")
    rows = _first_table_rows(md)
    holdings = max(0, len(rows) - 1)
    subject = f"【波段池周报】{today} 持仓 {holdings} 只"
    table = ""
    if rows:
        head = "".join(f"<th>{html.escape(c)}</th>" for c in rows[0])
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>"
            for r in rows[1:]
        )
        table = (
            '<table border="1" cellpadding="6" cellspacing="0" '
            'style="border-collapse:collapse"><tr>' + head + "</tr>" + body_rows + "</table>"
        )
    html_body = (
        '<html><body style="font-family:Microsoft YaHei,sans-serif">'
        f"<h2>波段池周报 {today}</h2>"
        "<p>本周例行：复核池内标的三重共振（行业景气/估值分位/周线趋势）。"
        "纪律：持仓&lt;5 只（含观察）、每月最多 1 次调仓、止损=跌破 30 周线或 -12%~-15% "
        "或买入逻辑破（严禁套用短线 -8%/10 日线）、任一批触发止损全部离场严禁补仓、"
        "非必要可以空仓。</p>"
        + table
        + '<h3>完整池子文件</h3><pre style="background:#f6f6f6;padding:12px;'
        'white-space:pre-wrap">'
        + html.escape(md)
        + "</pre>"
        '<p style="color:#888">本邮件由 lihu-scheduler swing_weekly 任务自动发送。'
        "仅供参考，不构成投资建议。</p></body></html>"
    )
    return subject, html_body


def send_swing_weekly(email_alerter, pool_path: Optional[Path] = None) -> bool:
    """发送波段池周报（直接走 EmailAlerter 邮件通道，不经告警级别过滤）。"""
    if email_alerter is None:
        logger.warning("[波段池周报] 邮件通道未配置（alert.email），跳过")
        return False
    subject, html_body = build_swing_email(pool_path)
    ok = email_alerter.send(subject, html_body, html=True)
    logger.info(f"[波段池周报] {'已发送' if ok else '发送失败'}: {subject}")
    return ok

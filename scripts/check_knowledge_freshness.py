"""知识时效复查：KB_VERIFIED 规则超期提醒（默认 180 天复查）。

数据源：data/verified_rules.json（手工维护：规则id/结论/验证日期/复查周期天）
输出：过期清单（接 alert/serverchan 时推送微信，无 key 则日志）。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "data" / "verified_rules.json"


def main() -> None:
    if not RULES.exists():
        logger.warning(f"未找到 {RULES}——请按模板创建")
        return
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    today = date.today()
    expired = []
    for r in rules:
        vd = date.fromisoformat(r["verified_date"])
        due = vd + timedelta(days=r.get("recheck_days", 180))
        if today >= due:
            expired.append((r["id"], r["verified_date"], r["summary"][:50]))
    if expired:
        msg = "以下已验证规则超期，需重跑对应门禁脚本复核：\n" + "\n".join(
            f"  - {i}（验证于 {d}）：{s}" for i, d, s in expired)
        logger.warning(msg)
    else:
        logger.info("全部规则在有效期内")


if __name__ == "__main__":
    main()

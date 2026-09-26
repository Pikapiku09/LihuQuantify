"""dsh-invest 建议追踪闭环：登记 + 对账。

用法：
    python scripts/track_dsh_advice.py register   # 扫描 invest-outputs 新报告，提取建议登记入册
    python scripts/track_dsh_advice.py review     # 对账：满 20 个交易日的建议，用面板数据核验方向

登记：data/dsh_advice_log.jsonl（每行：date/question/stocks/advice/evidence/report_path）
对账：data/research_panel.parquet 的 close 计算 [登记日, +20日] 收益，与建议方向比对，
      输出命中率统计（注意：样本小无统计显著性，仅作趋势参考）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb
from loguru import logger

OUT_DIR = Path(r"E:\Dsh_WorkSapce\Dify_Agents\invest-outputs")
LOG = ROOT / "data" / "dsh_advice_log.jsonl"

ADVICE_PAT = re.compile(r"综合评级|建议仓位|评级=|买入|持有|观望|回避")


def extract_advice(text: str) -> list[dict]:
    """从报告文本提取个股建议行（评级表行）。"""
    out = []
    for line in text.split("\n"):
        if ("|" in line and ADVICE_PAT.search(line)) or re.match(r"^\s*-\s*(建议|结论)", line.strip()):
            if any(k in line for k in ("买入", "持有", "观望", "回避", "减仓", "加仓")):
                codes = re.findall(r"\b(\d{6})\.(SH|SZ|BJ)\b", line)
                out.append({"line": line.strip()[:200],
                            "codes": [c + "." + s for c, s in codes]})
    return out


def register() -> None:
    seen = set()
    if LOG.exists():
        seen = {json.loads(l)["report_path"] for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()}
    n_new = 0
    for d in sorted(OUT_DIR.iterdir()):
        rep = d / "报告.md"
        if not rep.exists() or str(rep) in seen:
            continue
        text = rep.read_text(encoding="utf-8", errors="ignore")
        advice = extract_advice(text)
        if not advice:
            continue
        m = re.match(r"(\d{8})_(\d{6})", d.name)
        if not m:
            continue
        rec = {"date": m.group(1), "question": text.split("\n")[3][:80] if len(text.split("\n")) > 3 else "",
               "advice": advice[:10], "report_path": str(rep)}
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_new += 1
    logger.info(f"登记完成：新 {n_new} 份（总 {len(seen) + n_new}）")


def review() -> None:
    if not LOG.exists():
        logger.warning("无登记记录")
        return
    px = duckdb.query(f"SELECT ts_code, trade_date, close FROM '{ROOT / 'data' / 'research_panel.parquet'}'").fetchdf()
    px["trade_date"] = px["trade_date"].dt.strftime("%Y%m%d")
    px_idx = {c: g.reset_index(drop=True) for c, g in px.groupby("ts_code")}
    rows = [json.loads(l) for l in LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    hit, total = 0, 0
    for rec in rows:
        for adv in rec.get("advice", []):
            bullish = "买入" in adv["line"] or "加仓" in adv["line"]
            bearish = "回避" in adv["line"] or "减仓" in adv["line"]
            if not (bullish or bearish):
                continue
            for code in adv["codes"]:
                g = px_idx.get(code)
                if g is None:
                    continue
                pos = g.index[g["trade_date"] > rec["date"]]
                if len(pos) == 0 or pos[0] + 20 >= len(g):
                    continue   # 未满20交易日，跳过
                i0 = pos[0]
                ret = g.loc[i0 + 20, "close"] / g.loc[i0, "close"] - 1.0
                ok = (ret > 0) if bullish else (ret < 0)
                hit += int(ok)
                total += 1
                logger.info(f"{rec['date']} {code} {'看多' if bullish else '看空'} 20日收益={ret:+.2%} {'✓' if ok else '✗'} | {adv['line'][:60]}")
    if total:
        logger.info(f"对账完成：命中 {hit}/{total} = {hit/total:.0%}（样本小，仅趋势参考）")
    else:
        logger.info("暂无满 20 交易日的可对账建议")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "register"
    register() if cmd == "register" else review()

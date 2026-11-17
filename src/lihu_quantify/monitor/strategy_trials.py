"""AI 结论影子跟踪：把 dsh-invest 的结论放进行情里走一遍（2026-11-17，Step 1）。

设计边界（三条，先说清楚）
--------------------------
1. **只读 dsh-invest 的 ``conclusions.jsonl``，不跨仓写。** 原方案打算让 dsh-invest 往
   Lihu 写「提案文件」，那是两个仓库互相写对方的目录 —— merge-survey 点名的隐性耦合。
   结论本身就是「策略」，Lihu 直接读它、按规则算，就能完成验证，不需要任何回写。
2. **不进主模拟盘。** 这不是「模拟盘执行」，是**单笔独立影子跟踪**：每条结论一条独立
   假设交易，不占用资金、不参与仓位约束、不影响 ``paper_state.json``。要真正的撮合
   是另一件事（P2），混在一起会让主账本的语义被污染。
3. **每次全量重算，不做增量状态机。** 账本由「结论 + 行情」完全推导得出，重算即幂等；
   增量状态机一旦某天漏跑就会永久偏移，而重算永远不会。

两类产出，因为只有 5 条是买入信号
--------------------------------
实测 91 条结论的 stance 分布：观察 45 / 持有 17 / 减仓 15 / 拒绝 7 / **看多 5** / 离场 1。
只统计「买入后如何」的话样本只有 5 条，没有意义。所以每条结论产出两样：

- **方向命中**：按 stance 推断期望方向（看多为涨、减仓/离场/拒绝为跌、观察/持有中性），
  看拿掉前视偏差后的 5/10 日实际涨跌，判定有没有猜对。覆盖全部有行情的结论。
- **影子交易**：仅对看多/买入类按 T+1 收盘入场，跟踪止盈/止损/时间出场，算模拟盈亏。

执行规则（显式，可审计）
------------------------
- 入场：结论日（``ts``）之后**第一个有行情的交易日**按收盘价，固定 100 股名义仓位
- 出场（逐日检查，从入场次日开始）：收盘 ≤ stop → ``stop``；收盘 ≥ target → ``target``；
  持有交易日 ≥ ``max_hold_days``（默认 20）→ ``time``
- 写库失败/无行情 → ``skipped``，并记录 ``skip_reason``，绝不猜价格
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_INBOX = Path("E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/store/conclusions.jsonl")
LEDGER_FILE = Path("data") / "strategy_trials.json"

# stance → 期望方向：+1 涨 / -1 跌 / 0 中性
_EXPECTED = {
    "看多": 1, "买入": 1, "增持": 1,
    "拒绝": -1, "减仓": -1, "减仓/回避": -1, "离场": -1, "回避": -1,
    "观察": 0, "持有": 0,
}
_LONG_SET = {"看多", "买入", "增持"}
DEFAULT_MAX_HOLD = 20
SHARES = 100


def expected_direction(stance: Optional[str]) -> int:
    return _EXPECTED.get((stance or "").strip(), 0)


def load_proposals(path: Path) -> list[dict]:
    """读结论文件。坏行跳过；缺 code/ts 的跳过（无法定位到行情）。"""
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not row.get("code") or not row.get("ts"):
            continue
        out.append(row)
    out.sort(key=lambda r: str(r["ts"]))
    return out


def _proposal_id(row: dict) -> str:
    """稳定 id：优先 callId+code，其次 ts+code。重算时保证同一结论得到同一 id。"""
    call = row.get("callId") or ""
    return f"{row.get('ts','')}|{row.get('code','')}|{call}"



def _factor_at(series: dict, date: str):
    """取 date 当日或之前最近的复权因子；之前没有则取最早的（避免静默当 1.0）。

    复权因子是阶跃函数，除权除息日才变。adj_factors 的末日可能落后于行情
    （实测 2026-11-13 vs 行情 11-17），此时用「最近已知因子」是正确的，
    比拿 1.0 硬算更接近真值。
    """
    if not series:
        return None
    before = [d for d in series if d <= date]
    if before:
        return series[max(before)]
    after = [d for d in series if d > date]
    return series[min(after)] if after else None


def _adj_ratio(closes: dict, series: dict, base_date: str, later_date: str):
    """复权后的区间收益。因子缺失返回 None，由调用方回落到原始价。"""
    c0, c1 = closes.get(base_date), closes.get(later_date)
    f0, f1 = _factor_at(series, base_date), _factor_at(series, later_date)
    if not c0 or not c1 or not f0 or not f1:
        return None
    return (c1 * f1) / (c0 * f0) - 1.0


def track_one(row: dict, calendar: list, closes: dict, max_hold_days: int = DEFAULT_MAX_HOLD,
              adj: dict | None = None) -> dict:
    """跟踪一条结论。``calendar`` 为升序日期字符串列表，``closes`` 为 {date: close}。"""
    code = str(row["code"])
    ts = str(row["ts"])[:10]
    stance = (row.get("stance") or "").strip()
    rec = {
        "id": _proposal_id(row),
        "code": code,
        "stance": stance,
        "expected_direction": expected_direction(stance),
        "proposed_at": row.get("ts"),
        "proposed_date": ts,
        "source_call_id": row.get("callId"),
        "mode": row.get("mode"),
        "target": row.get("target"),
        "stop": row.get("stop"),
        "status": "skipped",
        "skip_reason": None,
        "entry_date": None,
        "entry_price": None,
        "exit_date": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl": None,
        "pnl_ratio": None,
        "ret5": None,
        "ret10": None,
        "basis": None,
        "hit5": None,
        "hit10": None,
    }
    if not closes:
        rec["skip_reason"] = "no_quote"
        return rec

    after = [d for d in calendar if d > ts and d in closes]
    if not after:
        rec["skip_reason"] = "no_forward_quote"
        return rec

    base_date = after[0]
    base_close = closes[base_date]

    # 方向命中：以「结论日后第一个交易日收盘」为基准，看第 5/10 个交易日
    for horizon, key_ret, key_hit in ((5, "ret5", "hit5"), (10, "ret10", "hit10")):
        if len(after) > horizon:
            later = closes.get(after[horizon])
            if later and base_close:
                # 优先复权口径（与「判断质量」页一致），因子缺失才回落原始价并标注
                adj_ret = _adj_ratio(closes, adj or {}, base_date, after[horizon])
                if adj_ret is not None:
                    ret = adj_ret
                    rec["basis"] = "adjusted"
                else:
                    ret = later / base_close - 1.0
                    rec["basis"] = rec["basis"] or "raw_fallback"
                rec[key_ret] = round(ret, 4)
                exp = rec["expected_direction"]
                if exp != 0:
                    rec[key_hit] = (ret > 0) == (exp > 0)

    # 影子交易：仅对买入类
    if stance not in _LONG_SET:
        rec["skip_reason"] = "not_long"
        rec["_base"] = {"date": base_date, "close": base_close}
        return rec

    target = row.get("target")
    stop = row.get("stop")
    rec["entry_date"] = base_date
    rec["entry_price"] = base_close
    rec["status"] = "open"
    horizon_days = after[1:1 + max_hold_days]
    for i, d in enumerate(horizon_days, start=1):
        px = closes.get(d)
        if not px:
            continue
        if stop is not None and px <= float(stop):
            rec.update(status="closed", exit_date=d, exit_price=px, exit_reason="stop")
            break
        if target is not None and px >= float(target):
            rec.update(status="closed", exit_date=d, exit_price=px, exit_reason="target")
            break
        if i >= max_hold_days:
            rec.update(status="closed", exit_date=d, exit_price=px, exit_reason="time")
            break
    if rec["status"] == "open" and horizon_days:
        last = horizon_days[-1]
        rec["mark_date"] = last
        rec["mark_price"] = closes.get(last)

    exit_date = rec["exit_date"] or rec.get("mark_date")
    exit_px = rec["exit_price"] if rec["exit_price"] is not None else rec.get("mark_price")
    if exit_px is not None and base_close:
        # 收益率走复权口径；元计盈亏也按复权价估算，并在 basis 里标明
        adj_ret = _adj_ratio(closes, adj or {}, base_date, exit_date) if exit_date else None
        if adj_ret is not None:
            rec["pnl_ratio"] = round(adj_ret, 4)
            rec["pnl"] = round(base_close * adj_ret * SHARES, 2)
            rec["basis"] = "adjusted"
        else:
            rec["pnl_ratio"] = round(exit_px / base_close - 1.0, 4)
            rec["pnl"] = round((exit_px - base_close) * SHARES, 2)
            rec["basis"] = rec["basis"] or "raw_fallback"
    return rec


def summarize(records: Iterable[dict]) -> dict:
    """分组汇总：方向命中率、影子交易盈亏。样本不足时如实标注。"""
    recs = list(records)
    by_expected: dict[int, list[dict]] = {1: [], -1: [], 0: []}
    for r in recs:
        by_expected.setdefault(r.get("expected_direction", 0), []).append(r)

    def _hit(rows: list[dict], horizon: int) -> dict:
        vals = [r.get(f"hit{horizon}") for r in rows if r.get(f"hit{horizon}") is not None]
        n = len(vals)
        return {"n": n, "hits": sum(1 for v in vals if v), "rate": (round(sum(1 for v in vals if v) / n, 4) if n else None)}

    return {
        "total": len(recs),
        "by_stance": {s: sum(1 for r in recs if (r.get("stance") or "") == s)
                      for s in sorted({(r.get("stance") or "") for r in recs})},
        "direction_hit_5d": {
            "expected_up": _hit(by_expected[1], 5),
            "expected_down": _hit(by_expected[-1], 5),
        },
        "direction_hit_10d": {
            "expected_up": _hit(by_expected[1], 10),
            "expected_down": _hit(by_expected[-1], 10),
        },
        "long_trials": {
            "n": sum(1 for r in recs if r.get("status") in ("open", "closed")),
            "open": sum(1 for r in recs if r.get("status") == "open"),
            "closed": sum(1 for r in recs if r.get("status") == "closed"),
            "pnl_total": round(sum(r["pnl"] for r in recs if r.get("pnl") is not None), 2),
            "wins": sum(1 for r in recs if (r.get("pnl_ratio") or 0) > 0),
        },
        "skipped": {reason: sum(1 for r in recs if r.get("skip_reason") == reason)
                    for reason in sorted({r.get("skip_reason") for r in recs if r.get("skip_reason")})},
    }


def build_trials_block(db_path: Path, inbox_path: Optional[Path] = None) -> Optional[dict]:
    """读结论 + 行情，产出契约块。库/文件缺失返回 None（调用方进 missing）。"""
    inbox = Path(inbox_path) if inbox_path else DEFAULT_INBOX
    proposals = load_proposals(inbox)
    if not proposals:
        return None
    try:
        import duckdb
    except ImportError:
        return None
    if not Path(db_path).exists():
        return None

    codes = sorted({str(p["code"]) for p in proposals})
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cal_df = con.execute(
            "SELECT DISTINCT trade_date FROM daily_quotes ORDER BY trade_date").fetchall()
        calendar = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in
                    (r[0] for r in cal_df)]
        q = con.execute(
            "SELECT ts_code, trade_date, close FROM daily_quotes WHERE ts_code IN ("
            + ",".join("?" * len(codes)) + ")", codes).fetchall()
        try:
            adj_rows = con.execute(
                "SELECT ts_code, trade_date, adj_factor FROM adj_factors WHERE ts_code IN ("
                + ",".join("?" * len(codes)) + ")", codes).fetchall()
        except Exception:  # noqa: BLE001 - 无复权表时整块回落原始价
            adj_rows = []
    finally:
        con.close()

    by_code: dict[str, dict[str, float]] = {}
    for code, day, close in q:
        d = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if close is None:
            continue
        by_code.setdefault(code, {})[d] = float(close)

    adj_by_code: dict[str, dict[str, float]] = {}
    for code, day, factor in adj_rows:
        d = day.isoformat() if hasattr(day, "isoformat") else str(day)
        if factor is None:
            continue
        adj_by_code.setdefault(code, {})[d] = float(factor)

    records = [track_one(p, calendar, by_code.get(str(p["code"]), {}),
                         adj=adj_by_code.get(str(p["code"]), {})) for p in proposals]
    # 内部字段不外泄
    for r in records:
        r.pop("_base", None)

    return {
        "generated_at": None,
        "source": str(inbox),
        "rules": {
            "entry": "结论日后第一个交易日的收盘价",
            "shares": SHARES,
            "max_hold_days": DEFAULT_MAX_HOLD,
            "exit": "收盘<=stop → stop；收盘>=target → target；持有>=max_hold → time",
            "long_stances": sorted(_LONG_SET),
            "return_basis": "优先复权（adj_factor），因子缺失回落原始价并在记录 basis 标注",
            "adj_covered_codes": len(adj_by_code),
            "adj_missing_codes": sorted({str(p["code"]) for p in proposals} - set(adj_by_code)),
        },
        "summary": summarize(records),
        "records": records,
    }

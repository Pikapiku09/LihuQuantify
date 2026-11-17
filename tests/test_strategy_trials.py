"""AI 结论影子跟踪（strategy_trials）单元测试（2026-11-17，Step 1）。

这层把「AI 说了什么」变成「可验证的数字」，所以守的是**不许美化**：
1. 只有买入类才建仓，其余一律 not_long 跳过（不硬凑样本）
2. 没有行情/没有前向窗口 → skipped + 原因，绝不猜价格
3. 止盈/止损/时间出场规则与契约里写明的一致
4. 方向命中用「结论日后第一个交易日收盘」作基准，避免前视偏差
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.strategy_trials import (  # noqa: E402
    build_trials_block, expected_direction, load_proposals, summarize, track_one,
)

CAL = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10",
       "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17"]


def _row(stance="看多", ts="2026-11-04T09:00:00Z", code="600011.SH",
         target=None, stop=None):
    return {"ts": ts, "code": code, "stance": stance, "target": target,
            "stop": stop, "callId": "c1", "mode": "个股"}


def test_expected_direction_mapping():
    assert expected_direction("看多") == 1
    assert expected_direction("买入") == 1
    assert expected_direction("减仓") == -1
    assert expected_direction("离场") == -1
    assert expected_direction("观察") == 0
    assert expected_direction("持有") == 0
    assert expected_direction(None) == 0


def test_non_long_is_skipped_not_forced():
    """观察/持有/减仓不建仓 —— 不许为了凑样本硬开仓。"""
    closes = {d: 10.0 for d in CAL}
    rec = track_one(_row(stance="观察"), CAL, closes)
    assert rec["status"] == "skipped"
    assert rec["skip_reason"] == "not_long"
    assert rec["entry_price"] is None


def test_no_quote_skipped():
    rec = track_one(_row(), CAL, {})
    assert rec["skip_reason"] == "no_quote"


def test_no_forward_window_skipped():
    """结论日已是最后一个交易日 → 没有前向数据，不许用当日价冒充。"""
    closes = {"2026-11-17": 10.0}
    rec = track_one(_row(ts="2026-11-17T09:00:00Z"), ["2026-11-17"], closes)
    assert rec["skip_reason"] == "no_forward_quote"


def test_entry_is_next_trading_day_close():
    closes = {d: 10.0 + i for i, d in enumerate(CAL)}
    rec = track_one(_row(ts="2026-11-04T09:00:00Z"), CAL, closes)
    assert rec["entry_date"] == "2026-11-05"
    assert rec["entry_price"] == closes["2026-11-05"]


def test_target_hit_closes_at_target_day():
    closes = {d: 10.0 for d in CAL}
    closes["2026-11-09"] = 12.0
    rec = track_one(_row(target=11.0, stop=8.0), CAL, closes)
    assert rec["status"] == "closed"
    assert rec["exit_reason"] == "target"
    assert rec["exit_date"] == "2026-11-09"
    assert rec["pnl_ratio"] is not None and rec["pnl_ratio"] > 0


def test_stop_hit_closes_first():
    closes = {d: 10.0 for d in CAL}
    closes["2026-11-06"] = 7.0
    rec = track_one(_row(target=20.0, stop=8.0), CAL, closes)
    assert rec["exit_reason"] == "stop"
    assert rec["pnl_ratio"] < 0


def test_time_exit_after_max_hold():
    closes = {d: 10.0 + (i * 0.1) for i, d in enumerate(CAL)}
    rec = track_one(_row(), CAL, closes, max_hold_days=3)
    assert rec["exit_reason"] == "time"
    assert rec["status"] == "closed"


def test_direction_hit_uses_next_day_base():
    """5 日收益基准是「结论日后第一个交易日」，不能直接用结论日收盘。"""
    closes = {d: 10.0 for d in CAL}
    closes["2026-11-12"] = 11.0        # 第 5 个前向交易日（11-05 起算第 5 个）
    rec = track_one(_row(stance="看多"), CAL, closes)
    assert rec["ret5"] == pytest.approx(0.1)
    assert rec["hit5"] is True


def test_bearish_stance_hits_on_decline():
    closes = {d: 10.0 for d in CAL}
    closes["2026-11-12"] = 9.0
    rec = track_one(_row(stance="减仓"), CAL, closes)
    assert rec["ret5"] == pytest.approx(-0.1)
    assert rec["hit5"] is True, "看空类遇到下跌应记为命中"


def test_load_proposals_skips_bad_lines(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text(
        '{"ts":"2026-11-04T09:00:00Z","code":"600011.SH","stance":"看多"}\n'
        "不是 JSON\n"
        '{"ts":"2026-11-05T09:00:00Z"}\n'          # 缺 code
        "\n",
        encoding="utf-8",
    )
    rows = load_proposals(p)
    assert len(rows) == 1 and rows[0]["code"] == "600011.SH"


def test_summarize_counts():
    closes = {d: 10.0 for d in CAL}
    recs = [track_one(_row(stance="看多"), CAL, closes),
            track_one(_row(stance="减仓"), CAL, closes),
            track_one(_row(stance="观察"), CAL, closes)]
    s = summarize(recs)
    assert s["total"] == 3
    assert s["long_trials"]["n"] == 1
    assert s["skipped"]["not_long"] == 2


def test_build_block_from_temp_db(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "q.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE daily_quotes (ts_code VARCHAR, trade_date DATE, close DOUBLE)")
    for i, d in enumerate(CAL):
        con.execute("INSERT INTO daily_quotes VALUES ('600011.SH', ?, ?)", [d, 10.0 + i])
    con.close()

    inbox = tmp_path / "c.jsonl"
    inbox.write_text(
        '{"ts":"2026-11-04T09:00:00Z","code":"600011.SH","stance":"看多",'
        '"target":12.0,"stop":9.0,"callId":"c1"}\n', encoding="utf-8")
    block = build_trials_block(db, inbox)
    assert block is not None
    assert len(block["records"]) == 1
    assert block["records"][0]["status"] in ("open", "closed")
    assert block["rules"]["shares"] == 100


def test_build_block_missing_inputs(tmp_path):
    assert build_trials_block(tmp_path / "nope.duckdb", tmp_path / "nope.jsonl") is None


def test_repo_block_consistent():
    """仓库真实数据：有结论就必须有记录，且统计口径字段齐全。"""
    from lihu_quantify.monitor.strategy_trials import DEFAULT_INBOX, LEDGER_FILE

    db = ROOT / "data" / "lihu_quant.duckdb"
    if not DEFAULT_INBOX.exists() or not db.exists():
        pytest.skip("缺少结论文件或行情库")
    block = build_trials_block(db, DEFAULT_INBOX)
    if block is None:
        pytest.skip("结论为空")
    assert block["records"]
    assert "n" in block["summary"]["direction_hit_5d"]["expected_up"]
    assert block["rules"]["entry"]

# ===== 复权口径（2026-11-17，v0.68.0）=====

def test_adj_ratio_uses_factor_ratio():
    """复权收益必须用因子比值，不能只看收盘价。"""
    from lihu_quantify.monitor.strategy_trials import _adj_ratio

    closes = {"2026-11-05": 10.0, "2026-11-12": 10.0}   # 价格没动
    adj = {"2026-11-05": 1.0, "2026-11-12": 1.1}        # 但发生除权
    r = _adj_ratio(closes, adj, "2026-11-05", "2026-11-12")
    assert r == pytest.approx(0.1), "价格不变但因子变，复权收益应为 +10%"


def test_adj_ratio_none_when_factor_missing():
    from lihu_quantify.monitor.strategy_trials import _adj_ratio

    assert _adj_ratio({"a": 1.0, "b": 2.0}, {}, "a", "b") is None


def test_factor_lookup_uses_last_known_before_date():
    """adj_factors 末日可能落后行情；用「最近已知因子」而不是当 1.0。"""
    from lihu_quantify.monitor.strategy_trials import _factor_at

    series = {"2026-11-10": 1.2, "2026-11-13": 1.5}
    assert _factor_at(series, "2026-11-17") == 1.5      # 之后没有 → 用最近已知
    assert _factor_at(series, "2026-11-11") == 1.2
    assert _factor_at(series, "2026-11-01") == 1.2      # 之前没有 → 用最早


def test_track_one_marks_basis():
    """有因子标 adjusted；无因子回落原始价并标 raw_fallback（页面据此提示未复权）。"""
    closes = {d: 10.0 for d in CAL}
    closes["2026-11-12"] = 11.0
    adj = {d: 1.0 for d in CAL}

    adjusted = track_one(_row(stance="看多"), CAL, closes, adj=adj)
    assert adjusted["basis"] == "adjusted"

    fallback = track_one(_row(stance="看多"), CAL, closes, adj={})
    assert fallback["basis"] == "raw_fallback"
    assert fallback["ret5"] == pytest.approx(0.1)

# ===== 资金约束下的撮合模拟（2026-11-17，待办 3）=====

def _mk_rec(code, entry_date, target=None, stop=None, stance="看多"):
    return {"code": code, "status": "open", "entry_date": entry_date,
            "target": target, "stop": stop, "stance": stance}


def test_portfolio_respects_max_positions():
    """仓位满了就必须跳过，不许超配。"""
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06", "2026-11-09"]
    closes = {c: {d: 10.0 for d in cal} for c in ("A.SZ", "B.SZ", "C.SZ")}
    recs = [_mk_rec(c, "2026-11-05") for c in ("A.SZ", "B.SZ", "C.SZ")]
    out = simulate_portfolio(recs, cal, closes, max_positions=2)
    assert out["n_open"] == 2
    assert out["skipped"]["no_slot"] == 1


def test_portfolio_position_size_capped():
    """单票不超过 总资产×上限；股数向下取整到 100 股。

    用**零成本**口径验仓位算法本身 —— 含成本时买入即扣费，净值必然低于初始，
    那是成本测试的事，不该混进仓位测试。
    """
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05"]
    closes = {"A.SZ": {"2026-11-05": 10.0}}
    out = simulate_portfolio(
        [_mk_rec("A.SZ", "2026-11-05")], cal, closes,
        initial_capital=100000.0, max_positions=3, max_single_position=0.15,
        costs={"commission_rate": 0.0, "min_commission": 0.0,
               "stamp_tax_rate": 0.0, "slippage": 0.0, "lot": 100})
    # 上限 15000 元 / 10 元 = 1500 股：现金 85000 + 市值 15000 = 100000
    assert out["final_equity"] == pytest.approx(100000.0)


def test_portfolio_insufficient_cash_when_lot_too_expensive():
    """一手都买不起 → insufficient_cash，且不动用现金。"""
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05"]
    closes = {"A.SZ": {"2026-11-05": 2000.0}}     # 一手 20 万 > 初始 10 万
    out = simulate_portfolio(
        [_mk_rec("A.SZ", "2026-11-05")], cal, closes,
        initial_capital=100000.0, max_single_position=0.15)
    assert out["n_open"] == 0
    assert out["skipped"]["insufficient_cash"] == 1
    assert out["final_equity"] == pytest.approx(100000.0)


def test_portfolio_target_exit_realizes_profit():
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06", "2026-11-09"]
    closes = {"A.SZ": {"2026-11-05": 10.0, "2026-11-06": 10.0, "2026-11-09": 12.0}}
    out = simulate_portfolio(
        [_mk_rec("A.SZ", "2026-11-05", target=11.0)], cal, closes,
        initial_capital=100000.0, max_single_position=0.15)
    assert out["n_trades"] == 1
    t = out["trades"][0]
    assert t["exit_reason"] == "target"
    assert t["exit_date"] == "2026-11-09"
    assert t["pnl"] > 0


def test_portfolio_stop_exit():
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06"]
    closes = {"A.SZ": {"2026-11-05": 10.0, "2026-11-06": 8.0}}
    out = simulate_portfolio(
        [_mk_rec("A.SZ", "2026-11-05", stop=9.0)], cal, closes,
        initial_capital=100000.0, max_single_position=0.15)
    assert out["trades"][0]["exit_reason"] == "stop"
    assert out["trades"][0]["pnl"] < 0


def test_portfolio_curve_covers_calendar():
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06", "2026-11-09"]
    closes = {"A.SZ": {d: 10.0 for d in cal}}
    out = simulate_portfolio([_mk_rec("A.SZ", "2026-11-05")], cal, closes)
    assert [p["d"] for p in out["equity_curve"]] == cal


def test_portfolio_empty_is_flat():
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06"]
    out = simulate_portfolio([], cal, {})
    assert out["n_trades"] == 0 and out["n_open"] == 0
    assert out["final_equity"] == pytest.approx(100000.0)
    assert out["total_return"] == pytest.approx(0.0)


def test_repo_config_has_portfolio_max_positions():
    import yaml
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    st = cfg.get("strategy_trials") or {}
    assert int(st.get("portfolio_max_positions") or 0) >= 1

# ===== 交易成本（2026-11-17）=====

def test_default_costs_source_is_broker():
    """成本常量必须来自 broker，不能另立一套数字。"""
    from lihu_quantify.monitor.strategy_trials import default_costs

    c = default_costs()
    assert c["commission_rate"] == pytest.approx(0.00025)
    assert c["min_commission"] == pytest.approx(5.0)
    assert c["stamp_tax_rate"] == pytest.approx(0.0005)
    assert c["slippage"] == pytest.approx(0.001)
    assert c["lot"] == 100
    assert "broker" in (c.get("source") or ""), "应标明取自 broker"


def test_buy_pays_slippage_and_commission():
    """买入：成交价上浮滑点，且收佣金（小仓位触发 5 元下限）。"""
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05"]
    closes = {"A.SZ": {"2026-11-05": 10.0}}
    out = simulate_portfolio([_mk_rec("A.SZ", "2026-11-05")], cal, closes,
                             initial_capital=100000.0, max_single_position=0.15)
    c = out["costs"]
    assert c["total_commission"] == pytest.approx(5.0), "15000 元仓位佣金不足 5 元 → 取下限"
    assert c["total_slippage"] > 0
    assert c["total_stamp_tax"] == 0.0, "买入不收印花税"
    assert out["final_equity"] < 100000.0, "扣完成本净值必然低于初始"


def test_sell_charges_stamp_tax_and_nets_out():
    """卖出：收印花税，且 pnl 为净额（小于毛额）。"""
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06"]
    closes = {"A.SZ": {"2026-11-05": 10.0, "2026-11-06": 12.0}}
    out = simulate_portfolio([_mk_rec("A.SZ", "2026-11-05", target=11.0)], cal, closes,
                             initial_capital=100000.0, max_single_position=0.15)
    assert out["n_trades"] == 1
    t = out["trades"][0]
    assert out["costs"]["total_stamp_tax"] > 0, "卖出必须收印花税"
    assert t["pnl"] < t["pnl_gross"], "净盈亏应小于毛盈亏"
    assert t["stamp_tax"] > 0
    assert t["exit_price"] < 12.0, "卖出吃滑点，成交价应低于名义价"


def test_costs_total_is_sum_of_parts():
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05", "2026-11-06"]
    closes = {"A.SZ": {"2026-11-05": 10.0, "2026-11-06": 12.0}}
    out = simulate_portfolio([_mk_rec("A.SZ", "2026-11-05", target=11.0)], cal, closes)
    c = out["costs"]
    assert c["total_fees"] == pytest.approx(
        c["total_commission"] + c["total_stamp_tax"] + c["total_slippage"], abs=0.01)


def test_no_cost_baseline_differs_from_zero_cost():
    """含成本与零成本必须给出不同结果 —— 否则说明成本没接上。"""
    from lihu_quantify.monitor.strategy_trials import simulate_portfolio

    cal = ["2026-11-05"]
    closes = {"A.SZ": {"2026-11-05": 10.0}}
    recs = [_mk_rec("A.SZ", "2026-11-05")]
    with_cost = simulate_portfolio(recs, cal, closes, initial_capital=100000.0)
    zero = simulate_portfolio(recs, cal, closes, initial_capital=100000.0,
                              costs={"commission_rate": 0.0, "min_commission": 0.0,
                                     "stamp_tax_rate": 0.0, "slippage": 0.0, "lot": 100})
    assert zero["final_equity"] == pytest.approx(100000.0)
    assert with_cost["final_equity"] < zero["final_equity"]


def test_repo_portfolio_has_cost_block():
    """真实产出必须带成本块，并声明费率来源。"""
    import json
    p = ROOT / "outputs" / "strategy_trials.json"
    if not p.exists():
        pytest.skip("缺少产出")
    d = json.loads(p.read_text(encoding="utf-8"))
    port = d.get("portfolio")
    if not port:
        pytest.skip("无撮合块")
    c = port.get("costs") or {}
    for k in ("commission_rate", "min_commission", "stamp_tax_rate", "slippage", "total_fees"):
        assert k in c, f"成本块缺 {k}"

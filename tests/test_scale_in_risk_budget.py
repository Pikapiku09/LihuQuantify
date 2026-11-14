"""第2层回归：nogate_half 半仓影子 + 连续风险预算 + 组合层 MDD 25% 硬守卫。

演化史（2026-11-14）：
    本文件原覆盖「连续风险预算 + 分批建仓 + MDD 守卫」三件事。
    分批建仓（scale_in）已于当日**永久删除**——与 docs/交易铁律.md 第4条
    「绝不向下补仓」冲突，且真实交割单实证 27.4% 的买入额发生在浮亏加仓。
    文件名保留（git 历史连续），内容已不再包含该功能。

    **行为变更**：MDD 守卫原先只在 scale_in.enabled 时调用，而该开关从未开启
    → 守卫实际从未运行。现改为每个交易日无条件评估，阈值读 risk.mdd_limit（25%），
    结果写 outputs/mdd_guard.json。因此 summary 必然出现 mdd_guard 键（有意变更）。

仍然保留的铁律落点：
    1. 闸门默认语义 → 预算关闭时 _gate_scale 逐组合对照旧实现（block/reduce/off）；
    2. 绝不自动平仓 → MDD 守卫路径无 sell 调用（结构断言 + 端到端卖出数=0）；
    3. market_filter_mode=block 语义不变 → 预算关闭时 block=0.0 且不买入；
    4. 影子账本隔离 → 各影子独立 state 文件，不混入主日报。
"""

from __future__ import annotations

import inspect
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
import yaml

from lihu_quantify.config import RiskConfig, ShadowBookConfig, StrategyConfig
from lihu_quantify.execution.oms import OrderManagementSystem
from lihu_quantify.execution.paper_trade import PaperBroker
from lihu_quantify.monitor import scheduler as sched_mod
from lihu_quantify.monitor.alerts import Alerter
from lihu_quantify.monitor.scheduler import (
    MDD_GUARD_POLICY,
    BookSpec,
    DailyScanner,
    _drawdown_metrics,
    _gate_scale,
    _load_risk_budget,
    _merge_equity_point,
    _risk_budget_coef,
)
from lihu_quantify.types import Signal

ROOT = Path(__file__).resolve().parents[1]
LATEST = date(2026, 8, 28)

# 连续风险预算（默认映射，与 settings.yaml 一致）
BUDGET_ON = {"enabled": True, "uptrend": 1.0, "rangebound": 0.7,
             "downtrend": 0.4, "unknown": 0.0}
BUDGET_OFF = dict(BUDGET_ON, enabled=False)

# 分批建仓（默认参数，与 settings.yaml 一致）
SCALE_IN_ON = {"enabled": True, "max_tranches": 2, "tranche_gap_pct": -5.0,
               "tranche_size_ratio": 0.5, "mdd_limit": 0.25}
SCALE_IN_OFF = dict(SCALE_IN_ON, enabled=False)


class _StratCfg:
    """最小策略配置替身（默认 block 模式）。"""

    def __init__(self, mode: str = "block", market_filter: bool = True):
        self.market_filter = market_filter
        self.market_filter_mode = mode


def _legacy_gate_scale(strategy_cfg, market_state: str, sentiment_ebb: bool):
    """改造前 `_gate_scale` 的逐字实现（只到 gate=follow 为止）——对照基准。"""
    block_mode = bool(
        getattr(strategy_cfg, "market_filter", False)
        and market_state != "上涨"
        and getattr(strategy_cfg, "market_filter_mode", "") == "block"
    )
    reduce_scale = 0.5 if (
        getattr(strategy_cfg, "market_filter", False)
        and market_state != "上涨"
        and getattr(strategy_cfg, "market_filter_mode", "") == "reduce"
    ) else 1.0
    if sentiment_ebb:
        block_mode = True
        reduce_scale = 0.0
    return block_mode, (0.0 if block_mode else reduce_scale)


# ============================================================
# 0. 测试基建（stub scanner + 全接线，参照 test_round8_capital_heatmap）
# ============================================================

def _settings(tmp_path: Path) -> "object":
    from lihu_quantify.config import Settings

    (tmp_path / "data").mkdir(exist_ok=True)
    return Settings(
        duckdb_path=str(tmp_path / "t.duckdb"),
        cache_dir=str(tmp_path / "cache"),
        tushare_token="fake-token-for-init-only",
    )


def _make_scanner(tmp_path, monkeypatch, *, init: float = 1_000_000.0,
                  book=None, real_gate: bool = True):
    """stub scanner（绕过 __init__ 的真实 Tushare/DuckDB），返回 (scanner, broker, oms_list)。"""
    broker = PaperBroker(init_capital=init, persist=False,
                         state_file=str(tmp_path / "state.json"))
    s = DailyScanner.__new__(DailyScanner)
    if book is not None:
        s.book = book
    s.broker = broker
    s.alerter = Alerter()
    s.reporter = MagicMock()
    s.reporter.daily_report.return_value = tmp_path / "report.md"
    s.mode = "paper"
    s.settings = MagicMock()

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(sched_mod, "_append_filter_stats", lambda rec: None)

    made_oms: list = []

    def _oms(b=None, registry_file=None):
        inst = OrderManagementSystem(
            b, registry_file=registry_file or str(tmp_path / "stop.json"),
            persist=True)
        made_oms.append(inst)
        return inst

    monkeypatch.setattr(sched_mod, "OrderManagementSystem", _oms)
    if not real_gate:   # 只看成交结果时绕过 8 项闸门（避免 take_profit 等噪声）
        gate = MagicMock()
        gate.check.return_value = MagicMock(approved=True, rejected_items=lambda: [])
        monkeypatch.setattr(sched_mod, "ChecklistGate", lambda **kw: gate)
    return s, broker, made_oms


def _wire_scan(monkeypatch, s, signals_by_code: dict, closes: dict,
               opens: dict | None = None, ma10s: dict | None = None,
               sectors: dict | None = None):
    """接线：client/策略/指标/池 全 stub；closes 同时注入 broker 现价。"""
    opens = opens or {}
    ma10s = ma10s or {}
    sectors = sectors or {}

    def fake_query(api, params=None, use_cache=True):
        code = (params or {}).get("ts_code", "600000.SH")
        dates = pd.date_range(end="20260828", periods=40, freq="D")
        if api == "adj_factor":
            return pd.DataFrame({"ts_code": code,
                                 "trade_date": [d.strftime("%Y%m%d") for d in dates],
                                 "adj_factor": 1.0})
        close = closes.get(code, 10.0)
        return pd.DataFrame({
            "ts_code": code,
            "trade_date": [d.strftime("%Y%m%d") for d in dates],
            "open": opens.get(code, close), "high": close * 1.02,
            "low": close * 0.98, "close": close,
            "vol": 1000.0, "amount": 100000.0,
        })

    s.client = MagicMock()
    s.client.query.side_effect = fake_query

    strategy = MagicMock()
    strategy.latest_signal.side_effect = lambda df: signals_by_code.get(
        str(df["ts_code"].iloc[-1]))
    monkeypatch.setattr(sched_mod, "CherryClaw", lambda **kw: strategy)

    def add_std(df):
        code = str(df["ts_code"].iloc[-1])
        close = closes.get(code, 10.0)
        return df.assign(ma10=ma10s.get(code, close * 0.95),
                         vol_ratio=1.0, body_ratio=0.5, ma5_x_ma10=1)

    monkeypatch.setattr(sched_mod, "add_all_standard", add_std)

    codes = list(signals_by_code.keys())
    monkeypatch.setattr(
        s, "_universe",
        lambda n: (codes, {c: sectors.get(c, "") for c in codes},
                   {c: f"股{c[:6]}" for c in codes}))
    for code, close in closes.items():
        s.broker.set_price(code, close)


def _run_scan(s, *, market_state: str = "上涨", market_filter: bool = False,
              mode: str = "block", budget=None,
              latest: date = LATEST):
    return s._scan_impl(
        latest, market_state, n=50, days=120,
        s=StrategyConfig(market_filter=market_filter,
                         market_filter_mode=mode),
        r=RiskConfig(), prev_total_asset=None,
        risk_budget=budget,
    )


def _sig(code: str, price: float, pct: float = 0.10) -> Signal:
    return Signal(kind="buy", ts_code=code, suggested_price=price,
                  stop_loss=round(price * 0.9, 4),
                  take_profit=[round(price * 1.1, 4)],
                  suggested_position_pct=pct, reason="测试信号")


def _buys(broker) -> list[dict]:
    return [t for t in broker.trades if t["side"] == "buy"]


def _sells(broker) -> list[dict]:
    return [t for t in broker.trades if t["side"] == "sell"]


# ============================================================
# 1. 默认关闭 → 线上零行为变化（核心证据）
# ============================================================

def test_repo_config_all_new_switches_off_and_block_unchanged():
    """线上 settings.yaml：新开关全 false；market_filter_mode=block 语义不动。"""
    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert raw["gate_risk_budget"]["enabled"] is False
    # 分批建仓已于 2026-11-14 永久删除：配置块不得复活
    assert "scale_in" not in raw, "scale_in 配置块应已删除，切勿恢复"
    # MDD 守卫阈值已从 scale_in 迁入 risk 段（守卫本身改为无条件评估）
    assert raw["risk"]["mdd_limit"] == 0.25
    assert raw["strategy"]["market_filter_mode"] == "block"   # 既定决策，不得改动
    # 影子账本：nogate（恒 1.0）与 nogate_half（恒 0.5）同时存在且语义区分
    ledgers = {x["name"]: x for x in raw["shadow_ledgers"]}
    assert ledgers["nogate"]["market_gate"] == "off"
    assert ledgers["nogate_half"]["market_gate"] == "half"
    books = [x["name"] for x in raw["shadow_books"]]
    assert "nogate_half" in books and "nogate" in books


@pytest.mark.parametrize("mode", ["block", "reduce", "off"])
@pytest.mark.parametrize("state", ["上涨", "下跌", "震荡", "未知"])
@pytest.mark.parametrize("sentiment", [False, True])
@pytest.mark.parametrize("market_filter", [True, False])
def test_gate_scale_identical_to_legacy_when_budget_disabled(
        mode, state, sentiment, market_filter):
    """diff 为空：预算关闭时 _gate_scale 与改造前逐组合完全一致（48 组 × 2 种入参）。

    risk_budget=None → 走真实 settings.yaml（enabled=false）；
    risk_budget=BUDGET_OFF → 显式关闭。两条路径都必须等于旧实现。
    """
    cfg = _StratCfg(mode=mode, market_filter=market_filter)
    expected = _legacy_gate_scale(cfg, state, sentiment)
    assert _gate_scale(BookSpec(), cfg, state, sentiment,
                       risk_budget=None) == expected
    assert _gate_scale(BookSpec(), cfg, state, sentiment,
                       risk_budget=BUDGET_OFF) == expected


def test_default_off_scan_writes_no_new_files_and_no_new_summary_keys(
        tmp_path, monkeypatch):
    """默认关闭全链路：成交口径不变。

    2026-11-14 行为变更：MDD 守卫改为每个交易日无条件评估（原先只在
    scale_in.enabled 时调用 → 实际从未运行），因此 summary 必然多出
    mdd_guard 键、并落盘 equity_curve/mdd_guard。除此之外不得有其它新键。
    """
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})

    summary = _run_scan(s)   # 市场过滤关闭 → 旧三档 entry_scale=1.0

    assert summary["entry_scale"] == 1.0
    # 连续风险预算默认关闭 → 不得有 risk_budget 键
    assert "risk_budget" not in summary
    # 分批建仓已删除 → 永远不得出现 scale_in 键
    assert "scale_in" not in summary
    # MDD 守卫无条件评估 → 必须有键（这是有意变更，不是回归）
    assert summary["mdd_guard"]["triggered"] is False
    # 旧公式逐位一致：invest = 0.10 × 总资产 × 1.0 → 100000/10 = 10000 股
    buys = _buys(broker)
    assert len(buys) == 1 and buys[0]["volume"] == 10000
    assert buys[0]["price"] == 10.0
    # MDD 守卫自 2026-11-14 起无条件落盘（有意变更）；scale_in 状态文件已彻底不存在
    assert (tmp_path / "outputs" / "mdd_guard.json").exists()
    assert (tmp_path / "data" / "equity_curve.json").exists()
    assert not (tmp_path / "data" / "scale_in_state.json").exists()
    # 绝不自动平仓：本轮无卖出
    assert _sells(broker) == []


def test_block_mode_semantics_unchanged_when_budget_off(tmp_path, monkeypatch):
    """market_filter_mode=block：非上涨段仍然 0 仓位 + 不买入（既定决策语义）。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    summary = _run_scan(s, market_state="下跌", market_filter=True, mode="block")
    assert summary["entry_scale"] == 0.0
    assert _buys(broker) == []
    # 预算关闭时即便传入 BUDGET_OFF 也不能改变 block 语义
    s2, broker2, _ = _make_scanner(tmp_path, monkeypatch)
    _wire_scan(monkeypatch, s2, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    summary2 = _run_scan(s2, market_state="下跌", market_filter=True,
                         mode="block", budget=BUDGET_OFF)
    assert summary2["entry_scale"] == 0.0 and _buys(broker2) == []


def test_disabled_config_blocks_are_inert(tmp_path, monkeypatch):
    """同一输入的 diff 为空：写入"新配置块（全 false）"与完全不写，巡检结果逐项一致。

    证据口径：summary 的实质字段（闸门系数/信号数/成交/拦截/资产/资金效率/
    市场闸门统计）完全相等，且 summary 键集合完全相同（无新增键）。
    """
    def _run(base: Path, with_blocks: bool):
        base.mkdir(parents=True, exist_ok=True)
        if with_blocks:
            (base / "config").mkdir(exist_ok=True)
            (base / "config" / "settings.yaml").write_text(
                "gate_risk_budget:\n"
                "  enabled: false\n"
                "  uptrend: 1.0\n  rangebound: 0.7\n  downtrend: 0.4\n  unknown: 0.0\n"
                "scale_in:\n"
                "  enabled: false\n"
                "  max_tranches: 2\n  tranche_gap_pct: -5\n"
                "  tranche_size_ratio: 0.5\n  mdd_limit: 0.25\n",
                encoding="utf-8")
        s, broker, _ = _make_scanner(base, monkeypatch)
        _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
                   {"600000.SH": 10.0})
        summary = _run_scan(s, market_state="下跌", market_filter=True,
                            mode="block")
        keep = ("entry_scale", "signals", "executed", "rejected", "total_asset",
                "cash", "capital", "market_gate", "stale_dropped", "positions")
        return ({k: summary[k] for k in keep},
                list(broker.trades), set(summary.keys()))

    no_blocks = _run(tmp_path / "a", with_blocks=False)
    with_blocks = _run(tmp_path / "b", with_blocks=True)
    assert no_blocks == with_blocks, "新配置块全 false 时不得产生任何行为差异"


# ============================================================
# 2. 任务一：nogate_half 半仓影子
# ============================================================

@pytest.mark.parametrize("state", ["上涨", "下跌", "震荡", "未知"])
@pytest.mark.parametrize("sentiment", [False, True])
def test_half_gate_always_half(state, sentiment):
    """gate=half：任何市场状态/情绪退潮下 entry_scale 恒 0.5 且不禁止开仓。"""
    cfg = _StratCfg(mode="block")
    assert _gate_scale(BookSpec(name="nogate_half", gate="half"),
                       cfg, state, sentiment) == (False, 0.5)
    # 与 nogate（恒 1.0）严格区分
    assert _gate_scale(BookSpec(name="nogate", gate="off"),
                       cfg, state, sentiment) == (False, 1.0)
    # 主账本（follow）仍按 settings 三档
    assert _gate_scale(BookSpec(), cfg, state, sentiment) == \
        _legacy_gate_scale(cfg, state, sentiment)


def test_three_shadow_books_isolated_state_files(tmp_path, monkeypatch):
    """main / nogate / nogate_half 三本账本资金与持仓互不影响、状态文件独立。"""
    from lihu_quantify.execution import paper_trade
    from lihu_quantify.monitor import scheduler as sm

    monkeypatch.setattr(sm, "_ROOT", tmp_path)
    monkeypatch.setattr(paper_trade, "_ROOT", tmp_path)
    st = _settings(tmp_path)
    main = DailyScanner(st, mode="paper")
    nogate = DailyScanner(
        st.model_copy(deep=True), mode="paper",
        book=BookSpec(name="nogate", pool_seed=42, silent=True,
                      strategy="intraday_reversal", gate="off"))
    half = DailyScanner(
        st.model_copy(deep=True), mode="paper",
        book=BookSpec(name="nogate_half", pool_seed=42, silent=True,
                      strategy="intraday_reversal", gate="half"))

    assert main.broker.state_file == str(tmp_path / "data" / "paper_state.json")
    assert nogate.broker.state_file == str(tmp_path / "data" / "paper_state_nogate.json")
    assert half.broker.state_file == \
        str(tmp_path / "data" / "paper_state_nogate_half.json")

    main_asset_before = main.broker.query_asset()
    main.broker.buy("600000.SH", 10.0, 100)
    nogate.broker.buy("600001.SH", 11.0, 100)
    half.broker.buy("600002.SH", 12.0, 200)

    # 资金/持仓互不影响（直接用 positions dict，避免触发真实行情查询）
    assert list(main.broker.positions) == ["600000.SH"]
    assert list(nogate.broker.positions) == ["600001.SH"]
    assert list(half.broker.positions) == ["600002.SH"]
    assert half.broker.positions["600002.SH"].volume == 200
    assert len({main.broker.cash, nogate.broker.cash, half.broker.cash}) == 3, \
        "三本账本现金互不影响"

    def _state(p):
        return json.loads(p.read_text(encoding="utf-8"))

    st_main = _state(tmp_path / "data" / "paper_state.json")
    st_ng = _state(tmp_path / "data" / "paper_state_nogate.json")
    st_half = _state(tmp_path / "data" / "paper_state_nogate_half.json")
    assert set(st_main["positions"]) == {"600000.SH"}
    assert set(st_ng["positions"]) == {"600001.SH"}
    assert set(st_half["positions"]) == {"600002.SH"}
    # 影子 silent：不发日报、邮箱通道不构建
    assert half.book.silent is True and half.alerter.enabled is False
    assert nogate.alerter.enabled is False


def test_half_ledger_never_enters_main_digest(tmp_path, monkeypatch):
    """半仓影子 silent：不进主账本日报的"无闸门影子"一行（只有 gate=off 才进）。"""
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "shadow_ledger_stats.json").write_text(json.dumps({
        "trade_date": str(LATEST),
        "ledgers": {"nogate_half": {"signals": 9, "executed": 3,
                                    "entry_scale": 0.5, "gate": "half"}},
    }, ensure_ascii=False), encoding="utf-8")
    s = DailyScanner.__new__(DailyScanner)
    assert s._shadow_ledger_digest_line(LATEST) == ""


@pytest.mark.parametrize("market_gate,expected", [("half", "half"), ("reduce", "half")])
def test_setup_scheduler_builds_nogate_half(tmp_path, monkeypatch, market_gate, expected):
    """setup_scheduler：shadow_ledgers.market_gate=half（别名 reduce）→ BookSpec.gate=half。"""
    import apscheduler.schedulers.blocking as blk

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        "shadow_ledgers:\n"
        "  - name: \"nogate_half\"\n"
        "    enabled: true\n"
        "    signal_source: \"main\"\n"
        f"    market_gate: \"{market_gate}\"\n",
        encoding="utf-8",
    )
    st = _settings(tmp_path)
    st.shadow_books = [ShadowBookConfig(name="nogate_half", seed=42,
                                        strategy="cherry_claw")]
    st.main_strategy = "intraday_reversal"
    st.universe.pool_seed = 42

    made: list = []

    class _FakeScanner:
        def __init__(self, settings, mode="paper", book=None):
            self.settings = settings
            self.book = book or BookSpec()
            made.append(self)

    class _FakeSched:
        def add_job(self, *a, **kw):
            pass

        def start(self):
            pass

    orig_cls, orig_blk = sched_mod.DailyScanner, blk.BlockingScheduler
    sched_mod.DailyScanner = _FakeScanner
    blk.BlockingScheduler = lambda *a, **kw: _FakeSched()
    try:
        sched_mod.setup_scheduler(st, mode="paper", n=5)
    finally:
        sched_mod.DailyScanner = orig_cls
        blk.BlockingScheduler = orig_blk

    by_name = {sc.book.name: sc for sc in made}
    assert set(by_name) == {"main", "nogate_half"}
    half = by_name["nogate_half"]
    assert half.book.gate == expected
    assert half.book.silent is True
    assert half.book.strategy == "intraday_reversal"      # 同主账本信号源
    assert half.book.pool_seed == st.universe.pool_seed   # 同主账本池 seed
    assert half.settings.universe.pool_seed == st.universe.pool_seed


# ============================================================
# 3. 任务二：连续风险预算
# ============================================================

def test_risk_budget_maps_market_states():
    """开启后按 market_state 映射连续系数（未知 → unknown 档）。"""
    cfg = _StratCfg(mode="block")
    assert _gate_scale(BookSpec(), cfg, "上涨", risk_budget=BUDGET_ON) == (False, 1.0)
    assert _gate_scale(BookSpec(), cfg, "震荡", risk_budget=BUDGET_ON) == (False, 0.7)
    assert _gate_scale(BookSpec(), cfg, "下跌", risk_budget=BUDGET_ON) == (False, 0.4)
    assert _gate_scale(BookSpec(), cfg, "未知", risk_budget=BUDGET_ON) == (False, 0.0)
    # reduce 模式下预算同样接管（否则会得到 0.5）
    assert _gate_scale(BookSpec(), _StratCfg(mode="reduce"), "下跌",
                       risk_budget=BUDGET_ON) == (False, 0.4)
    # 未知状态一律 unknown 档
    assert _risk_budget_coef(BUDGET_ON, "别的状态") == 0.0


def test_risk_budget_sentiment_still_blocks():
    """P6 情绪退潮独立于预算：退潮日仍禁开仓（block_mode=True → 0.0）。"""
    assert _gate_scale(BookSpec(), _StratCfg(), "上涨", sentiment_ebb=True,
                       risk_budget=BUDGET_ON) == (True, 0.0)


def test_risk_budget_ignored_when_market_filter_off():
    """market_filter=false（闸门整体关闭）→ 预算块不生效，仍是 1.0。"""
    cfg = _StratCfg(mode="block", market_filter=False)
    assert _gate_scale(BookSpec(), cfg, "下跌", risk_budget=BUDGET_ON) == (False, 1.0)


def test_risk_budget_respects_book_gate_overrides():
    """影子账本恒定系数优先于预算（half=0.5 / off=1.0 不被预算改写）。"""
    cfg = _StratCfg()
    assert _gate_scale(BookSpec(name="nogate_half", gate="half"), cfg, "下跌",
                       risk_budget=BUDGET_ON) == (False, 0.5)
    assert _gate_scale(BookSpec(name="nogate", gate="off"), cfg, "下跌",
                       risk_budget=BUDGET_ON) == (False, 1.0)


def test_load_risk_budget_yaml_and_clamp(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text(
        "gate_risk_budget:\n"
        "  enabled: true\n"
        "  uptrend: 1.2\n"        # 上界裁剪 → 1.0
        "  rangebound: 0.6\n"
        "  downtrend: -1\n"       # 下界裁剪 → 0.0
        "  unknown: 0.1\n",
        encoding="utf-8")
    out = _load_risk_budget(p)
    assert out == {"enabled": True, "uptrend": 1.0, "rangebound": 0.6,
                   "downtrend": 0.0, "unknown": 0.1}
    # 缺块 → 默认（关闭）
    assert _load_risk_budget(tmp_path / "missing.yaml")["enabled"] is False


def test_scan_impl_applies_continuous_budget(tmp_path, monkeypatch):
    """端到端：预算开启 → entry_scale=0.7，买入股数按 0.7 折扣计算。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=1_000_000.0,
                                 real_gate=False)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    summary = _run_scan(s, market_state="震荡", market_filter=True,
                        mode="block", budget=BUDGET_ON)
    assert summary["entry_scale"] == 0.7
    assert summary["risk_budget"]["coef"] == 0.7
    assert summary["risk_budget"]["map"]["rangebound"] == 0.7
    buys = _buys(broker)
    assert len(buys) == 1
    assert buys[0]["volume"] == 7000   # 0.10 × 1,000,000 × 0.7 / 10 = 7000 股
    assert summary["entry_scale"] == summary["risk_budget"]["coef"]


# ============================================================
# 4. 任务三：分批建仓 + MDD 25% 硬守卫
# ============================================================

def _seed_position(broker, code: str, price: float, volume: int):
    """在"前一交易日"建仓（成本=price），返回 (cost, notional)。"""
    broker.on_new_day(date(2026, 8, 27))
    res = broker.buy(code, price, volume)
    assert res.success
    return price, price * volume


@pytest.mark.parametrize("total_asset,expected", [
    (75_000.0, True),     # 峰 100k → 当前 75k = 恰好 25% → 触发
    (75_100.0, False),    # 24.9% → 未触及
    (90_000.0, False),
])
def test_mdd_guard_thresholds(tmp_path, monkeypatch, total_asset, expected):
    """MDD 守卫在 25% 处准确触发（24.9% 不触发）。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=total_asset)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    _wire_curve(tmp_path, [("2026-08-20", 100_000.0)])

    summary = _run_scan(s)

    payload = json.loads((tmp_path / "outputs" / "mdd_guard.json").read_text(
        encoding="utf-8"))
    assert payload["triggered"] is expected
    assert payload["mdd_limit"] == 0.25 and payload["mdd_limit_pct"] == "25%"
    assert summary["mdd_guard"]["triggered"] is expected
    # 与守卫一致：触发则不买入，未触发则正常买入
    assert (len(_buys(broker)) == 0) is expected
    if expected:
        assert any(r["name"] == "MDD 守卫" for r in summary["rejected"])
        assert any("MDD 守卫" in a["title"] for a in s.alerter.history)
    else:
        assert not any("MDD 守卫" in a["title"] for a in s.alerter.history)


def test_mdd_guard_seeds_from_init_capital(tmp_path, monkeypatch):
    """首次启用（无权益曲线）：以初始资金为高水位锚点，识别"启用前已发生"的回撤。

    否则曲线从启用日才开始，账户已从 10 万跌到 7.2 万的既有回撤会被漏判。
    """
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=100_000.0)
    _seed_position(broker, "600000.SH", 10.0, 7000)     # 成本 7 万
    # 持仓票现价 6（-40%）→ 总资产 ≈ 7.2 万；信号票为另一只（现价 10）
    _wire_scan(monkeypatch, s, {"600000.SZ": _sig("600000.SZ", 10.0)},
               {"600000.SZ": 10.0, "600000.SH": 6.0})
    n_buys_before = len(_buys(broker))

    summary = _run_scan(s)

    payload = json.loads((tmp_path / "outputs" / "mdd_guard.json").read_text(
        encoding="utf-8"))
    total = broker.query_asset()["total_asset"]
    assert payload["seeded_from_init_capital"] is True
    assert payload["init_capital"] == 100_000.0
    assert payload["mdd"] == pytest.approx(1 - total / 100_000.0, abs=1e-6)
    assert payload["mdd"] > 0.25
    assert payload["triggered"] is True
    assert len(_buys(broker)) == n_buys_before        # 守卫阻断买入
    assert _sells(broker) == []                       # 绝不自动平仓
    assert summary["mdd_guard"]["triggered"] is True


def _wire_curve(tmp_path, points):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "equity_curve.json").write_text(json.dumps({
        "points": [{"date": d, "total_asset": v} for d, v in points],
    }), encoding="utf-8")


def test_mdd_guard_exact_25_percent_and_no_auto_close(tmp_path, monkeypatch):
    """25% 触发时：写审计文件 + 告警 + 一个买单都不下 + 不卖出任何持仓。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=75_000.0)
    _seed_position(broker, "600000.SH", 10.0, 1000)   # 已有持仓 1 万
    n_buys_before = len(_buys(broker))
    _wire_scan(monkeypatch, s, {"600000.SZ": _sig("600000.SZ", 10.0)},
               {"600000.SZ": 10.0, "600000.SH": 10.0})
    _wire_curve(tmp_path, [("2026-08-20", 100_000.0)])

    summary = _run_scan(s)

    payload = json.loads((tmp_path / "outputs" / "mdd_guard.json").read_text(
        encoding="utf-8"))
    total_now = broker.query_asset()["total_asset"]
    expected_mdd = 1.0 - total_now / 100_000.0   # 已有持仓的买入佣金会让回撤略高于 25%
    assert payload["triggered"] is True
    assert payload["mdd"] == pytest.approx(expected_mdd, abs=1e-6)
    assert payload["mdd"] >= 0.25
    assert payload["mdd_pct"] == f"{expected_mdd:.2%}"
    assert payload["policy"] == MDD_GUARD_POLICY
    assert payload["policy"]["auto_close_position"] is False
    assert payload["book"] == "main"
    # 告警推送
    assert any("MDD 守卫" in a["title"] and a["level"] == "error"
               for a in s.alerter.history)
    # 不买入（含新仓与加仓）——守卫触发后本轮买单数为 0
    assert len(_buys(broker)) == n_buys_before
    # 绝不自动平仓
    assert _sells(broker) == []
    # 权益曲线追加当日点（峰不动 → 回撤保持）
    curve = json.loads((tmp_path / "data" / "equity_curve.json").read_text(encoding="utf-8"))
    assert len(curve["points"]) == 2
    assert curve["points"][-1] == {"date": str(LATEST), "total_asset": broker.query_asset()["total_asset"]}
    # summary 同时登记 MDD 守卫状态（scale_in 键已随功能一并删除）
    assert summary["mdd_guard"]["triggered"] is True


def test_mdd_guard_alert_once_per_trigger_state(tmp_path, monkeypatch):
    """同一触发状态不重复告警（首次触发告警一次；同日重跑幂等）。"""
    s, broker, _ = _make_scanner(tmp_path, monkeypatch, init=75_000.0)
    _wire_scan(monkeypatch, s, {"600000.SH": _sig("600000.SH", 10.0)},
               {"600000.SH": 10.0})
    _wire_curve(tmp_path, [("2026-08-20", 100_000.0)])

    for _ in range(2):
        _run_scan(s, market_state="下跌", market_filter=True, mode="block")

    alerts = [a for a in s.alerter.history if "MDD 守卫" in a["title"]]
    assert len(alerts) == 1, f"重复告警 {len(alerts)} 次"
    curve = json.loads((tmp_path / "data" / "equity_curve.json").read_text(encoding="utf-8"))
    assert len(curve["points"]) == 2, "同日权益点幂等覆盖，不得重复追加"


def test_drawdown_helpers_exact():
    """回撤纯函数：峰值/当前/最大回撤口径。"""
    m = _drawdown_metrics([
        {"date": "2026-01-03", "total_asset": 75_000},
        {"date": "2026-01-01", "total_asset": 80_000},
        {"date": "2026-01-02", "total_asset": 100_000},
    ])
    assert m["peak"] == 100_000 and m["peak_date"] == "2026-01-02"
    assert m["max_drawdown"] == pytest.approx(0.25)
    assert m["current_drawdown"] == pytest.approx(0.25)
    assert m["trough_date"] == "2026-01-03"
    assert _drawdown_metrics([])["max_drawdown"] is None
    assert _drawdown_metrics([{"date": "x", "total_asset": 0}])["max_drawdown"] is None
    # 同日覆盖（幂等）
    assert _merge_equity_point(
        [{"date": "2026-01-01", "total_asset": 1.0}], "2026-01-01", 2.0
    ) == [{"date": "2026-01-01", "total_asset": 2.0}]


# ============================================================
# 5. 结构边界：只买不卖 / 默认关闭时无副作用
# ============================================================

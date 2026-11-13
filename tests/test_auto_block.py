"""P0-3 评审自动阻断 + P1-4 无闸门影子账本 回归测试。

纪律（本文件全部用例的前提）：
    - P0-3 只告警 + 标记"建议冻结新开仓"——不自动平仓、不自动改配置；
      测试通过"只读统计入参 + 输出文件断言 + 告警次数断言"钉死边界；
    - P1-4 影子账本与主账本状态/资金/持仓完全隔离，只共用信号源；
    - 阈值来自 config/settings.yaml（可配），默认 50 轮 / 45%。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
from datetime import date
from pathlib import Path

import pytest

from lihu_quantify.config import ShadowBookConfig
from lihu_quantify.monitor import auto_block
from lihu_quantify.monitor import scheduler as sched_mod
from lihu_quantify.monitor.auto_block import (
    STATE_BLOCKED,
    STATE_INSUFFICIENT,
    STATE_OK,
    BlockConfig,
    check_and_block,
    load_block_config,
    wilson_lower_bound,
)
from lihu_quantify.monitor.scheduler import (
    BookSpec,
    DailyScanner,
    _gate_scale,
)


# ============================================================
# 0. 测试基建
# ============================================================

class _FakeAlerter:
    """记录 send 调用（不触网、不发邮件）。"""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def send(self, subject: str, body: str = "", level: str = "info") -> bool:
        self.calls.append((subject, body, level))
        return True


def _stats(rounds: int, win_rate: float, name: str = "main") -> dict:
    return {"name": name, "closed_rounds": rounds, "win_rate": win_rate}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ============================================================
# 1. P0-3：触发 / 不触发 / 未到轮次
# ============================================================

def test_trigger_when_below_precheck_line(tmp_path):
    """main 98 轮胜率 25.51% → 触发阻断、输出审计文件、推送一次告警。"""
    out = tmp_path / "auto_block.json"
    alerter = _FakeAlerter()
    d = check_and_block(
        [_stats(98, 0.2551)], output_path=out, alerter=alerter,
        now=dt.datetime(2026, 11, 14, 16, 30, 0),
    )

    assert d.triggered is True
    assert d.recommend_freeze is True          # 只是标记，不执行
    assert d.state == STATE_BLOCKED
    assert d.notified == ["main"]
    assert len(alerter.calls) == 1
    assert alerter.calls[0][2] == "error"      # 走实通道（邮件）级别

    data = _read(out)
    assert data["triggered"] is True
    assert data["triggered_at"] == "2026-11-14 16:30:00"
    assert data["last_notified_state"] == {"main": STATE_BLOCKED}
    assert data["policy"]["auto_close_position"] is False
    assert data["policy"]["auto_modify_config"] is False
    # summary/last_scan 内的同一份数据也带硬边界（供看板/复盘核对）
    assert d.as_dict()["policy"]["auto_close_position"] is False
    led = data["ledgers"][0]
    assert led["name"] == "main"
    assert led["closed_rounds"] == 98
    assert led["win_rate"] == pytest.approx(0.2551)
    # Wilson 95% 下界：98 轮 25.51% → 约 17.9%（低于 45% 预检线）
    assert led["wilson_ci_lower"] == pytest.approx(0.179, abs=0.005)
    assert 0.0 < led["wilson_ci_lower"] < led["win_rate"]
    # 简报一行含关键结论，可直接进日报
    line = d.brief_line()
    assert "【评审阻断】" in line and "建议冻结新开仓" in line
    assert "系统不会自动平仓" in line


def test_no_trigger_when_win_rate_ok(tmp_path):
    """60 轮胜率 55% → 不触发、不告警，状态 ok。"""
    out = tmp_path / "auto_block.json"
    alerter = _FakeAlerter()
    d = check_and_block([_stats(60, 0.55)], output_path=out, alerter=alerter)

    assert d.triggered is False
    assert d.recommend_freeze is False
    assert d.state == STATE_OK
    assert d.notified == []
    assert alerter.calls == []
    assert d.brief_line() == ""
    data = _read(out)
    assert data["triggered"] is False
    assert data["last_notified_state"] == {}


def test_no_trigger_before_min_rounds(tmp_path):
    """未满 50 轮 → 积累期，不触发（防小样本误伤，USER_GUIDE 同口径）。"""
    out = tmp_path / "auto_block.json"
    d = check_and_block([_stats(12, 0.10)], output_path=out,
                        alerter=_FakeAlerter())
    assert d.triggered is False
    assert d.state == STATE_INSUFFICIENT
    assert _read(out)["ledgers"][0]["state"] == STATE_INSUFFICIENT


# ============================================================
# 2. P0-3：幂等（同账本同状态不重复告警）
# ============================================================

def test_idempotent_same_state(tmp_path):
    """同日/次日重复检查：同状态只告警一次，并记 duplicates。"""
    out = tmp_path / "auto_block.json"
    alerter = _FakeAlerter()

    d1 = check_and_block([_stats(98, 0.2551)], output_path=out, alerter=alerter)
    d2 = check_and_block([_stats(99, 0.2480)], output_path=out, alerter=alerter)

    assert d1.notified == ["main"] and d1.duplicates == []
    assert d2.triggered is True                 # 规则仍处于越线状态
    assert d2.notified == []                    # 但不再重复告警
    assert d2.duplicates == ["main"]
    assert len(alerter.calls) == 1, "同账本同状态必须只告警一次"
    assert _read(out)["last_notified_state"] == {"main": STATE_BLOCKED}


def test_idempotent_rearm_after_recovery(tmp_path):
    """状态回到合规后解除幂等锁；再次越线才重新告警。"""
    out = tmp_path / "auto_block.json"
    alerter = _FakeAlerter()

    check_and_block([_stats(98, 0.2551)], output_path=out, alerter=alerter)
    ok = check_and_block([_stats(99, 0.50)], output_path=out, alerter=alerter)
    re_blocked = check_and_block([_stats(100, 0.40)], output_path=out, alerter=alerter)

    assert ok.state == STATE_OK
    assert re_blocked.notified == ["main"], "合规恢复后再次越线应重新告警"
    assert len(alerter.calls) == 2


# ============================================================
# 3. P0-3：阈值可配（config/settings.yaml）+ Wilson 函数
# ============================================================

def test_config_from_yaml_overrides_defaults(tmp_path):
    """阈值来自 YAML：min_rounds=5 / min_win_rate=0.50 → 10 轮 40% 会触发。"""
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "review_block_min_rounds: 5\n"
        "review_block_min_win_rate: 0.50\n",
        encoding="utf-8",
    )
    loaded = load_block_config(path=cfg)
    assert loaded == BlockConfig(min_rounds=5, min_win_rate=0.50)

    d = check_and_block([_stats(10, 0.40)], config_path=cfg,
                        output_path=tmp_path / "a.json", alerter=_FakeAlerter())
    assert d.triggered is True
    assert d.config == {"min_rounds": 5, "min_win_rate": 0.5}

    # 同一组数据在默认阈值（50 轮）下不触发
    d2 = check_and_block([_stats(10, 0.40)], output_path=tmp_path / "b.json",
                         alerter=_FakeAlerter())
    assert d2.triggered is False


def test_config_defaults_and_project_yaml():
    """默认 50/0.45；项目 settings.yaml 的 review_block_* 键必须存在且等价。"""
    assert BlockConfig().min_rounds == 50
    assert BlockConfig().min_win_rate == pytest.approx(0.45)
    loaded = load_block_config()
    assert loaded.min_rounds == 50
    assert loaded.min_win_rate == pytest.approx(0.45)


def test_wilson_lower_bound():
    """Wilson 下界：小样本更低、n=0 → None。"""
    assert wilson_lower_bound(0, 0) is None
    assert wilson_lower_bound(0, 10) == pytest.approx(0.0)
    low_n = wilson_lower_bound(5, 20)      # 25% / 20 轮
    high_n = wilson_lower_bound(25, 98)    # 25.5% / 98 轮
    assert low_n < high_n < 0.2551
    assert wilson_lower_bound(98, 98) > 0.95   # 全胜小样本下界仍应较高


# ============================================================
# 4. P0-3：结构边界——绝不自动平仓 / 不自动改配置
# ============================================================

def test_auto_block_module_has_no_trading_side_effects():
    """结构断言：auto_block 不 import 交易模块、不调用买卖接口、无 broker 形参。"""
    source = inspect.getsource(auto_block)
    for forbidden in ("PaperBroker", "OrderManagementSystem", ".sell(", ".buy(",
                      "execution."):
        assert forbidden not in source, f"auto_block 不得出现 {forbidden}"

    import inspect as _inspect

    params = _inspect.signature(check_and_block).parameters
    assert "broker" not in params, "check_and_block 不接受 broker（从结构上无法下单）"
    assert "ledgers" in params


# ============================================================
# 5. P1-4：无闸门影子——闸门判定单元 + 主账本隔离
# ============================================================

class _StratCfg:
    """最小策略配置替身（block 模式）。"""

    market_filter = True
    market_filter_mode = "block"


def test_gate_scale_gate_off_bypasses_block():
    """gate=off：下跌段仍 entry_scale=1.0 且不禁止开仓；主账本 gate=follow 仍被 block。"""
    main_book = BookSpec(name="main")
    nogate_book = BookSpec(name="nogate", gate="off")

    block_mode, scale = _gate_scale(main_book, _StratCfg(), "下跌")
    assert (block_mode, scale) == (True, 0.0)

    block_mode, scale = _gate_scale(nogate_book, _StratCfg(), "下跌")
    assert (block_mode, scale) == (False, 1.0)

    # 上涨段两者一致（唯一差异只在闸门）
    assert _gate_scale(main_book, _StratCfg(), "上涨") == (False, 1.0)
    assert _gate_scale(nogate_book, _StratCfg(), "上涨") == (False, 1.0)

    # 情绪退潮对 gate=off 也不生效（"闸门恒 off"语义；情绪默认关，仅作边界登记）
    assert _gate_scale(nogate_book, _StratCfg(), "下跌", sentiment_ebb=True) == (False, 1.0)


def _settings(tmp_path: Path):
    from lihu_quantify.config import Settings

    (tmp_path / "data").mkdir(exist_ok=True)
    return Settings(
        duckdb_path=str(tmp_path / "t.duckdb"),
        cache_dir=str(tmp_path / "cache"),
        tushare_token="fake-token-for-init-only",
    )


def test_nogate_shadow_does_not_touch_main_account(tmp_path, monkeypatch):
    """影子账本买入/持仓/状态文件与主账本完全隔离（不影响主账本资金与持仓）。"""
    from lihu_quantify.execution import paper_trade

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    monkeypatch.setattr(paper_trade, "_ROOT", tmp_path)

    st = _settings(tmp_path)
    main = DailyScanner(st, mode="paper")
    nogate = DailyScanner(
        st.model_copy(deep=True), mode="paper",
        book=BookSpec(name="nogate", pool_seed=st.universe.pool_seed,
                      silent=True, strategy="intraday_reversal", gate="off"),
    )

    # 路径隔离：主账本 paper_state.json / 影子 paper_state_nogate.json
    assert main.broker.state_file == str(tmp_path / "data" / "paper_state.json")
    assert nogate.broker.state_file == str(tmp_path / "data" / "paper_state_nogate.json")
    assert nogate.book.gate == "off"
    assert nogate.book.silent is True

    main_asset_before = main.broker.query_asset()
    main.broker.on_new_day(date(2026, 11, 13))   # 落一次主账本状态文件（含 trade_day）
    nogate.broker.buy("600000.SH", 10.0, 1000)   # 影子买入（主账本无此票）
    main_asset_after = main.broker.query_asset()

    assert main_asset_after == main_asset_before, "影子买入不得改变主账本资金/持仓"
    assert main.broker.query_positions() == []
    assert "600000.SH" in nogate.broker.positions

    main_state = _read(tmp_path / "data" / "paper_state.json")
    assert main_state["positions"] == {}
    assert main_state["trades"] == []
    shadow_state = _read(tmp_path / "data" / "paper_state_nogate.json")
    assert "600000.SH" in shadow_state["positions"]


# ============================================================
# 6. P1-4：日报一行 + shadow_ledgers 配置装配
# ============================================================

def test_shadow_ledger_digest_line(tmp_path, monkeypatch):
    """日报一行：当日无闸门影子命中数；缺数据/隔日 → 空串（不写假数据）。"""
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    stats_path = tmp_path / "data" / "shadow_ledger_stats.json"
    stats_path.write_text(json.dumps({
        "trade_date": "2026-11-13",
        "ledgers": {"nogate": {"signals": 7, "executed": 3,
                               "entry_scale": 1.0, "gate": "off"}},
    }, ensure_ascii=False), encoding="utf-8")

    s = DailyScanner.__new__(DailyScanner)
    line = s._shadow_ledger_digest_line(date(2026, 11, 13))
    assert line == "无闸门影子：今日命中 7 个信号（闸门 off，反事实对照）"
    assert "无闸门影子：今日命中 7 个信号" in line
    assert s._shadow_ledger_digest_line(date(2026, 11, 12)) == "", "隔日必须失效"


def test_shadow_ledger_stats_write_merge(tmp_path, monkeypatch):
    """影子落盘：按交易日记 signals/executed/gate，供主账本日报读取。"""
    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    s = DailyScanner.__new__(DailyScanner)
    s.book = BookSpec(name="nogate", silent=True, gate="off")
    s._write_shadow_ledger_stats(
        date(2026, 11, 13),
        {"signals": 4, "executed": [{"ts_code": "X"}], "entry_scale": 1.0},
    )
    data = _read(tmp_path / "data" / "shadow_ledger_stats.json")
    assert data["trade_date"] == "2026-11-13"
    assert data["ledgers"]["nogate"] == {
        "signals": 4, "executed": 1, "entry_scale": 1.0, "gate": "off"}


def test_scheduler_builds_nogate_from_shadow_ledgers(tmp_path, monkeypatch):
    """setup_scheduler：shadow_ledgers 块 → nogate 账本（gate=off、同主账本信号源）。"""
    import apscheduler.schedulers.blocking as blk

    monkeypatch.setattr(sched_mod, "_ROOT", tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "settings.yaml").write_text(
        "shadow_ledgers:\n"
        "  - name: \"nogate\"\n"
        "    enabled: true\n"
        "    signal_source: \"main\"\n"
        "    market_gate: \"off\"\n",
        encoding="utf-8",
    )

    st = _settings(tmp_path)
    st.shadow_books = [ShadowBookConfig(
        name="nogate", seed=42, strategy="cherry_claw")]
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
    assert set(by_name) == {"main", "nogate"}
    ng = by_name["nogate"]
    assert ng.book.gate == "off" and ng.book.silent is True
    assert ng.book.strategy == "intraday_reversal", "同主账本信号源（策略对齐）"
    assert ng.book.pool_seed == st.universe.pool_seed, "同主账本信号源（池 seed 对齐）"
    assert ng.settings.universe.pool_seed == st.universe.pool_seed

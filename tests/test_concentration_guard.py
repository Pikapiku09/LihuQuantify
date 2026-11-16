"""分散度检查（min_concurrent_positions）的单元测试（2026-11-16）。

守的是：
1. 持仓数低于下限时才告警，达到或超过不告警
2. 告警文案里必须带上「为什么」（p95 回撤对比），否则用户只会当成噪音
3. 空持仓/None 不得抛异常
4. 仓库配置必须真的把下限设成 3，且单票上限是 0.15（这两个是配套的决策）
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.risk.limits import (  # noqa: E402
    MAX_SINGLE_POSITION,
    MIN_CONCURRENT_POSITIONS,
    concentration_warning,
)


def test_warns_below_floor():
    msg = concentration_warning(1)
    assert msg and "分散度不足" in msg
    assert "48.4%" in msg and "16.7%" in msg, "告警必须说明依据，否则是噪音"


def test_no_warning_at_or_above_floor():
    assert concentration_warning(MIN_CONCURRENT_POSITIONS) is None
    assert concentration_warning(MIN_CONCURRENT_POSITIONS + 5) is None


def test_two_positions_still_warns():
    assert concentration_warning(2) is not None


def test_empty_is_safe():
    assert concentration_warning(0) is not None
    assert concentration_warning(None) is not None


def test_repo_config_pairs_cap_with_floor():
    """单票上限与持仓下限是配套决策：只改一个等于没改。"""
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    risk = cfg.get("risk") or {}
    assert float(risk.get("max_single_position")) == 0.15
    assert int(risk.get("min_concurrent_positions")) == 3
    assert MAX_SINGLE_POSITION == 0.15
    assert MIN_CONCURRENT_POSITIONS == 3

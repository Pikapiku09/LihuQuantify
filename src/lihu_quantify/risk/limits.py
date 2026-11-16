"""资金/仓位/板块铁律常量 — 单一事实来源（读 settings.risk）。

P2-9-3（第十一轮）：0.25/0.40 此前在 checklist / position_limit / report 三处
硬编码，统一收敛到本文件；改限额只动 `config/settings.yaml` 的 `risk:` 段。

注意：此处 import 时解析（与 get_settings 单例一致），运行期改配置需重启进程生效。
"""

from __future__ import annotations

from ..config import get_settings

_risk = get_settings().risk

# 单票最大仓位（上限 15%；2026-11-16 曾放宽到 20%，经 H-005/H-007 两度否证后改回）
# 与单板块最大合计仓位（上限 40%）
MAX_SINGLE_POSITION: float = _risk.max_single_position
MAX_SECTOR_POSITION: float = _risk.max_sector_position
# 同时持仓下限（2026-11-16）：低于该值视为分散度不足，日报提示，不自动阻断开仓。
MIN_CONCURRENT_POSITIONS: int = int(getattr(_risk, "min_concurrent_positions", 3))


def concentration_warning(n_positions: int, min_positions: int | None = None) -> str | None:
    """分散度检查：持仓数低于下限时返回中文提示，否则返回 None。

    为什么需要它：集中度压力测试（outputs/concentration_stress.json）显示决定回撤的是
    **同时持仓数**而不是单票上限 —— K=1 的 p95 回撤 48.4%，K=3 只有 16.7%；
    而 cap 15%→20% 仅 +3.65~3.98pp。账户级实测最大回撤 -39.75% 与 K=1~2 的测算吻合。
    单票 15% 上限只有在「真的分到 3 只以上」时才起作用，所以这条要和上限成对使用。

    只提示不阻断：历史行为是「想买几只买几只」，加自动阻断属于行为变更，需另行预注册。
    """
    floor = MIN_CONCURRENT_POSITIONS if min_positions is None else int(min_positions)
    n = int(n_positions or 0)
    if n >= floor:
        return None
    return (f"分散度不足：当前持仓 {n} 只 < 下限 {floor} 只。"
            f"集中度压力测试显示 K=1 时 p95 回撤 48.4%、K=3 时 16.7% —— "
            f"建议在单票 ≤{MAX_SINGLE_POSITION:.0%} 的前提下凑够 {floor} 只再上仓位")
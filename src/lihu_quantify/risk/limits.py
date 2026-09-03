"""资金/仓位/板块铁律常量 — 单一事实来源（读 settings.risk）。

P2-9-3（第十一轮）：0.25/0.40 此前在 checklist / position_limit / report 三处
硬编码，统一收敛到本文件；改限额只动 `config/settings.yaml` 的 `risk:` 段。

注意：此处 import 时解析（与 get_settings 单例一致），运行期改配置需重启进程生效。
"""

from __future__ import annotations

from ..config import get_settings

_risk = get_settings().risk

# 单票最大仓位（上限 25%）与单板块最大合计仓位（上限 40%）
MAX_SINGLE_POSITION: float = _risk.max_single_position
MAX_SECTOR_POSITION: float = _risk.max_sector_position
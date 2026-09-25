"""研究模块（Phase 0 脚手架）：论文因子 → 三级门禁验证。

门禁流程（见 docs/论文筛选打分卡.md 与 docs/因子验收单.md）：
    门禁1 可复现性（人工/脚本复现论文核心表）
    门禁2 单因子 IC/分组/成本（ic.py + acceptance.py）
    门禁3 组合稳健性（walkforward.py + regime.py 分状态检验）

数据约定：面板 DataFrame，列 = [trade_date, ts_code, open, high, low, close, vol, amount]，
按 (ts_code, trade_date) 升序。与 backtest/indicators 的日线列名一致。
"""

from .factor import FACTOR_REGISTRY, add_factor, compute_factor, momentum, reversal, turnover_premium
from .ic import calc_ic, quantile_backtest, deflated_sharpe
from .regime import classify_regime
from .walkforward import split_walkforward
from .acceptance import evaluate_factor, AcceptanceResult

__all__ = [
    "FACTOR_REGISTRY", "add_factor", "compute_factor", "momentum", "reversal", "turnover_premium",
    "calc_ic", "quantile_backtest", "deflated_sharpe",
    "classify_regime", "split_walkforward", "evaluate_factor", "AcceptanceResult",
]

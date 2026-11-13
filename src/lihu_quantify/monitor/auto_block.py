"""评审自动阻断（P0-3，2026-11-14）：预检线「≥50 轮且胜率<45% → 停下检查」的代码化。

依据（docs/USER_GUIDE.md §5.0「验收标准与决策时间表」原文）：
    | ≥50 笔轮次 | 初步胜率/盈亏比 vs 回测（58.9%/1.25）| 若严重偏离（胜率<45%）→ 停下检查 |

本模块把这条"人工承诺"变成每次巡检自动执行的防呆检查，但**边界极窄**：

    只读统计 → 只写一份审计文件（outputs/auto_block.json）→ 只推一条告警
    - 不接收 broker，不 import 任何 execution 模块，全文件无 buy/sell 调用；
    - 不自动平仓、不自动修改 config/settings.yaml、不自动停止调度；
    - 输出仅标记「建议冻结新开仓（人工决策）」，最终由人决定。

触发规则（全部从 config/settings.yaml 可配，缺省 min_rounds=50 / min_win_rate=0.45）：
    review_block_min_rounds: 50
    review_block_min_win_rate: 0.45

统计口径：轮次/胜率一律取自 monitor/review_progress.py（唯一统计入口，买入-卖出
FIFO 配对为一轮）；本模块不重算配对、不重算胜率，只做阈值判断与 Wilson 95%
置信区间下界估计（回答"低胜率是真差还是小样本噪声"）。

幂等：同账本同状态不重复告警——outputs/auto_block.json 的 last_notified_state
记录已告警账本；状态回到合规后自动解除，再次越线才重新告警。
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import yaml
from loguru import logger

# 项目根（src/lihu_quantify/monitor/auto_block.py 向上 3 级）
_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_MIN_ROUNDS = 50          # USER_GUIDE §5.0 预检点轮次
DEFAULT_MIN_WIN_RATE = 0.45      # USER_GUIDE §5.0 预检线胜率
DEFAULT_OUTPUT_PATH = _ROOT / "outputs" / "auto_block.json"
DEFAULT_CONFIG_PATH = _ROOT / "config" / "settings.yaml"
Z_95 = 1.96                      # 95% 正态分位

# 状态枚举（写入 auto_block.json 的 ledgers[].state）
STATE_BLOCKED = "blocked"                    # 已越线：建议冻结新开仓
STATE_OK = "ok"                              # 达轮次且胜率达标
STATE_INSUFFICIENT = "insufficient_rounds"   # 未到 50 轮预检点
STATE_NO_ROUNDS = "no_rounds"                # 尚无已平仓轮次
STATE_NO_WIN_RATE = "no_win_rate"            # 有轮次但胜率缺失（异常，按不阻断处理）

# 硬边界（写入审计文件，供人工/看板核对——本模块没有任何执行能力）
POLICY = {
    "action": "只告警 + 标记建议冻结新开仓（人工决策）",
    "auto_close_position": False,
    "auto_modify_config": False,
    "auto_stop_scheduler": False,
}


# ===== 配置 =====


@dataclass
class BlockConfig:
    """阻断阈值（可被 settings.yaml 覆盖）。"""

    min_rounds: int = DEFAULT_MIN_ROUNDS
    min_win_rate: float = DEFAULT_MIN_WIN_RATE

    def as_dict(self) -> dict:
        return {"min_rounds": self.min_rounds,
                "min_win_rate": self.min_win_rate}


def _as_int(value, default: int) -> int:
    """宽松取整数：数字/数字字符串可用，其余（None/MagicMock/异常）回退默认值。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _as_float(value, default: float) -> float:
    """宽松取浮点：同上，异常回退默认值。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return default
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def load_block_config(settings=None, path=None) -> BlockConfig:
    """读取阻断阈值，优先级：settings 显式字段 > settings.yaml > 模块默认。

    说明：`config.py` 的 Settings 目前未建模这两个键（不在 P0-3 写作用域），
    顶层 YAML 键会被 pydantic 以 extra="ignore" 丢弃，故此处直接读原始 YAML；
    若未来 config.py 增加字段，settings 属性会自动优先，无需改本函数。
    """
    min_rounds = _as_int(
        getattr(settings, "review_block_min_rounds", None), DEFAULT_MIN_ROUNDS)
    min_win_rate = _as_float(
        getattr(settings, "review_block_min_win_rate", None), DEFAULT_MIN_WIN_RATE)

    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    data: dict = {}
    try:
        if cfg_path.exists():
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"[评审阻断] 配置读取失败，用默认阈值: {e}")
        data = {}
    if isinstance(data, dict):
        # 仅当 settings 未给出（仍是默认值）时才用 YAML 覆盖
        if min_rounds == DEFAULT_MIN_ROUNDS:
            min_rounds = _as_int(data.get("review_block_min_rounds"), min_rounds)
        if min_win_rate == DEFAULT_MIN_WIN_RATE:
            min_win_rate = _as_float(data.get("review_block_min_win_rate"), min_win_rate)

    min_rounds = max(0, min_rounds)
    min_win_rate = min(max(min_win_rate, 0.0), 1.0)
    return BlockConfig(min_rounds=min_rounds, min_win_rate=min_win_rate)


# ===== 统计（Wilson 95% CI 下界） =====


def wilson_lower_bound(wins, n, z: float = Z_95) -> Optional[float]:
    """Wilson score 区间 95% 下界（n<=0 → None）。

    用途：胜率 25.51% / 98 轮 → 下界约 17.9%。低胜率是否"真差"看下界是否低于
    预检线 45%——比点估计更抗小样本噪声（本系统同时登记点估计与区间）。
    """
    try:
        n = int(n)
        wins = int(wins)
    except (TypeError, ValueError, OverflowError):
        return None
    if n <= 0:
        return None
    wins = min(max(wins, 0), n)
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (centre - margin) / denom)


def _normalize_win_rate(rate) -> Optional[float]:
    """胜率归一化到 0~1：显式百分数（>1）按 /100 处理；None/异常 → None。"""
    v = _as_float(rate, float("nan"))
    if v != v:          # NaN
        return None
    if v > 1.0:
        v = v / 100.0
    return min(max(v, 0.0), 1.0)


def _ledger_metrics(entry: dict) -> dict:
    """单个账本 → 统一指标（轮次/胜率/胜场/Wilson 下界）。

    入参兼容三种形态（便于调度层与测试各取所需）：
        {"name", "closed_rounds", "win_rate"}      # 直接用 review_progress 的结果
        {"name", "closed_rounds", "wins"}          # 给胜场
        {"name", "trades": [...]}                  # 给成交流水（内部走唯一统计入口）
    """
    name = str(entry.get("name") or "main")
    closed = 0
    win_rate: Optional[float] = None
    source = "stats"

    trades = entry.get("trades")
    if trades is not None:
        source = "trades"
        try:
            from .review_progress import review_stats

            stats = review_stats(list(trades), name=name)
            closed = _as_int(stats.get("closed_rounds"), 0)
            win_rate = _normalize_win_rate(stats.get("win_rate"))
        except Exception as e:  # noqa: BLE001 - 统计失败不得杀死巡检
            logger.warning(f"[评审阻断] {name} 轮次统计失败（跳过该账本）: {e}")
            closed, win_rate = 0, None
    else:
        closed = max(0, _as_int(entry.get("closed_rounds"), 0))
        if entry.get("win_rate") is not None:
            win_rate = _normalize_win_rate(entry.get("win_rate"))
        elif entry.get("wins") is not None and closed > 0:
            win_rate = _normalize_win_rate(
                _as_float(entry.get("wins"), 0.0) / closed)

    wins = int(round(win_rate * closed)) if (win_rate is not None and closed > 0) else 0
    wins = min(max(wins, 0), closed)
    return {
        "name": name,
        "closed_rounds": closed,
        "wins": wins,
        "win_rate": win_rate,
        "wilson_ci_lower": wilson_lower_bound(wins, closed),
        "source": source,
    }


def _classify(metrics: dict, cfg: BlockConfig) -> str:
    """按预检线分类账本状态（严格按 USER_GUIDE §5.0 口径）。"""
    closed = metrics["closed_rounds"]
    if closed <= 0:
        return STATE_NO_ROUNDS
    if closed < cfg.min_rounds:
        return STATE_INSUFFICIENT
    if metrics["win_rate"] is None:
        return STATE_NO_WIN_RATE
    if metrics["win_rate"] < cfg.min_win_rate:
        return STATE_BLOCKED
    return STATE_OK


# ===== 决策对象 =====


@dataclass
class BlockDecision:
    """一次评审检查的结论（纯数据，可直接 JSON 化进 summary/last_scan）。"""

    triggered: bool = False
    state: str = STATE_NO_ROUNDS            # 主账本（第一个账本）状态
    recommend_freeze: bool = False          # 建议冻结新开仓（人工决策，不自动执行）
    ledgers: list[dict] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)      # 本次真正推送的账本
    duplicates: list[str] = field(default_factory=list)    # 幂等跳过（同状态已告警）
    output_path: str = ""
    triggered_at: Optional[str] = None
    config: dict = field(default_factory=dict)
    last_notified_state: dict = field(default_factory=dict)

    @property
    def blocked_ledgers(self) -> list[dict]:
        return [x for x in self.ledgers if x.get("state") == STATE_BLOCKED]

    def brief_line(self) -> str:
        """给日报/简报用的一行（未触发 → 空串，调用方无需再判断）。"""
        blocked = self.blocked_ledgers
        if not blocked:
            return ""
        items = []
        for b in blocked:
            rate = b.get("win_rate")
            rate_s = f"{rate:.2%}" if rate is not None else "-"
            wl = b.get("wilson_ci_lower")
            wl_s = f"，Wilson 95% 下界 {wl:.2%}" if wl is not None else ""
            items.append(f"{b['name']} {b['closed_rounds']} 轮胜率 {rate_s}{wl_s}")
        return ("【评审阻断】" + "；".join(items)
                + "（预检线：≥50 轮且胜率<45% 需停下检查）；建议冻结新开仓（人工决策），"
                  "系统不会自动平仓、不会自动改配置。")

    def as_dict(self) -> dict:
        """JSON 安全字典（写 outputs/auto_block.json 与 summary["auto_block"]）。"""
        return {
            "triggered": self.triggered,
            "state": self.state,
            "recommend_freeze": self.recommend_freeze,
            "ledgers": self.ledgers,
            "notified": self.notified,
            "duplicates": self.duplicates,
            "output_path": self.output_path,
            "triggered_at": self.triggered_at,
            "config": self.config,
            "last_notified_state": self.last_notified_state,
            "policy": dict(POLICY),
        }


# ===== 落盘 / 告警 =====


def _read_output(path: Path) -> dict:
    """读上次 auto_block.json（不存在/损坏 → {}，绝不抛异常）。"""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[评审阻断] auto_block.json 读取失败（按首次处理）: {e}")
    return {}


def _write_output(path: Path, payload: dict) -> bool:
    """原子写审计文件（临时文件 + 替换）；失败只告警，不影响巡检。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"[评审阻断] auto_block.json 写入失败: {e}")
        return False


def _format_alert(ledger: dict, cfg: BlockConfig) -> tuple[str, str]:
    """构造告警标题/正文（可直接被 Alerter 或人工通道消费）。"""
    rate = ledger.get("win_rate")
    rate_s = f"{rate:.2%}" if rate is not None else "-"
    wl = ledger.get("wilson_ci_lower")
    wl_s = f"{wl:.2%}" if wl is not None else "-"
    subject = (f"评审阻断：{ledger['name']} {ledger['closed_rounds']} 轮"
               f"胜率 {rate_s} < 预检线 {cfg.min_win_rate:.0%}")
    detail = (
        f"账本 {ledger['name']} 已平仓 {ledger['closed_rounds']} 轮，"
        f"胜率 {rate_s}（{ledger['wins']} 胜），Wilson 95% 置信下界 {wl_s}。\n"
        f"预检线（docs/USER_GUIDE.md §5.0）：≥{cfg.min_rounds} 轮且胜率<"
        f"{cfg.min_win_rate:.0%} → 停下检查。\n"
        "本系统仅告警并标记「建议冻结新开仓」；不会自动平仓、不会自动修改配置，"
        "请人工复核后再决定。"
    )
    return subject, detail


def _notify(
    alerter,
    notify: Optional[Callable[[str, str], None]],
    subject: str,
    detail: str,
) -> None:
    """推送告警：复用现有 Alerter → 自定义 notify → 控制台 + logger.warning。"""
    if alerter is not None:
        try:
            alerter.send(subject, detail, level="error")
            return
        except Exception as e:  # noqa: BLE001 - 告警通道异常不得杀死巡检
            logger.warning(f"[评审阻断] 告警通道发送失败，回退日志: {e}")
    if notify is not None:
        try:
            notify(subject, detail)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[评审阻断] notify 回调失败，回退日志: {e}")
    # 无实通道 → 控制台 + logger.warning（loguru 走 stderr，无 GBK 编码风险）
    logger.warning(f"[评审阻断] {subject} | {detail}")


# ===== 主入口 =====


def check_and_block(
    ledgers: Iterable[dict],
    *,
    settings=None,
    output_path=None,
    config_path=None,
    alerter=None,
    notify: Optional[Callable[[str, str], None]] = None,
    now: Optional[_dt.datetime] = None,
) -> BlockDecision:
    """检查各账本是否越过评审预检线，越线则告警 + 标记建议冻结新开仓。

    只读：只消费 ledgers 里的统计/成交流水，不触碰任何账户、不发任何委托。
    只写：outputs/auto_block.json（审计 + 幂等状态），不写配置。

    Args:
        ledgers: 账本指标列表。元素形如
            {"name": "main", "closed_rounds": 98, "win_rate": 0.2551}
            或 {"name": "main", "trades": [成交流水...]}（内部复用唯一统计入口）。
            第一个账本视为"主账本"，其状态写入 decision.state。
        settings: 全局 Settings（若未来建模阈值字段则优先于 YAML）。
        output_path: 审计文件路径（默认 outputs/auto_block.json）。
        config_path: 阈值 YAML 路径（默认 config/settings.yaml）。
        alerter: 现有 Alerter（复用其 send 通道；silent 影子可传 None）。
        notify: 无 Alerter 时的自定义告警回调 (subject, detail)。
        now: 注入当前时间（测试可复现）。

    Returns:
        BlockDecision：triggered=规则是否越线；notified=本次真正推送的账本；
        recommend_freeze=是否建议冻结新开仓（仅标记，不执行）。
    """
    cfg = load_block_config(settings=settings, path=config_path)
    out_path = Path(output_path) if output_path is not None else DEFAULT_OUTPUT_PATH
    prev = _read_output(out_path)
    prev_notified = prev.get("last_notified_state")
    if not isinstance(prev_notified, dict):
        prev_notified = {}

    entries = [e for e in (ledgers or []) if isinstance(e, dict)]
    metrics = [_ledger_metrics(e) for e in entries]

    checked_at = (now or _dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    notified: list[str] = []
    duplicates: list[str] = []
    new_notified: dict = {}

    for item in metrics:
        state = _classify(item, cfg)
        item["state"] = state
        item["recommend_freeze"] = state == STATE_BLOCKED
        if state == STATE_BLOCKED:
            if str(prev_notified.get(item["name"], "")) == STATE_BLOCKED:
                # 幂等：同账本同状态已告警过 → 只记录，不重复推送
                duplicates.append(item["name"])
            else:
                subject, detail = _format_alert(item, cfg)
                _notify(alerter, notify, subject, detail)
                notified.append(item["name"])
            new_notified[item["name"]] = STATE_BLOCKED
        # 非 blocked 状态不写 last_notified_state → 回到合规即解除，再次越线会重新告警

    blocked = [m for m in metrics if m["state"] == STATE_BLOCKED]
    triggered = bool(blocked)
    primary_state = metrics[0]["state"] if metrics else STATE_NO_ROUNDS

    triggered_at = checked_at if triggered else prev.get("triggered_at")
    payload = {
        "triggered": triggered,
        "state": primary_state,
        "recommend_freeze": triggered,
        "ledgers": metrics,
        "checked_at": checked_at,
        "triggered_at": triggered_at,   # 最近一次越线时间（从未越线 → None）
        "config": cfg.as_dict(),
        "last_notified_state": new_notified,
        "policy": dict(POLICY),
        "note": "预检线：docs/USER_GUIDE.md §5.0（≥50 轮且胜率<45% → 停下检查）",
    }
    _write_output(out_path, payload)

    decision = BlockDecision(
        triggered=triggered,
        state=primary_state,
        recommend_freeze=triggered,
        ledgers=metrics,
        notified=notified,
        duplicates=duplicates,
        output_path=str(out_path),
        triggered_at=str(triggered_at) if triggered_at else None,
        config=cfg.as_dict(),
        last_notified_state=new_notified,
    )
    if triggered:
        logger.warning(
            f"[评审阻断] {', '.join(x['name'] for x in blocked)} 越过预检线；"
            f"建议冻结新开仓（人工决策），本次推送 {len(notified)} 条、"
            f"幂等跳过 {len(duplicates)} 条。")
    return decision

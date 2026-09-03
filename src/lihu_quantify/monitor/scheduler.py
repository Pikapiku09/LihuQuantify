"""APScheduler 定时巡检。

任务（docs/ARCHITECTURE.md §10.1）：
    daily_scan:   周一至周五 16:30（第四轮清单3：Tushare 当日日线 16:00 后完整）
    monthly_review: 月末 16:00

巡检流程（复用 run_live 逻辑）：
    扫描股票池 → 市场过滤 → 信号 → Checklist 闸门 → OMS（买入+止损同挂）
    → 止损监控 → 告警 → 报告归档
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from ..config import CapitalGuardConfig, Settings
from ..data.tushare_client import TushareClient
from ..data.duckdb_store import DuckDBStore
from ..indicators.standard import add_all_standard
from ..market import classify_market_state  # P2-9-4：包内引入，消除 sys.path hack
from ..strategy.cherry_claw import CherryClaw
from ..risk.checklist import ChecklistGate, CheckContext
from ..execution.paper_trade import PaperBroker
from ..execution.oms import OrderManagementSystem
from ..types import AccountSnapshot, Position, TradeRecord
from .ai_summary import build_ai_summary
from .alerts import Alerter
from .report import ReportGenerator

# _ROOT = 项目根目录：src/lihu_quantify/monitor/scheduler.py 向上 3 级
# 用于锚定 data/ 与 outputs/ 相对路径（测试常 monkeypatch 此变量做目录隔离）。
# P2-9-4：市场状态分类已由包内 lihu_quantify.market 引入，不再用 sys.path hack。
_ROOT = Path(__file__).resolve().parents[3]

# 第十轮需求1：settings.yaml 默认路径（与 run_scheduler.py / get_settings 一致）
_DEFAULT_SETTINGS_PATH = "config/settings.yaml"


def _json_safe(obj):
    """递归转 JSON 可序列化（date→str，第四轮清单6 last_scan 落盘用）。"""
    if isinstance(obj, (date,)):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _num_or_none(v):
    """问题3（第九轮）：bar 值 → float（NaN/缺失 → None，保证 JSON 可序列化）。"""
    try:
        f = float(v)
        return None if f != f else f   # NaN 自不等
    except (TypeError, ValueError):
        return None


# 止损原因 → 日报显示文案
_STOP_REASON_LABEL = {
    "price_stop": "价格止损",
    "ma_break": "MA10 破位",
    "trailing_stop": "移动止盈",   # 修复4(第六轮)
}


def build_account_snapshot(broker) -> AccountSnapshot:
    """P0-5（第十一轮）：从 broker 构造完整 AccountSnapshot（单一事实来源）。

    此前 scheduler._snapshot / run_live.broker_snapshot 各自构造且缺
    trades / halted_until / psychology_alert → checklist 的交易频率、
    连亏停手、心理门禁三项恒不生效（8 项闸门实际只剩 5 项）。统一补全：
      - trades：PaperBroker.trades（dict 流水 → TradeRecord，日期用
        broker._as_date 解析，解析失败回退当日）
      - halted_until：账户级连亏停手到期日（按票停手由 scan 预筛负责）
      - psychology_alert：无数据来源 → None（checklist 走"未知"分支，
        不拦截但标注，不伪造为通过）
    """
    positions = [
        Position(
            ts_code=p.ts_code, volume=p.volume, cost=p.cost,
            current_price=p.market_value / p.volume if p.volume else 0,
        )
        for p in broker.query_positions()
    ]
    asset = broker.query_asset()
    trades: list[TradeRecord] = []
    for t in getattr(broker, "trades", None) or []:
        d = (broker._as_date(t.get("date"))
             if hasattr(broker, "_as_date") else t.get("date"))
        trades.append(TradeRecord(
            ts_code=t["ts_code"],
            trade_date=d if isinstance(d, date) else date.today(),
            side=t["side"], price=t["price"], volume=t["volume"],
            pnl=t.get("pnl", 0) or 0, reason=t.get("reason", ""),
            commission=t.get("commission", 0) or 0,
            stamp_tax=t.get("stamp_tax", 0) or 0,
        ))
    return AccountSnapshot(
        total_asset=asset.get("total_asset", 0),
        cash=asset.get("cash", 0),
        positions=positions,
        trades=trades,
        halted_until=getattr(broker, "halted_until", None),
        psychology_alert=None,
    )


def _signal_score(bar) -> float:
    """需求1（第八轮）：信号质量代理评分（0~1，仅用于资金紧张时的 top-N 排序）。

    透明公式（非策略参数，不参与闸门判定与回测）：
        40% 量比强度（vol_ratio 截断 3.0）
        30% 实体占比（body_ratio）
        30% 金叉新鲜度（ma5_x_ma10，越新鲜得分越高，7 天为 0）
    依据 CherryClaw 三层过滤的三个连续度量做等权代理，无隐藏拟合。
    """
    try:
        vol_ratio = float(bar.get("vol_ratio") or 0)
        body = float(bar.get("body_ratio") or 0)
        # fresh=0（金叉当日）合法值，不能用 or 回退（0 为 falsy）
        raw_fresh = bar.get("ma5_x_ma10")
        fresh = float(raw_fresh) if raw_fresh is not None else 7.0
    except (TypeError, ValueError):
        return 0.0
    return (0.4 * min(max(vol_ratio, 0.0), 3.0) / 3.0
            + 0.3 * min(max(body, 0.0), 1.0)
            + 0.3 * (1 - min(max(fresh, 0.0), 7.0) / 7.0))


def _build_daily_summary(
    broker,
    latest: date,
    market_state: str,
    signals: int,
    entry_scale: float,
    executed: list[dict],
    rejected: list[dict],
    executed_stops: list[dict],
    pending_stops: list[dict],
    positions,
    stop_registry: dict,
    name_map: dict[str, str],
    prev_total_asset: Optional[float],
    report_path: str,
    mode: str,
    alerts: list[dict],
) -> dict:
    """第五轮：日报数据聚合（summary 同时持久化到 last_scan.json）。

    在巡检主体完成后调用：资产/持仓取收盘后最新状态，当日卖出从成交流水
    过滤（止损执行发生在 on_new_day 之后，date 口径可靠），
    每笔卖出的已实现盈亏扣双边费用（口径同 PaperBroker._on_sell_halt_check）。
    """
    from .daily_report import realized_pnl_for_sell

    # 资产（收盘后最新：含今日买卖与费用）
    asset = broker.query_asset() if hasattr(broker, "query_asset") else {}
    total_asset = asset.get("total_asset", 0)
    cash = asset.get("cash", 0)
    init_cap = getattr(broker, "init_capital", 0) or total_asset

    # 持仓明细（现价/浮动盈亏/止损线/占比）
    positions_detail = []
    for p in positions or []:
        vol = p.volume + p.frozen
        price = broker.get_price(p.ts_code) if hasattr(broker, "get_price") else 0.0
        if not price and vol:
            price = p.market_value / vol
        mv = vol * price
        cost_val = p.cost * vol
        float_pnl = mv - cost_val
        stop = (stop_registry or {}).get(p.ts_code)
        positions_detail.append({
            "ts_code": p.ts_code,
            "name": (name_map or {}).get(p.ts_code, ""),
            "volume": vol, "cost": p.cost, "price": price,
            "market_value": mv,
            "stop_price": stop.stop_price if stop else None,
            "float_pnl": float_pnl,
            "float_pnl_pct": float_pnl / cost_val if cost_val > 0 else 0.0,
            "weight": mv / total_asset if total_asset > 0 else 0.0,
        })

    # 当日卖出（含止损执行）→ 已实现盈亏
    day_str = str(latest)[:10]
    exec_reason = {
        it["ts_code"]: _STOP_REASON_LABEL.get(it.get("reason"), "止损离场")
        for it in (executed_stops or [])
    }
    sells_today = []
    for t in getattr(broker, "trades", []) or []:
        if t.get("side") == "sell" and str(t.get("date", ""))[:10] == day_str:
            sells_today.append({
                "ts_code": t["ts_code"],
                "name": (name_map or {}).get(t["ts_code"], ""),
                "price": t["price"], "volume": t["volume"],
                "pnl": realized_pnl_for_sell(broker.trades, t),
                "reason": exec_reason.get(t["ts_code"], "卖出离场"),
            })

    # 待执行止损（今日收盘登记，次日开盘执行）
    pending_detail = [
        {
            "ts_code": it["ts_code"],
            "name": (name_map or {}).get(it["ts_code"], ""),
            "volume": it.get("volume", 0),
            "stop_price": it.get("stop_price", 0),
            "reason": _STOP_REASON_LABEL.get(it.get("reason"), it.get("reason", "-")),
        }
        for it in (pending_stops or [])
    ]

    # 连亏停手（仅未到期的）
    halted = {}
    if hasattr(broker, "halted_codes"):
        today = latest if isinstance(latest, date) else date.today()
        for code, until in broker.halted_codes().items():
            until_d = until
            if isinstance(until, str):
                try:
                    until_d = date.fromisoformat(until[:10])
                except ValueError:
                    until_d = today
            if today < until_d:
                halted[code] = str(until_d)

    # 评审进度（100 笔验收；复用月度复盘同款 FIFO 配对口径）
    from .review_progress import review_stats as _review_stats
    summary_review = _review_stats(getattr(broker, "trades", []) or [])

    # 需求5（第十轮）：规则版简报（确定性兜底——AI 未配置/失败时永不为空）。
    # summary["brief"] = ai_summary or brief_rule（_scan_impl 中赋值）。
    _scale = entry_scale if entry_scale is not None else 1.0
    _filter = ("block（禁止开新仓）" if _scale == 0.0
               else "reduce（新仓减半）" if _scale == 0.5 else "off（正常开仓）")
    _cum = (total_asset / init_cap - 1) if init_cap else None
    _day = (total_asset - prev_total_asset) \
        if (prev_total_asset is not None and prev_total_asset > 0) else None
    _cum_s = f"{_cum:+.2%}" if _cum is not None else "-"
    _day_s = f"{_day:+,.0f}" if _day is not None else "-"
    brief_rule = (
        f"今日巡检完成：{signals} 信号，{len(executed)} 成交，{len(rejected)} 拦截；"
        f"市场{market_state}（{_filter}）；"
        f"总资产 {total_asset:,.0f}，"
        f"累计 {_cum_s}，"
        f"今日 {_day_s}；"
        f"持仓 {len(positions_detail)} 只。"
    )

    return {
        "trade_date": latest,
        "market_state": market_state,
        "signals": signals,
        "entry_scale": entry_scale,
        "executed": executed,
        "rejected": rejected,
        "sells_today": sells_today,
        "pending_stops": pending_detail,
        "halted_codes": halted,
        "positions": positions_detail,
        "total_asset": total_asset,
        "cash": cash,
        "init_capital": init_cap,
        "prev_total_asset": prev_total_asset,
        "alerts": alerts,
        "report": report_path,
        "mode": mode,
        "brief_rule": brief_rule,
        "review": summary_review,
    }


class DailyScanner:
    """每日巡检（可独立调用，也可被 Scheduler 调度）。"""

    def __init__(
        self,
        settings: Settings,
        broker=None,
        alerter: Optional[Alerter] = None,
        reporter: Optional[ReportGenerator] = None,
        mode: str = "paper",
    ):
        """
        Args:
            settings: 全局配置
            broker: 券商接口（None=按 mode 建 PaperBroker）
            alerter: 告警器（None=新建，key 取 settings.alert）
            reporter: 报告生成器（None=新建 outputs/reports/）
            mode: paper / live
        """
        self.settings = settings
        self.mode = mode
        # 第十轮需求1：配置热生效——scan() 每次巡检前从此路径重载 settings.yaml
        self._settings_path = _DEFAULT_SETTINGS_PATH
        # 第四轮清单1：邮件通道（enabled+配置齐全才生效；否则 None=不启用）
        # 第十轮需求1：授权码解析顺序 = data/secrets.json → env/yaml（设置界面可写）
        from .alerts import build_email_alerter

        _alert = getattr(settings, "alert", None)
        _email_cfg = getattr(_alert, "email", None) if _alert else None
        if _email_cfg is not None:
            try:
                _email_cfg = _email_cfg.model_copy(
                    update={"auth_code": settings.resolved_email_auth_code()})
            except Exception:
                pass
        _email = build_email_alerter(_email_cfg)
        self.alerter = alerter or Alerter(
            serverchan_key=getattr(getattr(settings, "alert", None), "serverchan_key", ""),
            email=_email,
        )
        # 第四轮清单2：缺席心跳（healthchecks.io；url 空=全部 no-op）
        from .heartbeat import Heartbeat

        self.heartbeat = Heartbeat(
            getattr(getattr(settings, "heartbeat", None), "healthchecks_url", "")
        )
        self.reporter = reporter or ReportGenerator(_ROOT / "outputs" / "reports")
        # 数据通道（独立于交易通道）
        self.client = TushareClient(
            token=settings.resolved_tushare_token(),
            cache_dir=settings.resolved_cache_dir(),
            cache_mtime_ttl=getattr(settings.tushare, "cache_ttl_seconds", 43200),
            rate_limit_interval=getattr(settings.tushare, "rate_limit_interval", 0.3),
        )
        self.store = DuckDBStore(settings.resolved_duckdb_path())
        self.broker = broker or PaperBroker(
            init_capital=settings.init_capital, tushare_client=self.client
        )

    # ===== 数据准备 =====

    def _universe(self, n: int) -> tuple[list[str], dict[str, str], dict[str, str]]:
        """返回 (股票池, sector_by_code, name_by_code)。修复C+H.4+J。

        修复J（三处同步换池）：pool_mode=strat 用成交额分层抽样池
        （head-50 已证实池子偏差：同参数旧池 +59.8% vs 分层池 +244.8%）。
        分层池构建时已内置流动性过滤（日均额≥1亿），无需逐股再查。
        修复C：industry → sector_by_code（空/NaN 用"未分类"，不因未知放行）
        第五轮：name_by_code → 日报邮件显示股票名称
        """
        u = self.settings.universe
        basic = self.client.query("stock_basic", {"list_status": "L"})
        if basic.empty:
            return [], {}, {}
        self.store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
        dfb = basic.copy()

        # ---- 修复J：池模式 ----
        if getattr(u, "pool_mode", "head") == "strat":
            from ..data.pool import build_stratified_pool

            codes = build_stratified_pool(
                self.client, self.store,
                target_n=getattr(u, "pool_size", 200) or n,
                layers=getattr(u, "pool_layers", 5),
                seed=getattr(u, "pool_seed", 42),
                min_list_days=u.min_list_days,
                min_avg_amount=u.min_avg_amount_20d,
            )
        else:
            # head 模式（旧，对照用）：前缀/ST 过滤 + 上市天数 + 逐股流动性
            mask = (
                ~dfb["ts_code"].str.startswith("688")
                & ~dfb["ts_code"].str.startswith("300")
                & ~dfb["ts_code"].str.startswith("301")
            )
            if "name" in dfb.columns:
                mask &= ~dfb["name"].str.contains("ST", na=False)
            if "list_date" in dfb.columns:
                list_dates = pd.to_datetime(dfb["list_date"], errors="coerce").dt.date
                cutoff = self._latest_trade_date() - timedelta(days=u.min_list_days)
                mask &= list_dates.apply(lambda d: bool(d) and d <= cutoff)
            candidates = dfb[mask].sort_values("ts_code")["ts_code"].tolist()
            codes = []
            for code in candidates:
                if len(codes) >= n:
                    break
                if self._passes_liquidity(code):
                    codes.append(code)

        # ---- 修复C：板块映射（全量构建）+ 第五轮：名称映射 ----
        sector_map: dict[str, str] = {}
        name_map: dict[str, str] = {}
        for _, row in dfb.iterrows():
            if "industry" in dfb.columns:
                ind = row.get("industry")
                ind = str(ind).strip() if pd.notna(ind) and str(ind).strip() else "未分类"
                sector_map[row["ts_code"]] = ind
            if "name" in dfb.columns:
                nm = row.get("name")
                name_map[row["ts_code"]] = str(nm).strip() if pd.notna(nm) else ""
        return codes, sector_map, name_map

    def _latest_trade_date(self) -> date:
        """P2-9-5：最新交易日（交易所日历锚定），取不到时回退系统“今天”。"""
        try:
            latest = self.store.get_latest_trade_date()
            if isinstance(latest, date):
                return latest
        except Exception:
            pass
        return date.today()

    def _passes_liquidity(self, code: str) -> bool:
        """修复H.4：近 20 日日均成交额 ≥ 阈值（缓存命中时开销极小）。"""
        min_amount = self.settings.universe.min_avg_amount_20d / 1e3   # 元 → 千元
        try:
            latest = self._latest_trade_date()
            start = latest - timedelta(days=45)
            df = self.client.query("daily", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": latest.strftime("%Y%m%d"),
            }, use_cache=True)
            if df.empty or len(df) < 5:
                return False
            avg_amount = df["amount"].tail(20).mean()
            return bool(avg_amount >= min_amount)
        except Exception:
            return False

    def _market_state(self) -> tuple[date, str]:
        """取真实最新交易日 + 市场状态（交易日锚定铁律）。

        第四轮清单3 补充修复：index_daily 固定参数 + 永久文件缓存 → 16:00 后
        仍命中 16:00 前的旧缓存（end_date=20301231 的 key 永不变），最新交易日
        卡在昨日。此处必须直连（use_cache=False），每日 1 次符合限频约束。
        """
        idx = self.client.query(
            "index_daily", {"ts_code": "000001.SH", "end_date": "20301231"},
            use_cache=False,
        )
        self.store.upsert("index_daily", idx)
        latest = self.store.get_latest_trade_date()
        idx_df = idx.copy()
        idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
        idx_df = idx_df.sort_values("trade_date").reset_index(drop=True)
        states = classify_market_state(idx_df)
        return latest, states.get(latest, "未知")

    # ===== 巡检主流程 =====

    def scan(self, n: int = 50, days: int = 120, force: bool = False) -> dict:
        """执行一次完整巡检。返回摘要 dict。

        幂等保护（第四轮清单6）：当日已巡检 → 直接返回上次摘要（防 cron+手动
        重复执行导致重复报告/重复止损登记）。force=True 强制重跑（人工补跑用）。
        心跳（第四轮清单2）：开始 /start，成功 ping，异常 /fail
        （healthchecks.io 侧"每日 17:30 前未收到成功 ping → 告警"实现缺席通知）。
        """
        self._reload_settings()   # 第十轮需求1：配置热生效（设置页保存 → 本次巡检即用）
        s, r = self.settings.strategy, self.settings.risk
        latest, market_state = self._market_state()
        last = self._read_last_scan()
        if not force:
            if last and str(last.get("trade_date")) == str(latest):
                logger.info(f"[幂等] {latest} 已巡检过（{last.get('finished_at', '?')}），跳过。"
                            f"如需重跑：--force")
                return last.get("summary") or {
                    "trade_date": latest, "market_state": market_state,
                    "signals": 0, "executed": [], "rejected": [], "report": "",
                    "total_asset": 0,
                }
        # 第五轮：上次巡检总资产（收盘价口径一致）→ 今日盈亏 = 本次 - 上次。
        # 仅取"上一交易日"的快照（同日 force 重跑时口径不乱）
        prev_total_asset = None
        if last and str(last.get("trade_date")) != str(latest):
            prev_total_asset = (last.get("summary") or {}).get("total_asset")
        logger.info(f"[巡检] 基准日 {latest}，市场状态 {market_state}")
        self.heartbeat.start()
        try:
            summary = self._scan_impl(
                latest, market_state, n, days, s, r,
                prev_total_asset=prev_total_asset,
            )
        except Exception as e:
            self.heartbeat.fail()
            logger.exception(f"[巡检异常] {latest}: {e}")
            if self.alerter.email is not None:
                self.alerter.email.send(
                    f"🚨 LihuQuantify 巡检异常 {latest}",
                    f"{type(e).__name__}: {e}\n\n详见容器/进程日志。",
                )
            raise
        # 幂等写入 + 心跳成功 + 每日摘要邮件
        self._write_last_scan(latest, summary)
        self.heartbeat.success()
        self._send_digest(summary)
        return summary

    def collect_signals(self, n: int = 50, days: int = 120) -> dict:
        """P2-9-6：抽取"股票池 + 信号扫描"供 run_live 复用（替代其重复 scan_universe）。

        DailyScanner 是股票池/参数/信号生成的单一实现；run_live 只在交互式
        打印流程里需要信号与市场状态，无需执行巡检完整副作用（落盘/止损/cron），
        故抽出本方法，统一 CherryClaw 参数构造（修掉 run_live 裸构造分叉）。

        Returns:
            {"latest": date, "market_state": str, "codes": [...],
             "signals": [(sig, last_ind), ...]}   # last_ind 为含指标的末行
        """
        latest, market_state = self._market_state()
        s, r = self.settings.strategy, self.settings.risk
        strategy = CherryClaw(
            ma_periods=tuple(s.ma_periods),
            golden_cross_max_freshness=s.golden_cross_max_freshness_days,
            volume_ratio_threshold=s.volume_ratio_threshold,
            entity_ratio_threshold=s.entity_ratio_threshold,
            close_to_ma5_max_dev=s.close_to_ma5_max_dev,
            max_position_pct=r.max_single_position,
            stop_loss_force_pct=r.stop_loss_force,
        )
        codes, _, _ = self._universe(n)
        start = latest - timedelta(days=days)
        signals: list[tuple] = []
        for code in codes:
            try:
                df = self.client.query("daily", {
                    "ts_code": code,
                    "start_date": start.strftime("%Y%m%d"),
                    "end_date": latest.strftime("%Y%m%d"),
                })
            except Exception as e:
                logger.warning(f"{code} 拉取失败: {e}")
                continue
            if df.empty or len(df) < 30:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            sig = strategy.latest_signal(df.sort_values("trade_date").reset_index(drop=True))
            if sig is not None:
                df_ind = add_all_standard(df.sort_values("trade_date").reset_index(drop=True))
                signals.append((sig, df_ind.iloc[-1]))
        return {"latest": latest, "market_state": market_state, "codes": codes, "signals": signals}

    def _scan_impl(
        self, latest: date, market_state: str, n: int, days: int, s, r,
        prev_total_asset: Optional[float] = None,
    ) -> dict:
        """巡检主体（由 scan() 包装：幂等/心跳/日报邮件在包装层）。

        第五轮：summary 扩展字段（持仓明细/当日卖出盈亏/待执行止损/股票名称/
        prev_total_asset 等），供每日综合日报邮件渲染（monitor/daily_report.py）。
        """
        logger.info(f"[巡检] 基准日 {latest}，市场状态 {market_state}")
        # 告警历史基线（常驻进程跨日累积，取当日增量）
        hist_start = len(self.alerter.history)

        # 修复2（第五轮清单）：日期切换（解冻 T+1 + trade_day=latest）必须在
        # 任何交易之前——否则当日买入/卖出被记到上一交易日，
        # _check_stops_with_alert 的 buys_today 守卫（当日新仓不做当日收盘
        # 止损判定）永远匹配不到当日买入，当日即被 MA10 破位登记（churn）。
        if hasattr(self.broker, "on_new_day"):
            self.broker.on_new_day(latest)

        # 崩溃恢复：无止损登记的持仓重建（默认成本-8%；修复B：文件已有
        # 原始止损价时优先保留，rebuild 只补缺失）
        oms = OrderManagementSystem(self.broker)
        positions_before = self.broker.query_positions()
        if positions_before:
            oms.rebuild_stops_from_positions()

        # 扫描信号（修复C：同时取板块映射）
        strategy = CherryClaw(
            ma_periods=tuple(s.ma_periods),
            golden_cross_max_freshness=s.golden_cross_max_freshness_days,
            volume_ratio_threshold=s.volume_ratio_threshold,
            entity_ratio_threshold=s.entity_ratio_threshold,
            close_to_ma5_max_dev=s.close_to_ma5_max_dev,
            max_position_pct=r.max_single_position,
            stop_loss_force_pct=r.stop_loss_force,
        )
        codes, sector_map, name_map = self._universe(n)
        start = latest - timedelta(days=days)
        signals: list[tuple] = []   # (signal, last_bar)
        hm_quotes: list[dict] = []   # 问题3（第九轮）：最新 bar → 热力图快照
        for code in codes:
            try:
                df = self.client.query("daily", {
                    "ts_code": code,
                    "start_date": start.strftime("%Y%m%d"),
                    "end_date": latest.strftime("%Y%m%d"),
                })
            except Exception as e:
                self.alerter.alert_api_error(f"daily:{code}", str(e))
                continue
            if df.empty:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            df = df.sort_values("trade_date").reset_index(drop=True)
            # 问题3（第九轮）：全池收集最新 bar（不足 30 根的票也进快照，
            # 热力图覆盖面 = 巡检池，而非仅有信号的票）
            last_q = df.iloc[-1]
            hm_quotes.append({
                "ts_code": code,
                "close": _num_or_none(last_q.get("close")),
                "pct_chg": _num_or_none(last_q.get("pct_chg")),
                "amount": _num_or_none(last_q.get("amount")),
                "trade_date": str(last_q.get("trade_date")),
            })
            if len(df) < 30:
                continue
            sig = strategy.latest_signal(df)
            if sig is not None:
                df_ind = add_all_standard(df)
                last_ind = df_ind.iloc[-1]
                # 需求1（第八轮）：信号评分（资金紧张时 top-N 排序用）
                signals.append((sig, last_ind, _signal_score(last_ind)))
        logger.info(f"[巡检] 扫描信号 {len(signals)} 个")

        # 问题3（第九轮）：热力图快照落盘（web 唯一数据源，切断 DuckDB 依赖）
        self._write_heatmap_snapshot(hm_quotes, name_map, sector_map)

        # 市场参考信号（修复A：reduce=非上涨段仓位减半；block=禁止）
        if market_state != "上涨" and s.market_filter:
            if s.market_filter_mode == "block":
                logger.info("[巡检] 市场过滤(block)：非上涨段，不开新仓")
            else:
                logger.info("[巡检] 市场参考(reduce)：非上涨段，仓位减半")

        executed, rejected = [], []
        gate = ChecklistGate(chasing_high_threshold=s.chasing_high_threshold)
        # ---- 需求3（第八轮）：先卖后买（口径修复） ----
        # 回测引擎日循环 = 撮合 T-1 卖单（T 开盘）→ T 收盘买入；
        # 纸面原顺序"买入→卖出"导致止损回笼资金闲置一天，先执行待执行止损。
        executed_stops = self._execute_pending_stops(latest)
        asset = self.broker.query_asset()
        total_asset = asset.get("total_asset", 0)
        cash = asset.get("cash", 0)

        block_mode = s.market_filter and market_state != "上涨" and s.market_filter_mode == "block"
        reduce_scale = 0.5 if (s.market_filter and market_state != "上涨"
                               and s.market_filter_mode == "reduce") else 1.0
        held = {p.ts_code for p in self.broker.query_positions()}
        # 需求1（第八轮）：资金守卫与 top-N 筛选配置
        # isinstance 守卫：MagicMock settings（测试）/异常配置 → 不启用
        cg = getattr(self.settings, "capital_guard", None)
        cg_enabled = isinstance(cg, CapitalGuardConfig) and cg.enabled
        budget = r.max_single_position * total_asset * reduce_scale   # 常规单笔投入
        guard_skipped = False
        topn_used, topn_skipped = 0, 0

        if not block_mode:
            # ---- 需求1：可用资金过低 → 本轮跳过全部买入尝试（汇总一条） ----
            if cg_enabled and cash < cg.min_cash_threshold:
                guard_skipped = True
                msg = (f"资金守卫：可用现金 {cash:,.0f} 低于阈值 "
                       f"{cg.min_cash_threshold:,.0f}，本轮跳过 {len(signals)} 个买入尝试")
                logger.info(f"[资金守卫] 现金 {cash:,.0f} < {cg.min_cash_threshold:,.0f}，"
                            f"跳过 {len(signals)} 个买入尝试")
                self.alerter.alert_checklist_reject("资金守卫", msg)
                rejected.append({"ts_code": "-", "name": "资金守卫",
                                 "price": 0, "reasons": msg})
            else:
                # 预筛（已持仓/停手期）——闸门与下单仍逐票实时执行（行为兼容）
                candidates: list[tuple] = []   # (score, idx, sig, last_bar)
                for idx, (sig, last_bar, score) in enumerate(signals):
                    if sig.ts_code in held:
                        continue   # 已持仓不重复买
                    # 修复F：连亏停手检查
                    if getattr(self.broker, "is_halted", None) and self.broker.is_halted(
                        sig.ts_code, today=latest
                    ):
                        rejected.append({"ts_code": sig.ts_code,
                                         "name": name_map.get(sig.ts_code, ""),
                                         "price": float(last_bar["close"]),
                                         "reasons": "铁律F：连亏3笔停手期内"})
                        self.alerter.alert_halt(sig.ts_code, "停手期内")
                        continue
                    candidates.append((score, idx, sig, last_bar))

                # ---- 需求1：资金紧张 → top-N 筛选（按评分只尝试前 N 个） ----
                slots = int(cash // budget) if budget > 0 else 0
                if (cg_enabled and cg.top_n_enabled and 0 < cg.top_n < len(candidates)
                        and len(candidates) > slots):
                    candidates.sort(key=lambda x: (-x[0], x[1]))   # 评分降序，同分按信号顺序
                    topn_skipped = len(candidates) - cg.top_n
                    candidates = candidates[:cg.top_n]
                    topn_used = len(candidates)
                    msg = (f"Top-N 筛选：过预筛 {topn_used + topn_skipped} 个 > "
                           f"可买 {slots} 槽（现金 {cash:,.0f}），按评分保留前 "
                           f"{topn_used} 个，{topn_skipped} 个未尝试")
                    logger.info(f"[Top-N] {msg}")
                    rejected.append({"ts_code": "-", "name": "Top-N 筛选",
                                     "price": 0, "reasons": msg})

                for score, idx, sig, last_bar in candidates:
                    # 修复E：执行价一律用最新交易日收盘
                    price = float(last_bar["close"])
                    invest = min(sig.suggested_position_pct, r.max_single_position) * total_asset * reduce_scale
                    volume = int(invest / price / 100) * 100
                    if volume < 100:
                        continue
                    snapshot = self._snapshot()
                    # 修复C：板块传入（同板块 ≤40% 校验生效）
                    check_ctx = CheckContext(
                        current_price=price,
                        ma10=float(last_bar.get("ma10", 0)) if pd.notna(last_bar.get("ma10")) else 0.0,
                        sector=sector_map.get(sig.ts_code, ""),
                        invest_amount=invest,
                    )
                    result = gate.check(sig, snapshot, check_ctx)
                    if not result.approved:
                        reasons = "、".join(
                            f"{i.name}({i.reason})" for i in result.rejected_items()
                        )
                        rejected.append({"ts_code": sig.ts_code,
                                         "name": name_map.get(sig.ts_code, ""),
                                         "price": price, "reasons": reasons})
                        self.alerter.alert_checklist_reject(sig.ts_code, reasons)
                        continue
                    buy_result, stop = oms.place_buy_with_stop(sig, volume, price)
                    if buy_result.success and stop:
                        executed.append({
                            "ts_code": sig.ts_code,
                            "name": name_map.get(sig.ts_code, ""),
                            "volume": volume, "price": price, "stop": stop.stop_price,
                        })
                        self.alerter.alert_bought(sig.ts_code, volume, price, stop.stop_price)
                    elif not buy_result.success:
                        rejected.append({"ts_code": sig.ts_code,
                                         "name": name_map.get(sig.ts_code, ""),
                                         "price": price, "reasons": buy_result.msg})
                        self.alerter.alert_checklist_reject(sig.ts_code, buy_result.msg)

        # 止损收盘判定（修复D：收盘判断→次日开盘执行，与回测口径对齐）
        # 需求3（第八轮）：执行已在买入前完成（先卖后买），此处只做新登记
        new_pending = self._register_new_stops(oms, latest)

        # 报告归档 + 日报数据聚合
        positions = self.broker.query_positions()
        # ---- 第五轮：日报数据聚合（先于报告生成——修复1/第六轮：.md 报告
        #      与邮件日报共用同一份 rich 数据，杜绝两处口径分叉） ----
        summary = _build_daily_summary(
            broker=self.broker,
            latest=latest,
            market_state=market_state,
            signals=len(signals),
            entry_scale=0.0 if block_mode else reduce_scale,
            executed=executed,
            rejected=rejected,
            executed_stops=executed_stops,
            pending_stops=new_pending,
            positions=positions,
            stop_registry=oms.stop_registry,
            name_map=name_map,
            prev_total_asset=prev_total_asset,
            report_path="",   # 报告生成后回填
            mode=self.mode,
            alerts=self.alerter.history[hist_start:],
        )
        # ---- 需求3（第八轮）：资金利用效率数据（.md 报告与邮件日报共用） ----
        summary["capital"] = {
            # 今日止损执行回笼金额（毛额，今日开盘价 × 股数）
            "released": round(sum(
                (st.get("open_price") or 0) * (st.get("volume") or 0)
                for st in executed_stops), 2),
            # 同轮再投资金额（先卖后买顺序修复后，当日回笼资金即可复用）
            "reinvested": round(sum(
                (e.get("price") or 0) * (e.get("volume") or 0)
                for e in executed), 2),
            "idle_cash": summary.get("cash", 0),          # 期末闲置现金
            "budget": round(budget, 2),                   # 常规单笔投入
            "idle_warn": bool(budget > 0 and summary.get("cash", 0) > 2 * budget),
            "guard_skipped": guard_skipped,               # 资金守卫触发（本轮未尝试买入）
            "topn_used": topn_used,                       # top-N 实际保留数（0=未启用）
            "topn_skipped": topn_skipped,                 # top-N 未尝试数
        }
        # ---- 第八轮清单：AI 收盘总结（纯展示层；巡检主体全部完成之后调用，
        #      不影响任何信号/成交/拦截结果。失败/未配置 → None 静默降级，
        #      结果随 last_scan.json 持久化） ----
        # 第十轮需求1：api_key 解析顺序 = data/secrets.json → 环境变量 → 空
        # （设置界面写 secrets.json，下次巡检即生效，不依赖 compose env）
        try:
            _ai_key = self.settings.resolved_ai_summary_api_key()
        except Exception:
            _ai_key = getattr(getattr(self.settings, "ai_summary", None), "api_key", "")
        summary["ai_summary"] = build_ai_summary(
            summary, getattr(self.settings, "ai_summary", None), _ai_key,
        )
        # 需求5（第十轮）：双层简报——AI 成功用 AI 版，失败/未配置自动回退规则版
        summary["brief"] = (summary.get("ai_summary")
                            or summary.pop("brief_rule", "") or "")
        stop_orders = [
            {"ts_code": st.ts_code, "stop_price": st.stop_price,
             "volume": st.volume, "triggered": st.triggered}
            for st in oms.stop_registry.values()
        ]
        halted_codes = getattr(self.broker, "halted_codes", lambda: {})()
        self.reporter._halted_codes = halted_codes or None
        report_path = self.reporter.daily_report(
            trade_date=latest,
            market_state=market_state,
            total_asset=summary["total_asset"],
            cash=summary["cash"],
            positions=[
                {"ts_code": p.ts_code, "volume": p.volume + p.frozen,
                 "cost": p.cost, "market_value": p.market_value}
                for p in positions
            ],
            signals=[{"ts_code": s_.ts_code, "price": s_.suggested_price, "reason": s_.reason}
                     for s_, _, _sc in signals],
            executed=executed,
            rejected=rejected,
            stop_orders=stop_orders,
            alerts=self.alerter.history,
            mode=self.mode,
            rich=summary,   # 修复1(第六轮)：rich 数据 → .md 报告五模块
        )
        summary["report"] = str(report_path)
        # 修复G(第三轮)：过滤命中统计（月度复盘读取）
        _append_filter_stats({
            "date": str(latest),
            "market_state": market_state,
            "signals": len(signals),
            "filter_mode": s.market_filter_mode if s.market_filter else "off",
            "entry_scale": 0.0 if block_mode else reduce_scale,
            "executed": len(executed),
        })
        return summary

    # ===== 工具 =====

    def _reload_settings(self) -> None:
        """需求1（第十轮）：配置热生效——每次巡检前重载 settings.yaml。

        背景：get_settings 为 lru_cache 单例 + 本对象启动时捕获 settings，
        改 yaml 对运行中的调度器不生效。此处清缓存重载，使设置页的修改在
        下一次巡检（16:30 cron 或手动 --run-now）生效，无需重启容器。

        守卫：__new__ 构造的测试 stub（无 _settings_path）与 MagicMock
        settings 不热载（同 capital_guard 的 isinstance 守卫约定）。
        通知通道就地更新属性而非换对象（保留告警历史与测试注入的 mock）。
        加载失败（yaml 语法错等）→ 沿用旧配置并告警（坏配置不杀死巡检）。
        """
        path = getattr(self, "_settings_path", None)
        if not path:
            return
        try:
            from ..config import Settings as _SettingsCls, get_settings

            if not isinstance(self.settings, _SettingsCls):
                return
            get_settings.cache_clear()
            fresh = get_settings(path)
            self.settings = fresh
            # 通知通道就地更新（不换对象）
            from .alerts import build_email_alerter

            _alert = getattr(fresh, "alert", None)
            if _alert is not None:
                self.alerter.serverchan_key = \
                    getattr(_alert, "serverchan_key", "") or ""
                _email_cfg = getattr(_alert, "email", None)
                if _email_cfg is not None:
                    try:
                        _email_cfg = _email_cfg.model_copy(
                            update={"auth_code": fresh.resolved_email_auth_code()})
                    except Exception:
                        pass
                self.alerter.email = build_email_alerter(_email_cfg)
            self.heartbeat.url = getattr(
                getattr(fresh, "heartbeat", None), "healthchecks_url", "") or ""
        except Exception as e:
            logger.warning(f"[配置热载] 失败，沿用旧配置: {e}")

    def _write_heatmap_snapshot(
        self, quotes: list[dict], name_map: dict, sector_map: dict
    ) -> None:
        """问题3（第九轮）：写 data/heatmap_snapshot.json（热力图唯一数据源）。

        根因：web 直读 DuckDB stock_basic 取名称/行业，而本进程常驻持有
        写锁 → web 只读连接必败 → 静默降级（名称回退代码、行业"未知"）。
        此处巡检时用 _universe 的 name_map/sector_map（scheduler 侧数据
        齐全，无需读库）生成 UTF-8 快照（ensure_ascii=False），编码链路
        单一可控，同时根治乱码问题（问题2）。
        schema：[{ts_code, name, industry, close, pct_chg, amount, trade_date}]
        """
        rows = [{
            "ts_code": q["ts_code"],
            "name": (name_map or {}).get(q["ts_code"]) or q["ts_code"],
            "industry": (sector_map or {}).get(q["ts_code"]) or "未知",
            "close": q["close"], "pct_chg": q["pct_chg"],
            "amount": q["amount"], "trade_date": q["trade_date"],
        } for q in quotes]
        # 只保留最新交易日（长期停牌等陈旧 bar 剔除，避免混合日期误导）
        if rows:
            max_td = max(r["trade_date"] for r in rows)
            rows = [r for r in rows if r["trade_date"] == max_td]
        rows.sort(key=lambda r: -(r.get("amount") or 0))
        path = _ROOT / "data" / "heatmap_snapshot.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            logger.info(f"[热力图] 快照已写入 {len(rows)} 只（{max_td if rows else '-'}）")
        except (OSError, TypeError) as e:
            logger.warning(f"[热力图] 快照写入失败: {e}")

    def _read_last_scan(self) -> Optional[dict]:
        """读 data/last_scan.json（幂等保护，第四轮清单6）。损坏/不存在 → None。"""
        path = _ROOT / "data" / "last_scan.json"
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[幂等] last_scan.json 读取失败（视为未巡检）: {e}")
        return None

    def _write_last_scan(self, trade_date, summary: dict) -> None:
        """写 data/last_scan.json（巡检成功后；第四轮清单6）。"""
        from datetime import datetime

        path = _ROOT / "data" / "last_scan.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "trade_date": str(trade_date),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "summary": _json_safe(summary),
                }, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except (OSError, TypeError) as e:
            logger.warning(f"[幂等] last_scan.json 写入失败: {e}")

    def _send_digest(self, summary: dict) -> None:
        """每日综合日报邮件（第五轮：每日一封，HTML 结构化）。

        开关：alert.email.send_daily_digest（沿用旧配置名，语义升级为综合日报）。
        """
        email_cfg = getattr(getattr(self.settings, "alert", None), "email", None)
        if not getattr(email_cfg, "send_daily_digest", False):
            return
        try:
            if self.alerter.send_daily_report(summary):
                logger.info("[邮件] 每日综合日报已发送")
        except Exception as e:
            logger.warning(f"[邮件] 每日综合日报发送失败: {e}")

    def _snapshot(self) -> AccountSnapshot:
        """P0-5（第十一轮）：委托 build_account_snapshot（补全 trades/
        halted_until/psychology_alert，恢复 checklist 8 项闸门）。"""
        return build_account_snapshot(self.broker)

    def _check_stops_with_alert(
        self, oms: OrderManagementSystem, latest: date
    ) -> tuple[list[dict], list[dict]]:
        """止损检查（修复D：与回测口径对齐）。第八轮拆分为两个子步骤：

            _execute_pending_stops  执行昨日登记（今日开盘价卖出）
            _register_new_stops     今日收盘判定（价格/MA10/移动止盈）

        保留本方法作为兼容包装（第六轮测试及旧调用）。
        """
        executed_stops = self._execute_pending_stops(latest)
        new_pending = self._register_new_stops(oms, latest)
        return executed_stops, new_pending

    def _execute_pending_stops(self, latest: date) -> list[dict]:
        """需求3（第八轮）：执行昨日登记的待执行止损（用今日开盘价）。

        顺序修复（先卖后买）：回测引擎日循环为"撮合 T-1 卖单（T 开盘成交）
        → T 收盘买入"，纸面原顺序"买入 → 卖出"导致当日止损回笼资金
        无法用于同轮买入（2026-08-27 曾因"可用 4429"拒绝 44 个信号、
        48k 资金闲置一天）。本方法移至买入循环之前调用，与回测口径对齐。

        P0-3（第十一轮）：失败单不再被静默清空——卖单成功才从 pending
        移除；失败/无开盘价 → 保留并累加 failed_count，连续 ≥3 次升级
        ERROR 告警（触发即时邮件通道），防止持仓失去止损保护。
        """
        pending_file = _ROOT / "data" / "pending_stops.json"
        executed_stops: list[dict] = []   # 今日开盘执行的止损
        remaining: list[dict] = []        # P0-3：未成交保留（失败/无开盘价）

        if pending_file.exists():
            try:
                pending = json.loads(pending_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pending = []
            for item in pending:
                code = item["ts_code"]
                volume = item["volume"]
                # 取今日开盘价
                open_price = self._fetch_open(code, latest)
                if not open_price or open_price <= 0:
                    # P0-3：无开盘价（停牌/数据缺失）→ 保留，不丢弃
                    item["failed_count"] = item.get("failed_count", 0) + 1
                    remaining.append(item)
                    logger.warning(f"[止损保留] {code} 无开盘价"
                                   f"（failed_count={item['failed_count']}），待下次执行")
                    continue
                # 修复4(第六轮)：原因（中文标签）传给交易记录
                reason_label = _STOP_REASON_LABEL.get(
                    item.get("reason", "price_stop"), "止损离场"
                )
                result = self.broker.sell(
                    code, round(open_price - 0.01, 2), volume,
                    reason=reason_label,
                )
                if result.success:
                    self.alerter.alert_stop_loss(code, open_price, item["stop_price"])
                    logger.info(f"[止损执行] {code} 次日开盘 {open_price:.2f} 成交"
                                f"（{reason_label}）")
                    executed_stops.append({
                        "ts_code": code, "volume": volume,
                        "stop_price": item.get("stop_price", 0),
                        "open_price": open_price,
                        "reason": item.get("reason", "price_stop"),
                    })
                else:
                    # P0-3：失败保留 + 计数；≥3 次升级 ERROR（即时邮件）
                    item["failed_count"] = item.get("failed_count", 0) + 1
                    remaining.append(item)
                    if item["failed_count"] >= 3:
                        logger.error(
                            f"[止损连续失败] {code}: {result.msg}"
                            f"（第 {item['failed_count']} 次），持仓失去止损保护")
                        self.alerter.send(
                            f"止损单连续 {item['failed_count']} 次执行失败：{code}",
                            f"失败原因：{result.msg}。该持仓已连续 "
                            f"{item['failed_count']} 个交易日无法止损离场，"
                            f"请人工核查（持仓状态/资金/行情数据）。",
                            level="error",
                        )
                    else:
                        logger.error(f"[止损执行失败] {code}: {result.msg}"
                                     f"（failed_count={item['failed_count']}，保留待执行）")
            # P0-3：只写剩余未成交单（成功单已移除；remaining 空 → "[]"）
            try:
                pending_file.write_text(
                    json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return executed_stops

    def _register_new_stops(
        self, oms: OrderManagementSystem, latest: date
    ) -> list[dict]:
        """今日收盘判断（价格止损 + MA10 破位 + 移动止盈）→ 登记明日待执行。

        口径对齐（live 补充）：当日新买入的持仓不做当日收盘判定。
        回测中 T+1 开盘成交的仓位首次止损判定在 T+1 收盘（信号日次一 bar）；
        模拟盘 T 收盘成交 → 首次判定同样在 T+1 收盘。
        （否则策略允许的"收盘乖离 MA5 -5%"入场票会被信号日 MA10 在上方
          而即时标记卖出，产生回测中不存在的当日 churn。）

        修复4(第六轮)：移动止盈——先更新高水位，再按
        trailing_profit_pullback(默认3%) 判定（高水位×0.97，与
        risk/stop_loss.py 第七轮修正后口径一致）。
        """
        pending_file = _ROOT / "data" / "pending_stops.json"
        buys_today = set()
        for t in getattr(self.broker, "trades", []) or []:
            if t.get("side") == "buy" and str(t.get("date", ""))[:10] == str(latest)[:10]:
                buys_today.add(t.get("ts_code"))

        # 修复4(第六轮)：移动止盈回撤阈值（settings.risk.trailing_profit_pullback，
        # 异常/不可得时回退 3%，与 stop_loss.py 默认一致）
        pullback = getattr(getattr(self.settings, "risk", None),
                           "trailing_profit_pullback", 0.03)
        try:
            pullback = float(pullback)
        except (TypeError, ValueError):
            pullback = 0.03
        if not 0 < pullback < 1:
            pullback = 0.03

        broker_positions = getattr(self.broker, "positions", None)
        new_pending = []
        for code, stop in oms.stop_registry.items():
            if stop.triggered or code in buys_today:
                continue
            # 修复4(第六轮)：已清仓的登记跳过（止损执行后持仓不存在，防误登记；
            # 仅对 dict 型 positions 生效——PaperBroker；实盘 broker 无该属性则跳过守卫）
            if isinstance(broker_positions, dict) and code not in broker_positions:
                continue
            # 价格止损：收盘价 ≤ 止损线
            close = self.broker.get_price(code)
            # MA10 破位（修复D）：取最新 MA10
            ma10 = self._fetch_ma10(code, latest)
            # 修复4(第六轮)：更新高水位（移动止盈基准；当日新仓买入价已起算）
            # P2-9-8：循环内 save=False（避免每票触新高就全量落盘+全持仓取价），
            # 循环结束后统一落盘一次。
            if hasattr(self.broker, "update_high_water") and close > 0:
                self.broker.update_high_water(code, close, save=False)
            if close > 0 and close <= stop.stop_price:
                new_pending.append({
                    "ts_code": code, "volume": stop.volume,
                    "stop_price": stop.stop_price, "reason": "price_stop",
                })
                logger.warning(f"[止损登记] {code} 收盘 {close:.2f} ≤ 止损线 {stop.stop_price:.2f}，"
                               f"待次日开盘执行")
            elif ma10 and close > 0 and close < ma10:
                new_pending.append({
                    "ts_code": code, "volume": stop.volume,
                    "stop_price": ma10, "reason": "ma_break",
                })
                logger.warning(f"[MA10破位登记] {code} 收盘 {close:.2f} < MA10 {ma10:.2f}，"
                               f"待次日开盘执行")
            elif hasattr(self.broker, "high_water_mark"):
                # 修复4(第六轮)：移动止盈——浮盈后从高水位回撤 pullback 离场
                hwm = self.broker.high_water_mark.get(code, 0.0)
                pos = broker_positions.get(code) if isinstance(broker_positions, dict) else None
                cost = pos.cost if pos is not None else 0.0
                trail_price = hwm * (1 - pullback)
                if hwm > cost > 0 and close > 0 and close <= trail_price:
                    new_pending.append({
                        "ts_code": code, "volume": stop.volume,
                        "stop_price": round(trail_price, 2), "reason": "trailing_stop",
                    })
                    logger.warning(
                        f"[移动止盈登记] {code} 高水位 {hwm:.2f} 回撤 {pullback:.0%} → "
                        f"收盘 {close:.2f} ≤ {trail_price:.2f}，待次日开盘执行"
                    )
        if new_pending:
            try:
                pending_file.parent.mkdir(parents=True, exist_ok=True)
                pending_file.write_text(
                    json.dumps(new_pending, ensure_ascii=False), encoding="utf-8"
                )
            except OSError as e:
                logger.warning(f"待执行止损写入失败: {e}")
        # P2-9-8：高水位统一落盘（循环内已 save=False）
        if getattr(self.broker, "_save_state", None) is not None:
            try:
                self.broker._save_state()
            except Exception as e:
                logger.warning(f"[高水位落盘] 失败: {e}")
        return new_pending

    def _fetch_open(self, ts_code: str, trade_date: date) -> float:
        """取指定日开盘价（Tushare daily）。"""
        try:
            df = self.client.query("daily", {
                "ts_code": ts_code,
                "start_date": trade_date.strftime("%Y%m%d"),
                "end_date": trade_date.strftime("%Y%m%d"),
            }, use_cache=True)
            if not df.empty:
                return float(df["open"].iloc[0])
        except Exception as e:
            logger.warning(f"取 {ts_code} 开盘价失败: {e}")
        return 0.0

    def _fetch_ma10(self, ts_code: str, trade_date: date) -> float:
        """取指定日 MA10（用最近 10 根收盘均价，数据不足返回 0）。"""
        try:
            start = trade_date - timedelta(days=30)
            df = self.client.query("daily", {
                "ts_code": ts_code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": trade_date.strftime("%Y%m%d"),
            }, use_cache=True)
            if len(df) >= 10:
                return float(df["close"].tail(10).mean())
        except Exception as e:
            logger.warning(f"取 {ts_code} MA10 失败: {e}")
        return 0.0


def setup_scheduler(
    settings: Settings,
    mode: str = "paper",
    n: int = 50,
):
    """构建 APScheduler（返回 scheduler，由调用方 start）。"""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BlockingScheduler(timezone=settings.scheduler.timezone)
    scanner = DailyScanner(settings, mode=mode)

    def daily_scan_job():
        logger.info("=" * 50)
        logger.info("[定时任务] 每日巡检启动")
        try:
            summary = scanner.scan(n=n)
            logger.info(f"[定时任务] 巡检完成: {summary['signals']}信号/"
                        f"{len(summary['executed'])}执行/{len(summary['rejected'])}拦截，"
                        f"报告 {summary['report']}")
        except Exception as e:
            logger.exception(f"[定时任务] 巡检异常: {e}")

    def monthly_review_job():
        """修复H.2：月末生成月度复盘报告（填 docs/月度复盘模板.md 字段）。"""
        logger.info("[定时任务] 月度复盘生成")
        try:
            _monthly_review(scanner)
        except Exception as e:
            logger.exception(f"[定时任务] 月度复盘异常: {e}")

    # 每日 15:30（A 股收盘后 30 分钟）
    sched.add_job(
        daily_scan_job,
        CronTrigger.from_crontab(settings.scheduler.daily_scan_cron),
        id="daily_scan",
        name="每日收盘巡检",
        misfire_grace_time=3600,
    )
    # 月末复盘（修复H.2；APScheduler 不支持 cron "L"，用 28-31 日 + 月内末次触发近似）
    sched.add_job(
        monthly_review_job,
        CronTrigger(day="28-31", hour=16, minute=0),
        id="monthly_review",
        name="月度复盘",
        misfire_grace_time=7200,
    )
    return sched


def _is_last_trade_day_of_month(scanner: "DailyScanner", today: date) -> bool | None:
    """修复D(第三轮)：判断今日是否本月最后一个交易日。

    用 trade_cal 查询今日之后本月剩余交易日；无剩余 → 今日为月末交易日。
    返回 None 表示日历不可得（调用方走幂等回退）。
    """
    try:
        # 本月最后一天（日历边界）
        if today.month == 12:
            month_end = date(today.year, 12, 31)
        else:
            month_end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        cal = scanner.client.query("trade_cal", {
            "exchange": "SSE",
            "start_date": today.strftime("%Y%m%d"),
            "end_date": month_end.strftime("%Y%m%d"),
        }, use_cache=True)
        if cal is None or cal.empty or "is_open" not in cal.columns:
            return None
        today_str = today.strftime("%Y%m%d")
        rest = cal[(cal["cal_date"] > today_str) & (cal["is_open"] == 1)]
        return rest.empty
    except Exception as e:
        logger.warning(f"[月度复盘] trade_cal 查询失败（走幂等回退）: {e}")
        return None


def _append_filter_stats(record: dict) -> None:
    """修复G(第三轮)：累积每日过滤命中统计（月度复盘读取）。

    data/filter_stats.json = [{"date","market_state","signals","filter_mode",
                                "entry_scale","executed"}, ...]（按日期去重覆盖）
    P0-4（第十一轮）：json 顶部统一导入——原局部 `import json as _json` 与
    except 子句的 json.JSONDecodeError 不一致，损坏文件时 NameError 崩溃。
    """
    path = _ROOT / "data" / "filter_stats.json"
    records = []
    try:
        if path.exists():
            records = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        records = []
    # 同日覆盖
    records = [r for r in records if r.get("date") != record.get("date")]
    records.append(record)
    # 只保留最近 400 天
    records = sorted(records, key=lambda r: r.get("date", ""))[-400:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning(f"[过滤统计] 写入失败: {e}")


def _monthly_anchor(scanner: "DailyScanner") -> date:
    """P2-9-5：月度复盘日期锚定最新交易日；DB 不可得回退配置时区“今天”。"""
    try:
        latest = scanner.store.get_latest_trade_date()
        if isinstance(latest, date):
            return latest
    except Exception:
        pass
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(scanner.settings.scheduler.timezone)).date()
    except Exception:
        return date.today()


def _monthly_review(scanner: DailyScanner) -> None:
    """月度复盘（修复H.2）：从 PaperBroker 成交流水统计，输出 Markdown。

    修复D(第三轮)：仅当今日为本月最后交易日时生成（trade_cal 判定）；
    日历不可得时幂等回退（monthly_YYYYMM.md 已存在即跳过）。
    修复G(第三轮)：新增"过滤命中统计"栏目。
    """
    # P2-9-5：日期纪律——锚定最新交易日（交易所日历），非本机时钟。
    today = _monthly_anchor(scanner)
    out = _ROOT / "outputs" / "reports" / f"monthly_{today.strftime('%Y%m')}.md"

    # ---- 修复D：月末判定 ----
    is_last = _is_last_trade_day_of_month(scanner, today)
    if is_last is False:
        logger.info(f"[月度复盘] 今日({today})非本月最后交易日，跳过")
        return
    if is_last is None and out.exists():
        # 日历不可得 + 已生成 → 幂等跳过（防 28-31 日重复触发）
        logger.info(f"[月度复盘] 日历不可得且报告已存在，跳过（幂等）")
        return

    broker = scanner.broker
    trades = getattr(broker, "trades", [])
    asset = broker.query_asset()
    total_asset = asset.get("total_asset", 0)
    init_cap = getattr(broker, "init_capital", total_asset)

    # 按 买入-卖出 轮次统计胜率/盈亏比
    # （收敛到 review_progress.review_stats——与看板/日报/邮件评审进度同源同口径）
    from .review_progress import review_stats as _review_stats
    _rv = _review_stats(trades)
    rounds_count = _rv["closed_rounds"]
    win_rate = _rv["win_rate"] if _rv["win_rate"] is not None else 0
    pl_ratio = _rv["pl_ratio"] if _rv["pl_ratio"] is not None else 0

    total_fees = sum(t.get("commission", 0) + t.get("stamp_tax", 0) for t in trades)

    # ---- 修复G：过滤命中统计（本月） ----
    month_prefix = today.strftime("%Y-%m")
    filter_stats = []
    try:
        stats_path = _ROOT / "data" / "filter_stats.json"
        if stats_path.exists():
            all_stats = json.loads(stats_path.read_text(encoding="utf-8"))
            filter_stats = [s for s in all_stats if str(s.get("date", "")).startswith(month_prefix)]
    except (json.JSONDecodeError, OSError):
        filter_stats = []
    days_scanned = len(filter_stats)
    non_uptrend_days = sum(1 for s in filter_stats if s.get("market_state") != "上涨")
    reduced_days = sum(1 for s in filter_stats if s.get("entry_scale") == 0.5)
    blocked_days = sum(1 for s in filter_stats if s.get("entry_scale") == 0.0)
    signals_seen = sum(s.get("signals", 0) for s in filter_stats)
    executed_count = sum(s.get("executed", 0) for s in filter_stats)

    lines = [
        f"# 月度复盘 {today.strftime('%Y-%m')}",
        "",
        "## 一、账户体检",
        "",
        "| 项目 | 数值 |",
        "|---|---|",
        f"| 期末总资产 | {total_asset:,.0f} |",
        f"| 期间收益率 | {total_asset/init_cap - 1:.2%} |" if init_cap else "| 期间收益率 | N/A |",
        f"| 手续费合计 | {total_fees:.0f} |",
        "",
        "## 二、交易统计",
        "",
        f"- 交易笔数（买入+卖出）: {len(trades)}",
        f"- 胜率（按轮次）: {win_rate:.1%}（{rounds_count} 轮）",
        f"- 盈亏比: {pl_ratio:.2f}",
        "",
        "## 三、市场过滤命中统计（修复G）",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 巡检天数 | {days_scanned} |",
        f"| 非上涨段天数 | {non_uptrend_days} |",
        f"| 减半参与天数（reduce） | {reduced_days} |",
        f"| 完全拦截天数（block） | {blocked_days} |",
        f"| 期间信号总数 | {signals_seen} |",
        f"| 实际开仓次数 | {executed_count} |",
        "",
        "> 过滤证据等级：中等偏弱（holdout 16 笔样本，变体脆弱），当前采用 reduce 起步；",
        "> 积累 ≥100 笔纸面交易后再评估是否升级 block（见 docs/决策日志.md）。",
        "",
        "## 四、铁律自检",
        "",
        "- 止损执行率见每日报告告警记录",
        f"- 当前停手票: {list(broker.halted_codes().keys()) if hasattr(broker, 'halted_codes') else '无'}",
        "",
        "---",
        "以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"[月度复盘] 已生成 {out}")

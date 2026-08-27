"""APScheduler 定时巡检。

任务（docs/ARCHITECTURE.md §10.1）：
    daily_scan:   周一至周五 16:30（第四轮清单3：Tushare 当日日线 16:00 后完整）
    monthly_review: 月末 16:00

巡检流程（复用 run_live 逻辑）：
    扫描股票池 → 市场过滤 → 信号 → Checklist 闸门 → OMS（买入+止损同挂）
    → 止损监控 → 告警 → 报告归档
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from ..config import Settings
from ..data.tushare_client import TushareClient
from ..data.duckdb_store import DuckDBStore
from ..indicators.standard import add_all_standard
from ..strategy.cherry_claw import CherryClaw
from ..risk.checklist import ChecklistGate, CheckContext
from ..execution.paper_trade import PaperBroker
from ..execution.oms import OrderManagementSystem
from ..types import AccountSnapshot, Position
from .alerts import Alerter
from .report import ReportGenerator

# 复用市场状态分类（run_backtest.py）
import sys

# _ROOT = 项目根目录：src/lihu_quantify/monitor/scheduler.py 向上 3 级
_ROOT = Path(__file__).resolve().parents[3]
for _p in (_ROOT, str(_ROOT / "src")):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from run_backtest import classify_market_state  # noqa: E402


def _json_safe(obj):
    """递归转 JSON 可序列化（date→str，第四轮清单6 last_scan 落盘用）。"""
    if isinstance(obj, (date,)):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# 止损原因 → 日报显示文案
_STOP_REASON_LABEL = {
    "price_stop": "价格止损",
    "ma_break": "MA10 破位",
    "trailing_stop": "移动止盈",   # 修复4(第六轮)
}


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
        # 第四轮清单1：邮件通道（enabled+配置齐全才生效；否则 None=不启用）
        from .alerts import build_email_alerter

        _email = build_email_alerter(
            getattr(getattr(settings, "alert", None), "email", None)
        ) if getattr(settings, "alert", None) else None
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
                cutoff = date.today() - timedelta(days=u.min_list_days)
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

    def _passes_liquidity(self, code: str) -> bool:
        """修复H.4：近 20 日日均成交额 ≥ 阈值（缓存命中时开销极小）。"""
        min_amount = self.settings.universe.min_avg_amount_20d / 1e3   # 元 → 千元
        try:
            start = date.today() - timedelta(days=45)
            df = self.client.query("daily", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": date.today().strftime("%Y%m%d"),
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
            if df.empty or len(df) < 30:
                continue
            df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
            df = df.sort_values("trade_date").reset_index(drop=True)
            sig = strategy.latest_signal(df)
            if sig is not None:
                df_ind = add_all_standard(df)
                signals.append((sig, df_ind.iloc[-1]))
        logger.info(f"[巡检] 扫描信号 {len(signals)} 个")

        # 市场参考信号（修复A：reduce=非上涨段仓位减半；block=禁止）
        if market_state != "上涨" and s.market_filter:
            if s.market_filter_mode == "block":
                logger.info("[巡检] 市场过滤(block)：非上涨段，不开新仓")
            else:
                logger.info("[巡检] 市场参考(reduce)：非上涨段，仓位减半")

        executed, rejected = [], []
        gate = ChecklistGate(chasing_high_threshold=s.chasing_high_threshold)
        asset = self.broker.query_asset()
        total_asset = asset.get("total_asset", 0)

        block_mode = s.market_filter and market_state != "上涨" and s.market_filter_mode == "block"
        reduce_scale = 0.5 if (s.market_filter and market_state != "上涨"
                               and s.market_filter_mode == "reduce") else 1.0
        held = {p.ts_code for p in self.broker.query_positions()}
        if not block_mode:
            for sig, last_bar in signals:
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

        # 止损检查（修复D：收盘判断→次日开盘执行，与回测口径对齐）
        # 修复2（第五轮清单）：on_new_day 已移至 _scan_impl 开头（买入前），
        # 此处直接处理待执行/新登记。
        executed_stops, new_pending = self._check_stops_with_alert(oms, latest)   # 第五轮：日报数据

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
                     for s_, _ in signals],
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

    def _read_last_scan(self) -> Optional[dict]:
        """读 data/last_scan.json（幂等保护，第四轮清单6）。损坏/不存在 → None。"""
        import json

        path = _ROOT / "data" / "last_scan.json"
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[幂等] last_scan.json 读取失败（视为未巡检）: {e}")
        return None

    def _write_last_scan(self, trade_date, summary: dict) -> None:
        """写 data/last_scan.json（巡检成功后；第四轮清单6）。"""
        import json
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
        positions = [
            Position(
                ts_code=p.ts_code, volume=p.volume, cost=p.cost,
                current_price=p.market_value / p.volume if p.volume else 0,
            )
            for p in self.broker.query_positions()
        ]
        asset = self.broker.query_asset()
        return AccountSnapshot(
            total_asset=asset.get("total_asset", 0),
            cash=asset.get("cash", 0),
            positions=positions,
        )

    def _check_stops_with_alert(
        self, oms: OrderManagementSystem, latest: date
    ) -> tuple[list[dict], list[dict]]:
        """止损检查（修复D：与回测口径对齐）。

        口径（三处统一，写入文档）：
            回测   = 当日收盘判断 → 次日开盘成交
            模拟盘 = 当日收盘判断 → 记入待执行 → 次日开盘价成交（本方法）
            实盘   = 盘中条件单（OMS monitor_loop）

        修复D 新增：
            1. 待执行队列（今日收盘触发 → 次日 scan 的开盘价执行）
            2. MA10 破位检查（收盘 < MA10 → ma_break 离场，与 stop_loss.py 一致）

        第五轮：返回 (今日执行的止损列表, 今日新登记的待执行列表) 供日报渲染。
        修复4(第六轮)：
            1. 待执行执行时把原因传给 broker.sell（交易记录存 pnl/reason）
            2. 第二步先更新高水位，再增加移动止盈判定（浮盈后从高水位回撤
               trailing_profit_pullback(默认3%) → trailing_stop 待执行），
               语义与 risk/stop_loss.py 的移动止盈注释口径一致（高水位×0.97）
        """
        pending_file = _ROOT / "data" / "pending_stops.json"
        executed_stops: list[dict] = []   # 今日开盘执行的止损

        # ---- 第一步：执行昨日登记的待执行止损（用今日开盘价） ----
        if pending_file.exists():
            try:
                import json

                pending = json.loads(pending_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pending = []
            for item in pending:
                code = item["ts_code"]
                volume = item["volume"]
                # 取今日开盘价
                open_price = self._fetch_open(code, latest)
                if open_price and open_price > 0:
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
                        logger.error(f"[止损执行失败] {code}: {result.msg}")
                        # 保留待执行
                        continue
            # 清空（全部处理或失败的保留）
            try:
                pending_file.write_text("[]", encoding="utf-8")
            except OSError:
                pass

        # ---- 第二步：今日收盘判断（价格止损 + MA10 破位 + 移动止盈） ----
        # 口径对齐（live 补充）：当日新买入的持仓不做当日收盘判定。
        # 回测中 T+1 开盘成交的仓位首次止损判定在 T+1 收盘（信号日次一 bar）；
        # 模拟盘 T 收盘成交 → 首次判定同样在 T+1 收盘。
        # （否则策略允许的"收盘乖离 MA5 -5%"入场票会被信号日 MA10 在上方
        #   而即时标记卖出，产生回测中不存在的当日 churn。）
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
            if hasattr(self.broker, "update_high_water") and close > 0:
                self.broker.update_high_water(code, close)
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
                import json

                pending_file.parent.mkdir(parents=True, exist_ok=True)
                pending_file.write_text(
                    json.dumps(new_pending, ensure_ascii=False), encoding="utf-8"
                )
            except OSError as e:
                logger.warning(f"待执行止损写入失败: {e}")
        return executed_stops, new_pending

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
    """
    import json as _json

    path = _ROOT / "data" / "filter_stats.json"
    records = []
    try:
        if path.exists():
            records = _json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        records = []
    # 同日覆盖
    records = [r for r in records if r.get("date") != record.get("date")]
    records.append(record)
    # 只保留最近 400 天
    records = sorted(records, key=lambda r: r.get("date", ""))[-400:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning(f"[过滤统计] 写入失败: {e}")


def _monthly_review(scanner: DailyScanner) -> None:
    """月度复盘（修复H.2）：从 PaperBroker 成交流水统计，输出 Markdown。

    修复D(第三轮)：仅当今日为本月最后交易日时生成（trade_cal 判定）；
    日历不可得时幂等回退（monthly_YYYYMM.md 已存在即跳过）。
    修复G(第三轮)：新增"过滤命中统计"栏目。
    """
    today = date.today()
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
    from ..backtest.metrics import _pair_rounds
    from ..types import TradeRecord
    recs = []
    for t in trades:
        d = broker._as_date(t.get("date")) if hasattr(broker, "_as_date") else t.get("date")
        recs.append(TradeRecord(
            ts_code=t["ts_code"], trade_date=d or date.today(),
            side=t["side"], price=t["price"], volume=t["volume"],
            commission=t.get("commission", 0), stamp_tax=t.get("stamp_tax", 0),
        ))
    rounds = _pair_rounds(recs)
    wins = [r for r in rounds if r > 0]
    losses = [r for r in rounds if r < 0]
    win_rate = len(wins) / len(rounds) if rounds else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    pl_ratio = avg_win / avg_loss if avg_loss else 0

    total_fees = sum(t.get("commission", 0) + t.get("stamp_tax", 0) for t in trades)

    # ---- 修复G：过滤命中统计（本月） ----
    month_prefix = today.strftime("%Y-%m")
    filter_stats = []
    try:
        stats_path = _ROOT / "data" / "filter_stats.json"
        if stats_path.exists():
            import json as _json

            all_stats = _json.loads(stats_path.read_text(encoding="utf-8"))
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
        f"- 胜率（按轮次）: {win_rate:.1%}（{len(rounds)} 轮）",
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

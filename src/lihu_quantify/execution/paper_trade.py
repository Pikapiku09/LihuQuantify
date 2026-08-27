"""模拟盘（paper trade）：复用回测撮合逻辑 + Tushare 实时价 + 状态持久化。

修复B（第二轮）：状态持久化
    - data/paper_state.json：cash/positions/trades/trade_day/halted_until
    - 每次 buy/sell/on_new_day 后原子写入（临时文件+替换）
    - 进程重启加载恢复，模拟盘连续验证语义保持
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Optional

import pandas as pd
from loguru import logger

from ..config import get_settings
from ..data.tushare_client import TushareClient
from ..data.duckdb_store import DuckDBStore
from .base import BrokerBase, OrderResult, PositionInfo

# 默认状态文件路径（项目根 data/）
_ROOT = None


def _default_state_file() -> str:
    global _ROOT
    if _ROOT is None:
        from pathlib import Path
        # src/lihu_quantify/execution/paper_trade.py → 项目根
        _ROOT = Path(__file__).resolve().parents[3]
    return str(_ROOT / "data" / "paper_state.json")


@dataclass
class _PaperPosition:
    """模拟盘内部持仓。"""

    volume: int = 0
    available: int = 0       # T+1 可用
    cost: float = 0.0
    today_bought: int = 0    # 今日买入（冻结）


class PaperBroker(BrokerBase):
    """模拟盘券商：现金/持仓/成交按回测口径模拟 + 状态持久化。"""

    COMMISSION_RATE = 0.00025
    STAMP_TAX_RATE = 0.0005
    MIN_COMMISSION = 5.0

    def __init__(
        self,
        init_capital: float = 100000.0,
        tushare_client: Optional[TushareClient] = None,
        duckdb_store: Optional[DuckDBStore] = None,
        state_file: Optional[str] = None,
        persist: bool = True,
    ):
        """
        Args:
            state_file: 状态文件路径（默认 data/paper_state.json）
            persist: 是否启用持久化（测试可关）
        """
        self.cash = init_capital
        self.init_capital = init_capital
        self.positions: dict[str, _PaperPosition] = {}
        self.trades: list[dict] = []          # 成交流水
        self.trade_day: Optional[object] = None
        self.halted_until: Optional[_date] = None   # 修复F：连亏停手（持久化）
        self._halt_map: dict[str, _date] = {}        # 修复F：按票停手 {ts_code: until}
        # 修复4(第六轮)：持仓期最高价（移动止盈基准，与回测 portfolio.high_water_mark 同语义）
        self.high_water_mark: dict[str, float] = {}
        # 行情源（Tushare；None 时用手动 set_price 注入，便于测试）
        self._tushare = tushare_client
        self._store = duckdb_store
        self._manual_prices: dict[str, float] = {}
        # 持久化（修复B）
        self.state_file = state_file or _default_state_file()
        self.persist = persist
        if self.persist:
            self._load_state()

    # ===== 持久化（修复B） =====

    def _save_state(self) -> None:
        """原子写入状态（临时文件 + 替换）。date 对象转字符串保证可序列化。"""
        if not self.persist:
            return

        def _jsonable(v):
            if isinstance(v, _date):
                return str(v)
            return v

        trades_json = [
            {k: _jsonable(v) for k, v in t.items()} for t in self.trades
        ]
        # 修复C(第三轮)：快照资产（cash/total_asset/market_value），看板读真实权益
        try:
            asset = self.query_asset()
        except Exception as e:
            logger.warning(f"[模拟盘] 资产快照失败: {e}")
            asset = {"cash": self.cash, "total_asset": self.cash, "market_value": 0.0}
        state = {
            "cash": self.cash,
            "init_capital": self.init_capital,
            "asset": asset,
            "trade_day": str(self.trade_day) if self.trade_day else None,
            "halted_until": str(self.halted_until) if self.halted_until else None,
            "halt_map": {k: str(v) for k, v in self._halt_map.items()},
            "high_water_mark": dict(self.high_water_mark),   # 修复4(第六轮)
            "positions": {
                code: {
                    "volume": p.volume, "available": p.available,
                    "cost": p.cost, "today_bought": p.today_bought,
                }
                for code, p in self.positions.items()
            },
            "trades": trades_json,
        }
        path = self._manual_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)   # 原子替换
            logger.debug(f"[模拟盘] 状态已持久化: {path.name}")
        except OSError as e:
            logger.warning(f"[模拟盘] 状态写入失败: {e}")

    def _load_state(self) -> None:
        """启动时加载状态。文件不存在则视为全新账户。"""
        from pathlib import Path

        path = Path(self.state_file)
        if not path.exists():
            logger.info(f"[模拟盘] 无历史状态文件，全新账户（资金 {self.init_capital:,.0f}）")
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            self.cash = state.get("cash", self.init_capital)
            self.init_capital = state.get("init_capital", self.init_capital)
            td = state.get("trade_day")
            self.trade_day = _date.fromisoformat(td) if td else None
            hu = state.get("halted_until")
            self.halted_until = _date.fromisoformat(hu) if hu else None
            self._halt_map = {
                k: _date.fromisoformat(v)
                for k, v in (state.get("halt_map") or {}).items()
            }
            # 修复4(第六轮)：恢复高水位（旧状态文件无此字段 → 空 dict，重建即可）
            self.high_water_mark = dict(state.get("high_water_mark") or {})
            self.positions = {
                code: _PaperPosition(
                    volume=p.get("volume", 0),
                    available=p.get("available", 0),
                    cost=p.get("cost", 0.0),
                    today_bought=p.get("today_bought", 0),
                )
                for code, p in (state.get("positions") or {}).items()
            }
            self.trades = state.get("trades", [])
            n = len(self.positions)
            logger.info(f"[模拟盘] 状态恢复：现金 {self.cash:,.0f}，持仓 {n} 只，"
                        f"成交 {len(self.trades)} 笔（trade_day={self.trade_day}）")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logger.warning(f"[模拟盘] 状态文件损坏，忽略并全新开始: {e}")

    def _manual_path(self):
        from pathlib import Path

        return Path(self.state_file)

    def connect(self) -> bool:
        logger.info(f"[模拟盘] 初始化资金 {self.init_capital:,.0f}")
        return True

    # ===== 行情 =====

    def set_price(self, ts_code: str, price: float) -> None:
        """手动注入价格（测试用）。"""
        self._manual_prices[ts_code] = price

    def get_price(self, ts_code: str) -> float:
        """取最新价：优先手动注入，其次 Tushare 日线收盘。"""
        if ts_code in self._manual_prices:
            return self._manual_prices[ts_code]
        if self._tushare is not None:
            try:
                df = self._tushare.query("daily", {"ts_code": ts_code}, use_cache=True)
                if not df.empty:
                    return float(df["close"].iloc[0])
            except Exception as e:
                logger.warning(f"模拟盘取价失败 {ts_code}: {e}")
        return 0.0

    def on_new_day(self, trade_date: object) -> None:
        """日期切换：解冻昨日买入（T+1）+ 记录流水量。"""
        for p in self.positions.values():
            p.available += p.today_bought
            p.today_bought = 0
        self.trade_day = trade_date
        self._save_state()   # 修复B：持久化

    # ===== 交易 =====

    def buy(self, ts_code: str, price: float, volume: int, reason: str = "") -> OrderResult:
        if volume <= 0 or volume % 100 != 0:
            return OrderResult(success=False, msg=f"股数 {volume} 非 100 倍数")
        turnover = price * volume
        commission = max(turnover * self.COMMISSION_RATE, self.MIN_COMMISSION)
        total_cost = turnover + commission
        if total_cost > self.cash:
            return OrderResult(success=False, msg=f"资金不足：需 {total_cost:.2f}，可用 {self.cash:.2f}")
        self.cash -= total_cost
        pos = self.positions.setdefault(ts_code, _PaperPosition())
        old_val = pos.cost * pos.volume
        pos.volume += volume
        pos.today_bought += volume
        pos.cost = (old_val + turnover) / pos.volume
        order_id = f"PB-B-{len(self.trades) + 1}"
        self.trades.append({
            "order_id": order_id, "ts_code": ts_code, "side": "buy",
            "price": price, "volume": volume, "commission": commission,
            "reason": reason,        # 修复4(第六轮)：交易记录存原因
            "date": self.trade_day,
        })
        # 修复4(第六轮)：新仓高水位以成交价起算（同回测 portfolio.apply_fill）
        self.update_high_water(ts_code, price, save=False)
        logger.info(f"[模拟盘买入] {ts_code} {volume}股 @ {price:.2f}（佣金 {commission:.2f}）")
        self._save_state()   # 修复B：持久化
        return OrderResult(success=True, order_id=order_id, filled_volume=volume, filled_price=price)

    def sell(self, ts_code: str, price: float, volume: int, reason: str = "") -> OrderResult:
        pos = self.positions.get(ts_code)
        if pos is None or pos.available < volume:
            avail = pos.available if pos else 0
            return OrderResult(success=False, msg=f"可卖不足：需 {volume}，可用 {avail}（T+1）")
        turnover = price * volume
        commission = max(turnover * self.COMMISSION_RATE, self.MIN_COMMISSION)
        stamp_tax = turnover * self.STAMP_TAX_RATE
        self.cash += turnover - commission - stamp_tax
        # 修复4(第六轮)：卖出实现盈亏（减仓前用加权成本，扣卖出双边费用）
        pnl = (price - pos.cost) * volume - commission - stamp_tax
        pos.volume -= volume
        pos.available -= volume
        if pos.volume <= 0:
            self.positions.pop(ts_code, None)
            self.high_water_mark.pop(ts_code, None)   # 清仓清理高水位（同回测）
        order_id = f"PB-S-{len(self.trades) + 1}"
        self.trades.append({
            "order_id": order_id, "ts_code": ts_code, "side": "sell",
            "price": price, "volume": volume,
            "commission": commission, "stamp_tax": stamp_tax,
            "pnl": pnl, "reason": reason,   # 修复4(第六轮)：盈亏+原因（看板交易表）
            "date": self.trade_day,
        })
        logger.info(f"[模拟盘卖出] {ts_code} {volume}股 @ {price:.2f}"
                    f"（盈亏 {pnl:+.2f}，费用 {commission + stamp_tax:.2f}，原因 {reason or '-'}）")
        self._on_sell_halt_check(ts_code, price)   # 修复F：连亏停手检查
        self._save_state()   # 修复B：持久化
        return OrderResult(success=True, order_id=order_id, filled_volume=volume, filled_price=price)

    def update_high_water(self, ts_code: str, price: float, save: bool = True) -> None:
        """修复4(第六轮)：更新持仓期最高价（只升不降；变化时持久化）。"""
        if price is None or price <= 0:
            return
        if price > self.high_water_mark.get(ts_code, 0.0):
            self.high_water_mark[ts_code] = price
            if save:
                self._save_state()

    def cancel(self, order_id: str) -> OrderResult:
        # 模拟盘即时成交，无撤单概念
        return OrderResult(success=False, order_id=order_id, msg="模拟盘即时成交，无可撤单")

    # ===== 修复F：连亏 3 笔停手（按票统计） =====

    @staticmethod
    def _as_date(v):
        """trades 里的 date 可能是 date 或恢复后的 str（ISO），统一为 date。"""
        if v is None or v == "":
            return None
        if isinstance(v, _date):
            return v
        try:
            return _date.fromisoformat(str(v)[:10])
        except ValueError:
            return None

    def _on_sell_halt_check(self, ts_code: str, sell_price: float) -> None:
        """卖出后统计该票连续亏损笔数，达到 3 笔 → 停手 30 天（写入 halt_map 并持久化）。

        语义：铁律"连亏 3 笔，停手一个月"按同一只票统计（最小语义）。
        修复F(第三轮)：亏损判定扣双边费用——
            pnl = (卖出价 - 买入价) * 股数 - (卖出佣金+印花税) - 买入佣金分摊
        边界微亏（价格持平但费用吃亏）也计入连亏。
        """
        sells = [t for t in self.trades if t["ts_code"] == ts_code and t["side"] == "sell"]
        if not sells:
            return
        # 卖出按时间排序
        def _key(t):
            d = self._as_date(t.get("date"))
            return d if d else _date.min

        sells = sorted(sells, key=_key)
        buys_all = sorted(
            [b for b in self.trades if b["ts_code"] == ts_code and b["side"] == "buy"],
            key=_key,
        )
        # 从最近一笔卖出倒序数连亏（修复F：真实盈亏口径）
        consec = 0
        for t in reversed(sells):
            t_date = _key(t)
            prior_buys = [b for b in buys_all if _key(b) <= t_date]
            if not prior_buys:
                break
            last_buy = prior_buys[-1]
            # 修复F：扣双边费用的真实盈亏（买入佣金按本次卖出股数分摊）
            sell_fees = t.get("commission", 0) + t.get("stamp_tax", 0)
            buy_fee_share = last_buy.get("commission", 0) * t["volume"] / max(1, last_buy["volume"])
            pnl = (t["price"] - last_buy["price"]) * t["volume"] - sell_fees - buy_fee_share
            if pnl < 0:
                consec += 1
            else:
                break
            if consec >= 3:
                from datetime import timedelta as _td
                today = self._as_date(self.trade_day) or _date.today()
                until = today + _td(days=30)
                self._halt_map[ts_code] = until
                logger.warning(f"[铁律F] {ts_code} 连亏 3 笔，停手至 {until}")
                return

    def is_halted(self, ts_code: str, today: Optional[_date] = None) -> bool:
        """该票是否处于停手期。"""
        until = self._halt_map.get(ts_code)
        if until is None:
            return False
        today = today or _date.today()
        return today < until

    def halted_codes(self) -> dict[str, _date]:
        """当前停手中的票 {ts_code: until}。"""
        return dict(self._halt_map)

    # ===== 查询 =====

    def query_positions(self) -> list[PositionInfo]:
        result = []
        for code, p in self.positions.items():
            if p.volume <= 0:
                continue
            cur = self.get_price(code)
            result.append(PositionInfo(
                ts_code=code,
                volume=p.available,
                frozen=p.volume - p.available,
                cost=p.cost,
                market_value=p.volume * cur,
            ))
        return result

    def query_asset(self) -> dict:
        mv = sum(p.volume * self.get_price(c) for c, p in self.positions.items())
        return {
            "cash": self.cash,
            "total_asset": self.cash + mv,
            "market_value": mv,
        }

    def _ensure_tushare(self):
        if self._tushare is None:
            settings = get_settings("config/settings.yaml")
            self._tushare = TushareClient(
                token=settings.resolved_tushare_token(),
                cache_dir=settings.resolved_cache_dir(),
            )
            self._store = self._store or DuckDBStore(settings.resolved_duckdb_path())

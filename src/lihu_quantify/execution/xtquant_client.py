"""MiniQMT (xtquant) 客户端封装。

依赖说明：
    xtquant 不在 PyPI——由 QMT 客户端安装目录自带
    （<QMT安装路径>/bin.x64/Lib/site-packages）。
    使用前需把该目录加入 sys.path 或 PYTHONPATH，并保持 QMT 极简模式客户端运行。

    本模块延迟导入 xtquant：无 QMT 环境时其他模块（paper_trade/OMS 测试）仍可用。
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from .base import BrokerBase, OrderResult, PositionInfo

# xtquant 代码后缀映射：600584.SH → QMT 格式（沪 .SH 深市用 1 结尾的证券代码）
# xtquant 使用 600584.SH / 000001.SZ 格式，与 Tushare 一致（仅 ETF/指数略有差异）


class MiniQMTClient(BrokerBase):
    """MiniQMT 实盘客户端。"""

    def __init__(self, qmt_path: str, account_id: str, account_type: str = "STOCK"):
        """
        Args:
            qmt_path: QMT 安装路径（bin.x64 上级），如 C:/国金QMT/userdata_mini
            account_id: 资金账号
            account_type: 账户类型（STOCK 股票）
        """
        self.qmt_path = qmt_path
        self.account_id = account_id
        self.account_type = account_type
        self._xttrader = None
        self._xtacc = None
        self._connected = False

    def _import_xtquant(self):
        """延迟导入 xtquant，给出清晰错误提示。"""
        try:
            from xtquant import xttrader, xtconstant  # noqa: F401
            return xttrader, xtconstant
        except ImportError as e:
            raise ImportError(
                "未找到 xtquant。请确认：\n"
                "1) 已安装 MiniQMT（极简模式）客户端并登录\n"
                "2) 把 QMT 安装目录下的 bin.x64/Lib/site-packages 加入 PYTHONPATH，\n"
                f"   当前 PYTHONPATH 未包含 QMT 路径（{self.qmt_path}）\n"
                f"原始错误: {e}"
            ) from e

    def connect(self) -> bool:
        """连接 MiniQMT。"""
        try:
            xttrader, xtconstant = self._import_xtquant()
            # xttrader 路径指向 userdata_mini
            trader = xttrader.XtQuantTrader(self.qmt_path, "lihu_quant_session")
            trader.start()
            connect_result = trader.connect()
            if connect_result != 0:
                logger.error(f"QMT 连接失败 code={connect_result}")
                return False
            # 订阅账户
            acc = xttrader.StockAccount(self.account_id)
            subscribe_result = trader.subscribe(acc)
            if subscribe_result != 0:
                logger.error(f"账户订阅失败 code={subscribe_result}")
                return False
            self._xttrader = trader
            self._xtacc = acc
            self._connected = True
            logger.info(f"MiniQMT 已连接：账号 {self.account_id}")
            return True
        except Exception as e:
            logger.error(f"MiniQMT 连接异常: {e}")
            return False

    @property
    def connected(self) -> bool:
        return self._connected

    def _ensure_connected(self):
        if not self._connected:
            raise RuntimeError("MiniQMT 未连接，请先 connect()")

    def buy(self, ts_code: str, price: float, volume: int) -> OrderResult:
        """市价买入（用限价=现价±滑点模拟市价，避免市价单被拒）。"""
        self._ensure_connected()
        try:
            from xtquant import xtconstant
            order_id = self._xttrader.order_stock(
                self._xtacc,
                ts_code,
                xtconstant.STOCK_BUY,
                volume,
                xtconstant.FIX_PRICE,   # 限价单（比市价单可控）
                price,
                "lihu_quantify",
            )
            if order_id < 0:
                return OrderResult(success=False, msg=f"下单失败 code={order_id}")
            logger.info(f"[实盘买入] {ts_code} {volume}股 @ {price} → order_id={order_id}")
            return OrderResult(success=True, order_id=str(order_id))
        except Exception as e:
            logger.error(f"买入异常 {ts_code}: {e}")
            return OrderResult(success=False, msg=str(e))

    def sell(self, ts_code: str, price: float, volume: int) -> OrderResult:
        self._ensure_connected()
        try:
            from xtquant import xtconstant
            order_id = self._xttrader.order_stock(
                self._xtacc,
                ts_code,
                xtconstant.STOCK_SELL,
                volume,
                xtconstant.FIX_PRICE,
                price,
                "lihu_quantify",
            )
            if order_id < 0:
                return OrderResult(success=False, msg=f"下单失败 code={order_id}")
            logger.info(f"[实盘卖出] {ts_code} {volume}股 @ {price} → order_id={order_id}")
            return OrderResult(success=True, order_id=str(order_id))
        except Exception as e:
            logger.error(f"卖出异常 {ts_code}: {e}")
            return OrderResult(success=False, msg=str(e))

    def cancel(self, order_id: str) -> OrderResult:
        self._ensure_connected()
        try:
            code = self._xttrader.cancel_order_stock(self._xtacc, int(order_id))
            return OrderResult(success=code == 0, order_id=order_id,
                               msg="" if code == 0 else f"撤单失败 code={code}")
        except Exception as e:
            return OrderResult(success=False, msg=str(e))

    def query_positions(self) -> list[PositionInfo]:
        """查询持仓。"""
        self._ensure_connected()
        try:
            pos_list = self._xttrader.query_stock_positions(self._xtacc)
            result = []
            for p in pos_list:
                if p.volume <= 0 and p.can_use_volume <= 0:
                    continue
                result.append(PositionInfo(
                    ts_code=p.stock_code,
                    volume=p.can_use_volume,       # 可用（T+1 后）
                    frozen=p.volume - p.can_use_volume,
                    cost=p.open_price,
                    market_value=p.market_value,
                ))
            return result
        except Exception as e:
            logger.error(f"查询持仓异常: {e}")
            return []

    def query_asset(self) -> dict:
        """查询资产。"""
        self._ensure_connected()
        try:
            asset = self._xttrader.query_stock_asset(self._xtacc)
            return {
                "cash": asset.cash,
                "total_asset": asset.total_asset,
                "market_value": asset.market_value,
                "frozen_cash": getattr(asset, "frozen_cash", 0),
            }
        except Exception as e:
            logger.error(f"查询资产异常: {e}")
            return {"cash": 0, "total_asset": 0, "market_value": 0}

    def get_price(self, ts_code: str) -> float:
        """查最新价（xtdata 行情）。"""
        try:
            from xtquant import xtdata
            data = xtdata.get_market_data_ex(
                ["close"], [ts_code], period="tick", count=1
            )
            if ts_code in data and not data[ts_code].empty:
                return float(data[ts_code]["close"].iloc[-1])
            return 0.0
        except Exception as e:
            logger.warning(f"获取 {ts_code} 最新价失败: {e}")
            return 0.0

    def close(self) -> None:
        if self._xttrader is not None:
            try:
                self._xttrader.stop()
            except Exception:
                pass
            self._connected = False
            logger.info("MiniQMT 已断开")

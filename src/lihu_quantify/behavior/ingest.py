"""券商交割单读取与清洗（真实资金成交记录）。

数据特征（2026-11 实测，两个导出文件首尾相接）：
    - **GBK / GB18030 编码**：用 UTF-8 读会得到乱码列名，别浪费时间试
    - 证券代码 / 股东账号列带 Excel 字符串公式残留，形如 ``= "516510      "``
    - 「交易类别」列只有「证券买入」「证券卖出」是真实成交；其余
      （天天宝赎回 / 证券申购 / 证券转银行 / 利息归本 / 红利入账 / 融券购回 …）
      是资金流转，必须排除，否则盈亏与换手率会被系统性虚增

设计约束：
    - 纯函数、无全局状态、不依赖网络、不调用 Tushare
    - 不 import ``scripts/`` 下的私有诊断脚本（那是探索产物，不是库）
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

# 视为真实成交的交易类别；其余一律跳过
BUY_KINDS = ("证券买入",)
SELL_KINDS = ("证券卖出",)

# 沪市前缀：6xx 股票、11x 可转债、5xx 基金（51/58）
_SH_PREFIX2 = {"11", "51", "58", "90"}
# 北交所前缀
_BJ_PREFIX1 = {"4", "8"}


def clean_cell(raw) -> str:
    """去掉 Excel 字符串公式残留与首尾空白。"""
    if raw is None:
        return ""
    s = str(raw).strip()
    if s.startswith("="):
        s = s.lstrip("=").strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.strip()


def to_float(raw, default: float = 0.0) -> float:
    """宽松浮点解析（去逗号、兼容 ``--``）。"""
    s = clean_cell(raw).replace(",", "")
    if not s or s in {"--", "-", "—"}:
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def to_ts_code(raw) -> str:
    """6 位证券代码 → ``.SH`` / ``.SZ`` / ``.BJ`` ts_code。

    规则：6 开头及 11/51/58/90 前缀 → SH；4/8 开头 → BJ；其余 → SZ。
    """
    digits = "".join(ch for ch in clean_cell(raw) if ch.isdigit())
    if not digits:
        return ""
    if len(digits) < 6:
        digits = digits.zfill(6)
    code = digits[:6]
    if code[0] == "6" or code[:2] in _SH_PREFIX2:
        return f"{code}.SH"
    if code[0] in _BJ_PREFIX1:
        return f"{code}.BJ"
    return f"{code}.SZ"


def parse_date(raw) -> date | None:
    """解析 ``2026-11-13`` / ``20261113`` / ``2026/11/13``。"""
    s = clean_cell(raw)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def load_fills(paths) -> list[dict]:
    """读取一个或多个交割单 CSV（GB18030），返回规范化成交列表。

    每项字段：
        ts_code / name / side("buy"|"sell") / trade_date / price /
        volume / amount / fee
    只保留真实成交，按 (日期, 代码) 升序。
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    fills: list[dict] = []
    for p in paths:
        path = Path(p)
        with path.open("r", encoding="gb18030", newline="", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rec = {
                    (k or "").strip().replace("\ufeff", ""): v
                    for k, v in row.items()
                }
                kind = clean_cell(rec.get("交易类别"))
                if kind in BUY_KINDS:
                    side = "buy"
                elif kind in SELL_KINDS:
                    side = "sell"
                else:
                    continue
                ts_code = to_ts_code(rec.get("证券代码"))
                trade_date = parse_date(rec.get("发生日期")) or parse_date(
                    rec.get("交收日期"))
                if not ts_code or trade_date is None:
                    continue
                price = to_float(rec.get("成交均价"))
                volume = int(to_float(rec.get("成交数量")))
                if price <= 0 or volume <= 0:
                    continue
                fee = (to_float(rec.get("佣金"))
                       + to_float(rec.get("印花税"))
                       + to_float(rec.get("过户费"))
                       + to_float(rec.get("其他费用")))
                amount = to_float(rec.get("成交金额")) or price * volume
                fills.append({
                    "ts_code": ts_code,
                    "name": clean_cell(rec.get("证券名称")),
                    "side": side,
                    "trade_date": trade_date,
                    "price": price,
                    "volume": volume,
                    "amount": amount,
                    "fee": fee,
                    # 资金余额用于换手率的分母（平均资金规模）
                    "cash_balance": to_float(rec.get("资金余额")),
                })
    fills.sort(key=lambda r: (r["trade_date"], r["ts_code"]))
    return fills


def to_trade_records(fills: list[dict]) -> list:
    """fills → ``TradeRecord`` 列表（供 backtest.metrics 的 FIFO 配对复用）。

    费用归集：买入侧全部记 commission；卖出侧全部记 commission+stamp_tax
    （``_iter_fifo_matches`` 对卖出侧取两者之和，故与原始费用等价）。
    """
    from ..types import TradeRecord

    out = []
    for f in fills:
        out.append(TradeRecord(
            ts_code=f["ts_code"],
            trade_date=f["trade_date"],
            side=f["side"],
            price=f["price"],
            volume=f["volume"],
            commission=float(f["fee"]),
            stamp_tax=0.0,
        ))
    return out

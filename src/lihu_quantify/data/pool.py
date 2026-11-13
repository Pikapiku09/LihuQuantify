"""股票池构建：分层抽样（修复J）。

来源：scripts/grid_search_v2.py 的 build_stratified_pool 抽取为公共模块，
供 网格搜索 / 巡检 / 回测 三处共用（清单J 第2条：同步换池）。

设计：
    - 高效取数：daily(trade_date=...) 一次返回全市场当日行情（20 次 API 而非逐股）
    - 过滤：主板 / 剔ST / 上市≥60日 / 近20日日均成交额≥1亿
    - 分层：按日均成交额排序分 N 层，每层等比抽样（大中小盘代表性）
    - 固定 seed 可复现
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from .tushare_client import TushareClient
from .duckdb_store import DuckDBStore

# 池清单缓存（同 seed+数量 复用，避免每天重建消耗 API）
_POOL_CACHE_DIR = Path(__file__).resolve().parents[3] / "data"


def build_stratified_pool(
    client: TushareClient,
    store: DuckDBStore,
    target_n: int = 200,
    layers: int = 5,
    seed: int = 42,
    min_list_days: int = 60,
    min_avg_amount: float = 1e8,
    use_cache: bool = True,
    bar_date_hint: date | str | None = None,
) -> list[str]:
    """按成交额分层抽样构建股票池（详见模块 docstring）。"""
    import json

    # P2-1（第十一轮）：缓存文件名带日期段，防股票池卡死在首次构建日；
    # 最新交易日由 store 锚定（P2-9-5 日期纪律），非本地时钟。
    bar_date = "unknown"
    try:
        bar_date = store.get_latest_trade_date()
    except Exception:
        pass
    if isinstance(bar_date, date):
        bar_date = bar_date.strftime("%Y%m%d")
    if bar_date_hint is None:
        # 未显式提供基准日时，用刚锚定的最新交易日
        bar_date_hint = bar_date
    cache_file = (
        _POOL_CACHE_DIR
        / f"strat_pool_n{target_n}_L{layers}_seed{seed}_{bar_date}.json"
    )
    if use_cache and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, list) and len(cached) >= target_n * 0.8:
                logger.info(f"[分层池] 命中缓存 {cache_file.name}（{len(cached)} 只）")
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    rng = random.Random(seed)

    # 1. 基础过滤（stock_basic）
    basic = client.query("stock_basic", {"list_status": "L"})
    if basic.empty:
        return []
    store.upsert("stock_basic", basic, date_cols=("list_date", "delist_date"))
    dfb = basic.copy()
    mask = (
        ~dfb["ts_code"].str.startswith("688")
        & ~dfb["ts_code"].str.startswith("300")
        & ~dfb["ts_code"].str.startswith("301")
    )
    if "name" in dfb.columns:
        mask &= ~dfb["name"].str.contains("ST", na=False)
    if "list_date" in dfb.columns:
        list_dates = pd.to_datetime(dfb["list_date"], errors="coerce").dt.date
        cutoff = pd.to_datetime(str(bar_date_hint)).date() - timedelta(days=min_list_days)
        mask &= list_dates.apply(lambda d: bool(d) and d <= cutoff)
    dfb = dfb[mask]
    candidates = set(dfb["ts_code"])
    logger.info(f"[分层池] 基础过滤后候选 {len(candidates)} 只")

    # 2. 近 20 个交易日全市场成交额
    # 第四轮清单3 补充修复：固定参数 + 永久缓存会卡在旧交易日，直连（每日 1 次）
    idx = client.query(
        "index_daily", {"ts_code": "000001.SH", "end_date": "20301231"},
        use_cache=False,
    )
    store.upsert("index_daily", idx)
    latest = store.get_latest_trade_date()
    idx_df = idx.copy()
    idx_df["trade_date"] = pd.to_datetime(idx_df["trade_date"], format="%Y%m%d").dt.date
    recent_dates = sorted(idx_df[idx_df["trade_date"] <= latest]["trade_date"])[-20:]

    amount_sum: dict[str, float] = {}
    days_counted = 0
    for td in reversed(recent_dates):
        try:
            day = client.query("daily", {"trade_date": td.strftime("%Y%m%d")}, use_cache=True)
        except Exception as e:
            logger.warning(f"[分层池] {td} 行情拉取失败: {e}")
            continue
        if day.empty:
            continue
        days_counted += 1
        for _, row in day.iterrows():
            code = row["ts_code"]
            if code in candidates:
                amount_sum[code] = amount_sum.get(code, 0.0) + float(row["amount"])
    if days_counted == 0:
        logger.error("[分层池] 无行情数据")
        return []
    avg_amount = {c: s / days_counted for c, s in amount_sum.items()}
    # amount 千元；min_avg_amount 元 → 千元
    min_amt = min_avg_amount / 1e3
    liquid = sorted(
        [(c, a) for c, a in avg_amount.items() if a >= min_amt],
        key=lambda x: x[1],
    )
    logger.info(f"[分层池] 流动性过滤后 {len(liquid)} 只（{days_counted} 日均值）")

    # 3. 分层等比抽样
    n = len(liquid)
    if n <= target_n:
        picked = [c for c, _ in liquid]
    else:
        per_layer = target_n // layers
        picked: list[str] = []
        layer_size = n / layers
        for i in range(layers):
            lo, hi = int(i * layer_size), int((i + 1) * layer_size)
            layer_codes = [c for c, _ in liquid[lo:hi]]
            take = min(per_layer, len(layer_codes))
            picked.extend(rng.sample(layer_codes, take))
        if len(picked) < target_n:
            rest = [c for c, _ in liquid if c not in set(picked)]
            picked.extend(rng.sample(rest, min(target_n - len(picked), len(rest))))
        picked = sorted(set(picked))

    # 缓存
    try:
        _POOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        logger.info(f"[分层池] 最终 {len(picked)} 只（seed={seed}），缓存 {cache_file.name}")
    except OSError as e:
        logger.warning(f"[分层池] 缓存写入失败: {e}")
    return picked

"""补齐 index_daily 与 adj_factors 到最新交易日（2026-11-17）。

为什么单独一个脚本，不塞进 backfill_market_data.py
--------------------------------------------------
那个脚本是**按交易日拉全市场**（``daily(trade_date=...)`` 一次拿全市场），
但 ``index_daily`` 与 ``adj_factor`` 的接口形状不同：
- ``index_daily``：按 **ts_code + 日期区间** 取，一个指数一次请求；
- ``adj_factor``：可以按 ``trade_date`` 取全市场，形状与 daily 一致。

硬塞进按日期循环会让两条路径互相将就；分开写，各自用对形状，逻辑也更清楚。

两个缺口（2026-11-17 实测）
--------------------------
- ``index_daily``：000001.SH 已到 2026-11-13，缺 11-16 / 11-17。
  **这条是市场闸门的输入** —— 停在旧日期会让「上涨段/下跌段」判断用过期数据。
- ``adj_factors``：828 只、末日 2026-11-13，缺 11-16 / 11-17；
  另外 AI 结论涉及的 37 只有 31 只**完全没有因子**，影子交易无法统一复权口径。

用法::

    .venv\\Scripts\\python.exe scripts/backfill_index_and_adj.py
    .venv\\Scripts\\python.exe scripts/backfill_index_and_adj.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lihu_quantify.config import get_settings  # noqa: E402
from lihu_quantify.data.duckdb_store import DuckDBStore  # noqa: E402
from lihu_quantify.data.tushare_client import TushareClient  # noqa: E402

DB_PATH = ROOT / "data" / "lihu_quant.duckdb"
CACHE_DIR = ROOT / "data" / "cache"
DEFAULT_INBOX = Path("E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/store/conclusions.jsonl")
INDEX_CODES = ["000001.SH"]


def build_client() -> TushareClient:
    settings = get_settings()
    token = settings.resolved_tushare_token()
    if not token:
        raise SystemExit("Tushare token 解析为空")
    return TushareClient(token=token, cache_dir=CACHE_DIR)


def _conclusion_codes(path: Path) -> set[str]:
    import json

    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = row.get("code")
        if code:
            out.add(str(code))
    return out


def _store_years(store: DuckDBStore) -> int:
    try:
        row = store.query("SELECT MIN(trade_date) AS d FROM daily_quotes").iloc[0]
        return int(pd.Timestamp(row["d"]).year)
    except Exception:  # noqa: BLE001
        return date.today().year - 2


def backfill_index(client: TushareClient, store: DuckDBStore, start: date, end: date) -> int:
    total = 0
    for code in INDEX_CODES:
        try:
            df = client.query("index_daily", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[指数] {code} 拉取失败: {exc}")
            continue
        if df is None or df.empty:
            logger.info(f"[指数] {code} 区间内无新数据")
            continue
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        keep = [c for c in ("ts_code", "trade_date", "open", "high", "low", "close",
                            "pre_close", "change", "pct_chg", "vol", "amount") if c in df.columns]
        store.upsert("index_daily", df[keep])
        total += len(df)
        logger.info(f"[指数] {code} 写入 {len(df)} 行（{df['trade_date'].min()} ~ {df['trade_date'].max()}）")
    return total


def backfill_adj(client: TushareClient, store: DuckDBStore, days: list[date],
                 universe: set[str]) -> int:
    total = 0
    for d in days:
        try:
            df = client.query("adj_factor", {"trade_date": d.strftime("%Y%m%d")})
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[复权] {d} 拉取失败: {exc}")
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        if universe:
            df = df[df["ts_code"].isin(universe)]
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        keep = [c for c in ("ts_code", "trade_date", "adj_factor") if c in df.columns]
        store.upsert("adj_factors", df[keep])
        total += len(df)
        logger.info(f"[复权] {d} 写入 {len(df)} 行")
    return total



def backfill_adj_by_code(client: TushareClient, store: DuckDBStore,
                         codes: set[str], start: date, end: date) -> int:
    """按标的补历史因子。

    为什么还需要这条路径：``adj_factor(trade_date=...)`` 只能拿到「那一天的全市场」，
    把新标的加进股票池**不会**凭空长出历史行（实测：补完 11-16/17 后，29 只标的只有 2 行）。
    而这些标的历史上确实有因子，只是本地没采过 —— 必须按 ts_code + 区间单独取。
    """
    total = 0
    for code in sorted(codes):
        try:
            df = client.query("adj_factor", {
                "ts_code": code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[复权-逐只] {code} 失败: {exc}")
            continue
        if df is None or df.empty:
            logger.info(f"[复权-逐只] {code} 无数据（ETF/基金无复权因子属正常）")
            continue
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        keep = [c for c in ("ts_code", "trade_date", "adj_factor") if c in df.columns]
        store.upsert("adj_factors", df[keep])
        total += len(df)
        logger.info(f"[复权-逐只] {code} 写入 {len(df)} 行")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--inbox", default=str(DEFAULT_INBOX))
    ap.add_argument("--start", default=None, help="默认：各表各自末日 +1 天")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = DuckDBStore(Path(args.db))
    end = date.fromisoformat(args.end)

    # 交易日范围：从两张表各自末日里较早的那个 +1 天开始，保证都补上
    try:
        idx_max = store.query("SELECT MAX(trade_date) AS d FROM index_daily").iloc[0]["d"]
        adj_max = store.query("SELECT MAX(trade_date) AS d FROM adj_factors").iloc[0]["d"]
        earliest = min(x for x in (idx_max, adj_max) if x is not None)
        start = date.fromisoformat(str(pd.Timestamp(earliest).date())) + timedelta(days=1)
    except Exception:  # noqa: BLE001
        start = date.fromisoformat(args.start) if args.start else date(end.year, 1, 1)
    if args.start:
        start = date.fromisoformat(args.start)

    logger.info(f"区间 {start} ~ {end}")
    if args.dry_run:
        logger.info("dry-run：不写库")
        return 0

    client = build_client()

    # 1) 指数（按 ts_code + 区间）
    n_idx = backfill_index(client, store, start, end)

    # 2) 复权因子（按交易日，保留 库内已有 ∪ 结论标的）
    try:
        uni = set(store.query("SELECT DISTINCT ts_code FROM adj_factors")["ts_code"].tolist())
    except Exception:  # noqa: BLE001
        uni = set()
    try:
        uni |= set(store.query("SELECT DISTINCT ts_code FROM daily_quotes")["ts_code"].tolist())
    except Exception:  # noqa: BLE001
        pass
    extra = _conclusion_codes(Path(args.inbox))
    if extra:
        logger.info(f"结论标的 {len(extra)} 只并入复权池（其中 {len(extra - uni)} 只是新增）")
        uni |= extra

    # 只对「新区间内仍缺」的交易日请求，避免重复
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:              # 粗筛工作日；非交易日接口会返回空
            days.append(d)
        d += timedelta(days=1)
    n_adj = backfill_adj(client, store, days, uni)

    # 3) 逐只补历史：只针对「因子行数明显不足」的标的
    try:
        cov = store.query(
            "SELECT ts_code, COUNT(*) AS n FROM adj_factors GROUP BY ts_code"
        )
        thin = {r["ts_code"] for _, r in cov.iterrows() if int(r["n"]) < 10}
    except Exception:  # noqa: BLE001
        thin = set()
    need = (extra | uni) & (thin | extra) if extra else thin
    # 只补结论标的里历史缺失的，避免无谓请求
    need = {c for c in need if c in extra} if extra else set()
    hist_start = date(end.year - 1, 12, 1)
    if args.start:
        hist_start = min(hist_start, date.fromisoformat(args.start))
    n_hist = backfill_adj_by_code(client, store, need, hist_start, end) if need else 0

    logger.info(f"完成：index_daily +{n_idx} 行，adj_factors +{n_adj}（按日）+{n_hist}（逐只）")

    # 复核
    for table in ("index_daily", "adj_factors"):
        row = store.query(
            f"SELECT COUNT(*) AS n, MIN(trade_date) AS a, MAX(trade_date) AS b FROM {table}"
        ).iloc[0]
        logger.info(f"  {table:14s} {int(row['n']):>8} 行 {row['a']} ~ {row['b']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

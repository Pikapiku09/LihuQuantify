"""波段池回测数据拉取（复用 TushareClient 的缓存/限流/重试，断点续跑）。

段1 gold：broker_recommend 202309-202610 月度金股名单（PIT 无未来函数，回测①景气的机械口径）
段2 px：候选票 daily_basic(2020-10 起：close/pe_ttm/pb，估值 3 年分位)
        + adj_factor(2022-12 起：复权因子；注意 daily 接口不含 adj_factor，必须单独查)

用法：
  python scripts/swing_bt_data.py gold
  python scripts/swing_bt_data.py px [--limit N]

输出：
  data/swing_bt/gold_membership.json  {月份: {ts_code: {name, brokers}}}
  data/swing_bt/px/<code>.json        [{d, close, adj, pe, pb}] 升序合并序列
  data/swing_bt/progress_px.json      断点续跑清单
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loguru import logger

from lihu_quantify.config import get_settings
from lihu_quantify.data.tushare_client import TushareClient

BT = ROOT / "data" / "swing_bt"
PX_DIR = BT / "px"
GOLD_PATH = BT / "gold_membership.json"
PROGRESS = BT / "progress_px.json"
MONTHS = [f"{y}{m:02d}" for y in range(2023, 2027) for m in range(1, 13)
          if (y, m) >= (2023, 9) and (y, m) <= (2026, 10)]
PX_START, PX_END = "20221201", "20261016"     # 复权因子（30 周均线 warmup）
DB_START, DB_END = "20201001", "20261016"     # 收盘/估值（窗口起点即有 3 年分位）


def _read_token(path: str) -> str:
    raw = Path(path).read_text(encoding="utf-8").strip()
    if raw.startswith("{"):
        import re
        m = re.search(r"token=([A-Za-z0-9]+)", raw)
        if not m:
            raise ValueError(f"无法从 {path} 的 MCP url 中提取 token")
        return m.group(1)
    return raw


def client() -> TushareClient:
    cfg = get_settings()
    token = cfg.tushare.token or _read_token(cfg.tushare.token_file)
    return TushareClient(token=token, cache_dir=ROOT / "data" / "cache",
                         cache_mtime_ttl=0, rate_limit_interval=0.6)


def num(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def stage_gold(c: TushareClient) -> None:
    BT.mkdir(parents=True, exist_ok=True)
    out = {}
    if GOLD_PATH.exists():
        out = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    for m in MONTHS:
        if m in out:
            continue
        df = c.query("broker_recommend", {"month": m})
        month_map = {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get("ts_code") or "")
                if not code:
                    continue
                e = month_map.setdefault(code, {"name": str(r.get("name") or ""), "brokers": 0})
                e["brokers"] += 1
                if not e["name"] and r.get("name"):
                    e["name"] = str(r["name"])
        out[m] = month_map
        GOLD_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        logger.info(f"[gold] {m}: {len(month_map)} 只（累计 {len(out)} 月）")
    codes = sorted({c0 for mm in out.values() for c0 in mm})
    logger.info(f"[gold] 完成：{len(out)} 个月，唯一候选 {len(codes)} 只")


def stage_px(c: TushareClient, limit: int | None = None) -> None:
    PX_DIR.mkdir(parents=True, exist_ok=True)
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    codes = sorted({c0 for mm in gold.values() for c0 in mm})
    done = set(json.loads(PROGRESS.read_text(encoding="utf-8"))) if PROGRESS.exists() else set()
    # 防竞态：以 px 文件存在与否为准（progress 可能被中断的任务回写污染）
    todo = [x for x in codes if x not in done and not (PX_DIR / f"{x.replace('.', '_')}.json").exists()]
    if limit:
        todo = todo[:limit]
    logger.info(f"[px] 候选 {len(codes)}，已完成 {len(done)}，本次 {len(todo)}")
    for i, code in enumerate(todo, 1):
        try:
            db = c.query("daily_basic", {"ts_code": code, "start_date": DB_START, "end_date": DB_END,
                                         "fields": "trade_date,close,pe_ttm,pb"})
            adj = c.query("adj_factor", {"ts_code": code, "start_date": PX_START, "end_date": PX_END,
                                         "fields": "trade_date,adj_factor"})
            if db is None or db.empty:
                logger.warning(f"[px] {code} 无收盘/估值数据，跳过")
            else:
                dbi = {str(k): v for k, v in db.set_index("trade_date").iterrows()}
                adi = {str(k): v for k, v in adj.set_index("trade_date").iterrows()} if adj is not None and not adj.empty else {}
                dates = sorted(set(dbi) | set(adi))
                merged = []
                for d in dates:
                    drow = dbi.get(d)
                    arow = adi.get(d)
                    merged.append({
                        "d": d,
                        "close": num(drow.get("close")) if drow is not None else None,
                        "adj": num(arow.get("adj_factor")) if arow is not None else None,
                        "pe": num(drow.get("pe_ttm")) if drow is not None else None,
                        "pb": num(drow.get("pb")) if drow is not None else None,
                    })
                (PX_DIR / f"{code.replace('.', '_')}.json").write_text(json.dumps(merged), encoding="utf-8")
            done.add(code)
        except Exception as e:
            logger.warning(f"[px] {code} 失败: {e}")
        if i % 20 == 0:
            PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
            logger.info(f"[px] 进度 {i}/{len(todo)}")
    PROGRESS.write_text(json.dumps(sorted(done)), encoding="utf-8")
    logger.info(f"[px] 完成：累计 {len(done)} 只")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "gold"
    c = client()
    if mode == "gold":
        stage_gold(c)
    elif mode == "px":
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        stage_px(c, lim)
    else:
        print("用法: swing_bt_data.py gold|px [--limit N]")

"""扩展样本数据拉取（P1-①）：把波段池回测样本从 2023-10 前推至 2021-01。

段1 gold_ext：broker_recommend 2021-01 ~ 2023-08（32 个月，PIT 月度金股名单）
段2 px_ext  ：全部候选票重取长区间（daily_basic 2018-01 起 → 3 年估值分位 warmup；
              adj_factor 2019-01 起 → 30 周均线 warmup），覆盖写入旧文件

数据口径与 swing_bt_data.py 完全一致，仅拉长区间；断点续跑：data/swing_bt/ext_px_done.json

用法：
  python scripts/swing_bt_ext_data.py            # 全量（gold + px）
  python scripts/swing_bt_ext_data.py gold       # 只补金股名单
  python scripts/swing_bt_ext_data.py px [--limit N]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.data.tushare_client import TushareClient

BT = ROOT / "data" / "swing_bt"
PX_DIR = BT / "px"
GOLD_PATH = BT / "gold_membership.json"
MARKS = BT / "ext_px_done.json"

GOLD_EXT = [f"{y}{m:02d}" for y in (2021, 2022, 2023) for m in range(1, 13)
            if (2021, 1) <= (y, m) <= (2023, 8)]
DB_START, DB_END = "20180101", "20261016"
PX_START, PX_END = "20190101", "20261016"


def _token() -> str:
    cfg = get_settings_token()
    if cfg:
        return cfg
    raw = Path(r"E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/tushare.token").read_text(encoding="utf-8").strip()
    if raw.startswith("{"):
        import re
        return re.search(r"token=([A-Za-z0-9]+)", raw).group(1)
    return raw


def get_settings_token() -> str:
    try:
        from lihu_quantify.config import get_settings
        return get_settings().resolved_tushare_token()
    except Exception:
        return ""


def client() -> TushareClient:
    return TushareClient(token=_token(), cache_dir=ROOT / "data" / "cache",
                         cache_mtime_ttl=0, rate_limit_interval=0.5)


def q(c: TushareClient, api: str, params: dict, tries: int = 4):
    for i in range(tries):
        try:
            return c.query(api, params)
        except Exception as e:
            msg = str(e)[:160]
            print(f"  [retry {i+1}/{tries}] {api} {params.get('ts_code')}: {msg}", flush=True)
            time.sleep(2 + 3 * i)
    return None


def num(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def stage_gold(c: TushareClient) -> None:
    out = json.loads(GOLD_PATH.read_text(encoding="utf-8")) if GOLD_PATH.exists() else {}
    for m in GOLD_EXT:
        if m in out and out[m]:
            continue
        df = q(c, "broker_recommend", {"month": m})
        mm = {}
        if df is not None and not df.empty:
            for _, r in df.iterrows():
                code = str(r.get("ts_code") or "")
                if not code:
                    continue
                e = mm.setdefault(code, {"name": str(r.get("name") or ""), "brokers": 0})
                e["brokers"] += 1
        out[m] = mm
        GOLD_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        print(f"[gold] {m}: {len(mm)} 只（累计 {len(out)} 月）", flush=True)
    print(f"[gold] 完成：{len(out)} 月", flush=True)


def stage_px(c: TushareClient, limit: int | None = None) -> None:
    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    codes = sorted({x for mm in gold.values() for x in mm})
    done = set(json.loads(MARKS.read_text(encoding="utf-8"))) if MARKS.exists() else set()
    todo = [x for x in codes if x not in done]
    if limit:
        todo = todo[:limit]
    print(f"[px] 候选 {len(codes)}，已完成 {len(done)}，本次 {len(todo)}", flush=True)
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        db = q(c, "daily_basic", {"ts_code": code, "start_date": DB_START, "end_date": DB_END,
                                  "fields": "trade_date,close,pe_ttm,pb"})
        adj = q(c, "adj_factor", {"ts_code": code, "start_date": PX_START, "end_date": PX_END,
                                  "fields": "trade_date,adj_factor"})
        if db is None or db.empty:
            print(f"[px] {code} 无数据，跳过（不标记完成）", flush=True)
            continue
        dbi = {str(k): v for k, v in db.set_index("trade_date").iterrows()}
        adi = {str(k): v for k, v in adj.set_index("trade_date").iterrows()} if adj is not None and not adj.empty else {}
        dates = sorted(set(dbi) | set(adi))
        rows = []
        for d in dates:
            dr = dbi.get(d)
            ar = adi.get(d)
            rows.append({"d": d,
                         "close": num(dr.get("close")) if dr is not None else None,
                         "adj": num(ar.get("adj_factor")) if ar is not None else None,
                         "pe": num(dr.get("pe_ttm")) if dr is not None else None,
                         "pb": num(dr.get("pb")) if dr is not None else None})
        PX_DIR.mkdir(parents=True, exist_ok=True)
        (PX_DIR / f"{code.replace('.', '_')}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        done.add(code)
        if i % 25 == 0 or i == len(todo):
            MARKS.write_text(json.dumps(sorted(done), ensure_ascii=False), encoding="utf-8")
            el = time.time() - t0
            eta = el / i * (len(todo) - i) / 60 if i else 0
            print(f"[px] {i}/{len(todo)} {code} 行数={len(rows)} 已用 {el/60:.1f}min ETA {eta:.0f}min", flush=True)
    MARKS.write_text(json.dumps(sorted(done), ensure_ascii=False), encoding="utf-8")
    print("[px] 完成", flush=True)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = None
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            limit = int(a.split("=", 1)[1])
    c = client()
    if not args or args[0] in ("gold", "all"):
        if args and args[0] == "gold":
            stage_gold(c)
            return
    stage_gold(c)
    stage_px(c, limit=limit)


if __name__ == "__main__":
    main()

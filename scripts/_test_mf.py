import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from lihu_quantify.data.tushare_client import TushareClient
from lihu_quantify.config import get_settings
import re
cfg = get_settings()
raw = Path(cfg.tushare.token_file).read_text(encoding="utf-8").strip()
m = re.search(r"token=([A-Za-z0-9]+)", raw)
token = m.group(1) if raw.startswith("{") else raw
c = TushareClient(token=token, cache_dir=cfg.tushare.cache_dir)
df = c.query("moneyflow_dc", {"ts_code": "600519.SH", "start_date": "20240101", "end_date": "20260831"}, use_cache=False)
print("rows:", len(df))
if not df.empty:
    print("cols:", list(df.columns))
    print(df.head(3).to_string())

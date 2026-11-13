"""Tushare Pro API 客户端：HTTP 取数 + 文件缓存。

继承 dsh-invest-plugin 的缓存纪律：
- 缓存文件命名：<api>_<ts_code>_<end_date>_<start_date_or_na>[_fields].json
- 缓存命中直接读取，跳过 API 请求
- 取数成功后写缓存，供后续阶段与本日其他运行复用

第十一轮 P2-1/P2-2/P2-3：
- P2-1 缓存 TTL：文件 mtime 超过 cache_mtime_ttl 秒 → 视为 miss（防"新财报永不拉到"）
- P2-2 fields 入缓存键：缓存路径含 fields 哈希，防子集查询污染全量缓存
- P2-3 HTTP 重试 + 限流：状态码/JSON 异常捕获、限流指数退避重试、同 API 最小间隔
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from loguru import logger

TUSHARE_API = "https://api.tushare.pro"


class TushareClient:
    """Tushare Pro REST API 客户端。"""

    def __init__(
        self,
        token: str,
        cache_dir: Path | str = "./data/cache",
        cache_mtime_ttl: float = 43200.0,     # 缓存有效期秒（默认 12h）
        rate_limit_interval: float = 0.3,     # 同一 API 相邻请求最小间隔秒
        retries: int = 3,                     # HTTP 失败/限流重试次数
        session: requests.Session | None = None,
    ):
        if not token:
            raise ValueError("Tushare token 不能为空")
        self.token = token
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_mtime_ttl = float(cache_mtime_ttl)
        self.rate_limit_interval = float(rate_limit_interval)
        self.retries = int(retries)
        self._session = session if session is not None else requests.Session()
        self._last_request_ts: dict[str, float] = {}   # {api_name: 上次调用时间}

    def _cache_path(self, api_name: str, params: dict) -> Path:
        """生成缓存文件路径。命名规则见模块 docstring。

        P2-2（第十一轮）：fields 不再单独传入，参数若含 __fields 标记则哈希进文件名，
        保证"先子集后全量"不会命中残缺缓存。
        """
        ts_code = params.get("ts_code", "na")
        end_date = params.get("end_date", "na")
        start_date = params.get("start_date", "na")
        # 接口无 ts_code 时用其他主键
        if ts_code == "na":
            ts_code = params.get("trade_date", params.get("month", "na"))
        safe_code = str(ts_code).replace("/", "_").replace(".", "_")
        # fields 标记（调用方注入过的字段规范化串；内部按 | 分段排序后哈希，
        # 字段顺序不同不影响缓存键，P2-2 归一化）
        fields_marker = params.get("__fields", "")
        if fields_marker:
            parts = sorted(p for p in fields_marker.split("|") if p)
            fields_marker = "_" + hashlib.md5(
                "|".join(parts).encode()
            ).hexdigest()[:8]
        return self.cache_dir / f"{api_name}_{safe_code}_{end_date}_{start_date}{fields_marker}.json"

    def _read_cache(self, path: Path) -> dict | None:
        # P2-1：TTL<=0 = 完全禁用文件缓存（每次都重新请求）
        if self.cache_mtime_ttl > 0:
            if not path.exists():
                return None
            try:
                age = time.time() - path.stat().st_mtime
                if age > self.cache_mtime_ttl:
                    logger.debug(f"缓存过期（{age:.0f}s>TTL），{path.name} 重新请求")
                    return None
            except OSError:
                pass
        else:
            return None
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"缓存读取失败 {path}: {e}，将重新请求")
            return None

    def _write_cache(self, path: Path, data: dict) -> None:
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"缓存写入失败 {path}: {e}")

    def _rate_limiter(self, api_name: str) -> None:
        """P2-3：同一 API 相邻请求最小间隔（防顶积分限流）。"""
        now = time.time()
        last = self._last_request_ts.get(api_name, 0.0)
        wait = self.rate_limit_interval - (now - last)
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts[api_name] = time.time()

    def _post_with_retry(self, body: dict, api_name: str) -> dict:
        """P2-3：POST Tushare，带状态码/JSON 异常捕获 + 限流指数退避重试。"""
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._rate_limiter(api_name)
            try:
                resp = self._session.post(TUSHARE_API, json=body, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                # 识别限流（code!=0 且 msg 含频率/限流特征）→ 退避重试
                if data.get("code") != 0 and _is_rate_limit_msg(data.get("msg", "")):
                    if attempt < self.retries:
                        delay = 2 ** (attempt - 1)  # 1s/2s/4s
                        logger.warning(f"[Tushare 限流] {api_name} {body.get('params')} "
                                       f"msg={data.get('msg')} → {delay}s 后重试({attempt}/{self.retries})")
                        time.sleep(delay)
                        continue
                return data
            except (requests.RequestException, ValueError) as e:  # 网络/JSON 异常
                last_err = e
                if attempt < self.retries:
                    delay = 2 ** (attempt - 1)
                    logger.warning(f"[Tushare HTTP 异常] {api_name}: {e} → {delay}s 后重试({attempt}/{self.retries})")
                    time.sleep(delay)
        # 全部重试耗尽
        if last_err is not None:
            raise last_err
        return {"code": -1, "msg": "请求未能完成"}

    def _parse_response(self, resp: dict) -> pd.DataFrame:
        """Tushare 响应 → DataFrame。响应结构：{code, data:{fields,items}, msg}。"""
        if resp.get("code") != 0:
            raise RuntimeError(
                f"Tushare 接口报错 code={resp.get('code')} msg={resp.get('msg')}"
            )
        data = resp.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not fields:
            return pd.DataFrame()
        return pd.DataFrame(items, columns=fields)

    def query(
        self,
        api_name: str,
        params: dict | None = None,
        use_cache: bool = True,
        fields: list[str] | None = None,
    ) -> pd.DataFrame:
        """查询 Tushare 接口，返回 DataFrame。

        Args:
            api_name: 接口名，如 daily / index_daily / moneyflow
            params: 接口参数，如 {ts_code, start_date, end_date}
            use_cache: 是否使用文件缓存（默认 True）
            fields: 指定返回字段（None 表示全部）
        """
        params = dict(params or {})
        request_fields = fields
        cache_params = dict(params)
        # P2-2：fields 规范化后注入缓存键标记（仅用于缓存路径，不发送给服务端）
        if request_fields:
            cache_params["__fields"] = "|".join(sorted(request_fields))
        cache_path = self._cache_path(api_name, cache_params)

        if use_cache:
            cached = self._read_cache(cache_path)
            if cached is not None:
                logger.debug(f"[缓存命中] {api_name} {params}")
                df = self._parse_response(cached)
                return self._select_fields(df, request_fields)

        body = {"api_name": api_name, "token": self.token, "params": params}
        if request_fields:
            body["fields"] = ",".join(request_fields)

        logger.info(f"[Tushare 请求] {api_name} {params}")
        resp = self._post_with_retry(body, api_name)

        if use_cache and resp.get("code") == 0:
            self._write_cache(cache_path, resp)

        df = self._parse_response(resp)
        return self._select_fields(df, request_fields)

    @staticmethod
    def _select_fields(df: pd.DataFrame, fields: list[str] | None) -> pd.DataFrame:
        if not fields or df.empty:
            return df
        cols = [c for c in fields if c in df.columns]
        return df[cols]

    def query_raw(self, api_name: str, params: dict | None = None) -> dict:
        """返回原始响应 dict（用于无 fields/items 结构的接口，如 trade_cal）。"""
        params = dict(params or {})
        cache_path = self._cache_path(api_name, params)
        cached = self._read_cache(cache_path)
        if cached is not None:
            logger.debug(f"[缓存命中] {api_name} {params}")
            return cached
        body = {"api_name": api_name, "token": self.token, "params": params}
        logger.info(f"[Tushare 请求] {api_name} {params}")
        resp = self._post_with_retry(body, api_name)
        if resp.get("code") == 0:
            self._write_cache(cache_path, resp)
        return resp

    @staticmethod
    def load_local_sample(path: Path | str) -> pd.DataFrame:
        """从本地 samples JSON 加载为 DataFrame（测试/离线用）。"""
        with Path(path).open(encoding="utf-8") as f:
            resp = json.load(f)
        client = TushareClient.__new__(TushareClient)
        return client._parse_response(resp)

    def get_latest_trade_date(self) -> str:
        """交易日锚定铁律：用 index_daily(000001.SH) 取最大 trade_date。

        禁止用 datetime.now() 判断"今天"。
        第四轮清单3 补充修复：固定参数的永久缓存会卡在旧交易日，必须直连。
        """
        df = self.query(
            "index_daily",
            {"ts_code": "000001.SH", "end_date": "20301231"},
            use_cache=False,
        )
        if df.empty:
            raise RuntimeError("无法获取 index_daily 000001.SH 行情")
        return str(df["trade_date"].max())


def _is_rate_limit_msg(msg: str) -> bool:
    """判断 Tushare 报错信息是否属于频率限制。"""
    if not msg:
        return False
    markers = ("每分钟", "频率", "limit", "频繁", "访问过快", "次数")
    return any(m in msg for m in markers)

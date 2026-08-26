"""缺席心跳（第四轮清单2）：healthchecks.io ping。

原理：告警不能只依赖进程自己活着——进程崩溃/断电 = 静默失联。
    - 巡检开始 → ping {url}/start
    - 巡检成功 → ping {url}
    - 巡检异常 → ping {url}/fail
    healthchecks.io 后台配置"每日 17:30 前未收到成功 ping → 邮件告警"，
    进程死了也能收到通知。

配置（config/settings.yaml）：
    heartbeat:
      healthchecks_url: ""     # 空=不 ping（默认，零侵入）
"""

from __future__ import annotations

import requests
from loguru import logger


def ping(url: str, suffix: str = "") -> bool:
    """GET {url}{suffix}。失败仅 warning，不影响主流程。

    Args:
        url: healthchecks ping URL（如 https://hc-ping.com/<uuid>）
        suffix: ""（成功）/ "/start"（开始）/ "/fail"（失败）
    """
    if not url:
        return False
    try:
        resp = requests.get(f"{url.rstrip('/')}{suffix}", timeout=10)
        ok = resp.status_code == 200
        logger.debug(f"[心跳] ping {suffix or 'ok'} → {resp.status_code}")
        return ok
    except Exception as e:
        logger.warning(f"[心跳] ping 失败: {e}")
        return False


class Heartbeat:
    """巡检心跳封装（start/success/fail）。url 空=全部 no-op。"""

    def __init__(self, url: str = ""):
        self.url = url or ""

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def start(self) -> None:
        """巡检开始。"""
        ping(self.url, "/start")

    def success(self) -> None:
        """巡检成功完成。"""
        ping(self.url)

    def fail(self) -> None:
        """巡检异常退出。"""
        ping(self.url, "/fail")

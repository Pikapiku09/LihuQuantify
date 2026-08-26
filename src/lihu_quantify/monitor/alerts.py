"""告警器：微信（Server酱）/ 邮件（SMTP）/ 控制台。

第四轮清单1：EmailAlerter（smtplib + SSL，QQ/163 授权码）。
    - 所有 alert_* 业务方法同时走 Server酱 + 邮件双通道
    - send_daily_digest：巡检完成后发摘要邮件
    - 邮件发送失败不影响主流程（仅 warning 日志）

触发场景（docs/ARCHITECTURE.md §10.3）：
    - Checklist 拒绝（风控拦截）
    - 止损触发
    - 连亏 3 笔停手
    - API 异常
    - 巡检完成摘要（邮件）

配置（config/settings.yaml）：
    alert:
      serverchan_key: ""            # 空=不推送，仅控制台
      email:
        enabled: true
        smtp_host: "smtp.qq.com"
        smtp_port: 465
        username: "xxx@qq.com"
        auth_code: ""                # 授权码；.env: LIHU_ALERT__EMAIL__AUTH_CODE 覆盖
        to: ["xxx@qq.com"]
        send_daily_digest: true
"""

from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

import requests
from loguru import logger

SERVERCHAN_API = "https://sctapi.ftqq.com/{key}.send"

# 告警级别
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

_LEVEL_PREFIX = {"info": "✅", "warn": "⚠️", "error": "🚨"}


class EmailAlerter:
    """SMTP 邮件通道（第四轮清单1）。独立类便于测试与复用。"""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        auth_code: str,
        to: list[str],
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.auth_code = auth_code
        self.to = to or []

    @property
    def ready(self) -> bool:
        """配置完备性（host/user/auth_code/to 齐全）。"""
        return bool(self.smtp_host and self.username and self.auth_code and self.to)

    def send(self, subject: str, body: str) -> bool:
        """发送邮件。失败返回 False（不抛异常，不影响主流程）。"""
        if not self.ready:
            logger.debug(f"[邮件] 配置不完整，跳过: {subject}")
            return False
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = formataddr(("LihuQuantify", self.username))
            msg["To"] = ", ".join(self.to)
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15) as srv:
                srv.login(self.username, self.auth_code)
                srv.sendmail(self.username, self.to, msg.as_string())
            logger.debug(f"[邮件] 已发送: {subject} → {self.to}")
            return True
        except Exception as e:
            logger.warning(f"[邮件] 发送失败: {subject} | {e}")
            return False


class Alerter:
    """统一告警出口：Server酱微信 + 邮件 + 控制台/日志。"""

    def __init__(
        self,
        serverchan_key: str = "",
        enabled: bool = True,
        email: Optional[EmailAlerter] = None,
    ):
        """
        Args:
            serverchan_key: Server酱 SendKey（空则只走控制台）
            enabled: 总开关（关闭则完全静默，测试用）
            email: 邮件通道（None=未配置；配置见 alert.email 段）
        """
        self.serverchan_key = serverchan_key
        self.enabled = enabled
        self.email = email
        # 告警历史（去重 + 供报告读取）
        self.history: list[dict] = []

    def send(
        self,
        title: str,
        detail: str = "",
        level: str = LEVEL_INFO,
    ) -> bool:
        """发送告警（控制台 + Server酱 + 邮件）。返回是否成功（至少控制台成功即 True）。"""
        if not self.enabled:
            return False
        record = {"title": title, "detail": detail, "level": level}
        self.history.append(record)

        # 控制台 + 日志（永远执行）
        tag = {"info": "INFO", "warn": "WARN", "error": "ERROR"}.get(level, "INFO")
        logger.log("WARNING" if level != LEVEL_INFO else "INFO",
                   f"[告警:{tag}] {title} {('| ' + detail) if detail else ''}")
        print(f"[告警:{tag}] {title}" + (f" | {detail}" if detail else ""))

        # Server酱微信推送
        if self.serverchan_key:
            self._push_serverchan(title, detail)
        # 邮件通道（第四轮清单1：仅 warn/error 即时发；info 类进每日摘要）
        if self.email is not None and level in (LEVEL_WARN, LEVEL_ERROR):
            subject = f"{_LEVEL_PREFIX.get(level, '')} LihuQuantify: {title}"
            self.email.send(subject, detail or title)
        return True

    def _push_serverchan(self, title: str, detail: str) -> bool:
        """Server酱推送（失败不影响主流程）。"""
        try:
            resp = requests.post(
                SERVERCHAN_API.format(key=self.serverchan_key),
                data={"title": title[:32], "desp": detail},
                timeout=10,
            )
            data = resp.json()
            if data.get("code") == 0:
                logger.debug(f"Server酱推送成功: {title}")
                return True
            logger.warning(f"Server酱推送失败: {data.get('message')}")
        except Exception as e:
            logger.warning(f"Server酱推送异常: {e}")
        return False

    # ===== 每日摘要（第四轮清单1：巡检完成后邮件） =====

    def send_daily_digest(self, summary: dict) -> bool:
        """每日巡检摘要邮件（trade_date/市场状态/信号数/成交/拦截/权益/报告路径）。"""
        if self.email is None:
            return False
        executed = summary.get("executed", 0)
        rejected = summary.get("rejected", 0)
        executed_n = len(executed) if isinstance(executed, (list, tuple)) else executed
        rejected_n = len(rejected) if isinstance(rejected, (list, tuple)) else rejected
        lines = [
            f"基准日: {summary.get('trade_date', '-')}",
            f"市场状态: {summary.get('market_state', '-')}",
            f"信号: {summary.get('signals', 0)} 个 | 成交: {executed_n} 笔"
            f" | 拦截: {rejected_n} 个",
            f"总资产: {summary.get('total_asset', 0):,.0f}",
            f"报告: {summary.get('report', '-')}",
            "",
            "以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。",
        ]
        subject = f"📊 LihuQuantify 巡检摘要 {summary.get('trade_date', '')}"
        return self.email.send(subject, "\n".join(lines))

    # ===== 业务告警便捷方法 =====

    def alert_checklist_reject(self, ts_code: str, reasons: str) -> bool:
        """Checklist 拒绝（风控拦截）。"""
        return self.send(
            f"风控拦截: {ts_code}",
            f"拒绝原因: {reasons}",
            level=LEVEL_WARN,
        )

    def alert_stop_loss(self, ts_code: str, price: float, stop_price: float) -> bool:
        """止损触发。"""
        return self.send(
            f"止损触发: {ts_code}",
            f"现价 {price:.2f} ≤ 止损线 {stop_price:.2f}，已挂卖出",
            level=LEVEL_WARN,
        )

    def alert_halt(self, ts_code: str, until) -> bool:
        """连亏 3 笔停手。"""
        return self.send(
            f"连亏停手: {ts_code}",
            f"该票连亏 3 笔，停手至 {until}",
            level=LEVEL_ERROR,
        )

    def alert_api_error(self, api: str, msg: str) -> bool:
        """API 异常。"""
        return self.send(
            f"接口异常: {api}",
            msg[:200],
            level=LEVEL_ERROR,
        )

    def alert_bought(self, ts_code: str, volume: int, price: float, stop: float) -> bool:
        """买入成交（含止损线，便于人工复核）。"""
        return self.send(
            f"买入成交: {ts_code}",
            f"{volume}股 @ {price:.2f}，止损线 {stop:.2f}（先写止损再点买入 ✓）",
            level=LEVEL_INFO,
        )

    def alerts_since(self, level: Optional[str] = None) -> list[dict]:
        """读取告警历史（报告用）。"""
        if level is None:
            return self.history
        return [h for h in self.history if h["level"] == level]


def build_email_alerter(email_cfg) -> Optional[EmailAlerter]:
    """从配置构建 EmailAlerter（enabled=false 或配置不全 → None）。

    Args:
        email_cfg: settings.alert.email（EmailAlertConfig）
    """
    if not getattr(email_cfg, "enabled", False):
        return None
    alerter = EmailAlerter(
        smtp_host=email_cfg.smtp_host,
        smtp_port=email_cfg.smtp_port,
        username=email_cfg.username,
        auth_code=email_cfg.auth_code,
        to=list(email_cfg.to or []),
    )
    if not alerter.ready:
        logger.warning("[邮件] enabled=true 但配置不全（host/username/auth_code/to），邮件通道未启用")
        return None
    return alerter

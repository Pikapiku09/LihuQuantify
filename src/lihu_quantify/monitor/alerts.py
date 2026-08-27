"""告警器：微信（Server酱）/ 邮件（SMTP）/ 控制台。

第四轮清单1：EmailAlerter（smtplib + SSL，QQ/163 授权码）。
第五轮（邮件通知优化）：每交易日仅一封综合日报邮件。
    - WARN/INFO 级告警（风控拦截/止损/停手等）不再即时发邮件，
      全部记入当日告警历史，由日报统一呈现（旧机制每日几十封碎片邮件）
    - 仅 ERROR（API 异常等系统故障）保留即时邮件（日报发不出时的兜底通知）
    - send_daily_report：巡检完成后发结构化 HTML 综合日报
      （账户总览/当前持仓/今日操作/盈亏分析/风险提示，见 monitor/daily_report.py）
    - 邮件发送失败不影响主流程（仅 warning 日志）

触发场景（docs/ARCHITECTURE.md §10.3）：
    - Checklist 拒绝（风控拦截）→ 记录进日报
    - 止损触发 → 记录进日报
    - 连亏 3 笔停手 → 记录进日报
    - API 异常 → 即时邮件（ERROR）+ 记录进日报
    - 巡检完成综合日报（邮件，每日一封）

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
        send_daily_digest: true      # 每日综合日报邮件（HTML）
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

    def send(self, subject: str, body: str, html: bool = False) -> bool:
        """发送邮件。失败返回 False（不抛异常，不影响主流程）。

        Args:
            subject: 主题
            body: 正文（html=True 时为 HTML 内容）
            html: 是否 HTML 邮件（日报用）
        """
        if not self.ready:
            logger.debug(f"[邮件] 配置不完整，跳过: {subject}")
            return False
        try:
            msg = MIMEText(body, "html" if html else "plain", "utf-8")
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
        # 邮件通道（第五轮：仅 ERROR 系统故障即时发；WARN/INFO 进每日日报，
        # 确保每交易日只发一封综合简报，避免碎片邮件轰炸）
        if self.email is not None and level == LEVEL_ERROR:
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

    # ===== 每日综合日报（第五轮：巡检完成后邮件，每日一封） =====

    def send_daily_report(self, summary: dict) -> bool:
        """每日综合日报邮件（HTML 结构化，见 monitor/daily_report.py）。

        模块：账户总览 / 当前持仓 / 今日操作（买入·卖出·拒绝）/ 盈亏分析 /
        市场与风险提示。summary 为 DailyScanner._scan_impl 的扩展输出。
        """
        if self.email is None:
            return False
        from .daily_report import build_daily_report_email

        subject, html = build_daily_report_email(summary or {})
        return self.email.send(subject, html, html=True)

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
        """连亏 3 笔停手。

        修复3（第五轮清单）：ERROR→WARN。ERROR 会即时发邮件，与"每交易日
        仅一封日报"冲突；降为 WARN 后归入日报"市场与风险提示"区呈现。
        """
        return self.send(
            f"连亏停手: {ts_code}",
            f"该票连亏 3 笔，停手至 {until}",
            level=LEVEL_WARN,
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

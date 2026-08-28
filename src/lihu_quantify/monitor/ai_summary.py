"""AI 收盘总结（第八轮清单）：LLM 生成当日日报总结段（纯展示层）。

硬边界（docs/第八轮清单_AI收盘总结_for_Trae.md）：
    - 只读分析：输出仅追加到 .md 报告与邮件日报展示，绝不参与信号/下单/
      止损/风控决策——调用点位于巡检主体（信号→闸门→下单→止损→汇总）
      全部完成之后；
    - 静默降级：未配置 / 超时 / 失败 / 空输出一律返回 None，巡检与报告照常，
      仅日志一条 warning；
    - 回测零 LLM：本模块仅被 monitor.scheduler（纸面/实盘巡检）调用，
      回测与纸面撮合引擎不 import 本模块，回测保持确定性。

协议：OpenAI 兼容 `POST {api_base}/chat/completions`（Bearer key，
messages/temperature/max_tokens）。换模型只改 settings.yaml 的
ai_summary.api_base / model。api_key 不入 yaml/git，仅经 .env 注入：
    LIHU_AI_SUMMARY__API_KEY=sk-xxx（pydantic env 嵌套覆盖）
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

import requests
from loguru import logger

from ..config import AiSummaryConfig

SYSTEM_PROMPT = """你是 A 股量化交易系统的日报总结助手。规则：
1. 只基于给定的真实数据做客观总结，禁止编造任何数据；
2. 不预测未来涨跌，不输出"建议买入/卖出"类指令；
3. 用简洁中文输出，总长 250 字以内，分四段：今日市场与账户 / 持仓点评 / 今日操作回顾 / 风险提示；
4. 结尾固定加一句"以上内容由 AI 自动生成，仅供参考，不构成投资建议。"
"""


def _fmt_money(v, sign: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+,.0f}" if sign else f"{v:,.0f}"


def _fmt_pct(v, sign: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


def _fmt_price(v) -> str:
    """价格两位小数（股价口径，区别于金额的整元）。"""
    if v is None:
        return "-"
    return f"{v:,.2f}"


def _build_prompt(summary: dict) -> str:
    """rich summary（scheduler._build_daily_summary）→ 中文模板 prompt。

    复用既有数据，零新增取数：市场状态/过滤、账户、持仓明细、
    今日操作（买入/卖出/拒绝）、风险（待执行止损/停手/告警）。
    """
    lines: list[str] = []

    # 1. 市场与过滤
    scale = summary.get("entry_scale", 1.0)
    if scale == 0.0:
        filter_mode = "block（今日禁止开新仓）"
    elif scale == 0.5:
        filter_mode = "reduce（今日新仓仓位减半）"
    else:
        filter_mode = "off（正常开仓）"
    lines.append(f"交易日：{summary.get('trade_date', '-')}；"
                 f"市场状态：{summary.get('market_state', '-')}；市场过滤：{filter_mode}")

    # 2. 账户
    total = summary.get("total_asset", 0) or 0
    cash = summary.get("cash", 0) or 0
    init_cap = summary.get("init_capital") or 0
    cum_ret = (total - init_cap) / init_cap if init_cap > 0 else None
    prev = summary.get("prev_total_asset")
    day_pnl = (total - prev) if (prev is not None and prev > 0) else None
    acc = (f"账户：总资产 {_fmt_money(total)}，现金 {_fmt_money(cash)}，"
           f"累计收益率 {_fmt_pct(cum_ret)}")
    if day_pnl is not None:
        acc += f"，今日盈亏 {_fmt_money(day_pnl, sign=True)}"
    lines.append(acc)

    # 3. 持仓明细
    positions = summary.get("positions") or []
    if positions:
        lines.append("持仓明细：")
        for p in positions:
            stop = (f"止损线 {_fmt_price(p.get('stop_price'))}"
                    if p.get("stop_price") else "止损线未登记")
            lines.append(
                f"- {p.get('ts_code')} {p.get('name') or ''}："
                f"成本 {_fmt_price(p.get('cost'))}，现价 {_fmt_price(p.get('price'))}，"
                f"浮盈亏 {_fmt_money(p.get('float_pnl'), sign=True)}"
                f"（{_fmt_pct(p.get('float_pnl_pct'))}），{stop}，"
                f"占比 {_fmt_pct(p.get('weight'), sign=False)}"
            )
    else:
        lines.append("持仓：当前空仓")

    # 4. 今日操作
    executed = summary.get("executed") or []
    sells = summary.get("sells_today") or []
    rejected = summary.get("rejected") or []
    lines.append(f"今日操作：买入 {len(executed)} 笔，卖出 {len(sells)} 笔，被拒信号 {len(rejected)} 个")
    for e in executed:
        lines.append(f"- 买入 {e.get('ts_code')} {e.get('name') or ''} "
                     f"@{_fmt_price(e.get('price'))} × {e.get('volume', 0):,} 股")
    for t in sells:
        lines.append(f"- 卖出 {t.get('ts_code')} {t.get('name') or ''} "
                     f"@{_fmt_price(t.get('price'))} × {t.get('volume', 0):,} 股，"
                     f"实现盈亏 {_fmt_money(t.get('pnl'), sign=True)}（{t.get('reason', '')}）")
    if rejected:
        reasons = Counter(
            r for x in rejected for r in (x.get("reasons") or []) if r)
        if reasons:
            top = "；".join(f"{k}（{v} 次）" for k, v in reasons.most_common(3))
            lines.append(f"- 主要拒绝原因：{top}")

    # 5. 风险
    pending = summary.get("pending_stops") or []
    halted = summary.get("halted_codes") or {}
    alerts = summary.get("alerts") or []
    if pending:
        items = "、".join(f"{p.get('ts_code')} {p.get('name') or ''}"
                          f"（{p.get('volume', 0):,} 股 @ {p.get('stop_price', 0):.2f}）"
                          for p in pending)
        lines.append(f"风险：待执行止损 {len(pending)} 个（次日开盘价执行）——{items}")
    else:
        lines.append("风险：无待执行止损")
    if halted:
        lines.append("连亏停手中：" + "、".join(
            f"{c}（至 {u}）" for c, u in halted.items()))
    if alerts:
        warn = sum(1 for a in alerts if a.get("level") in ("warn", "error"))
        titles = "；".join(a.get("title", "") for a in alerts[:3])
        lines.append(f"当日告警 {len(alerts)} 条（其中警告/错误 {warn} 条）：{titles}")

    lines.append("请按系统指令，基于以上真实数据生成今日收盘总结。")
    return "\n".join(lines)


def build_ai_summary(summary: dict, cfg, api_key) -> Optional[str]:
    """调用 LLM 生成当日收盘总结。

    未配置 / 超时 / 失败 / 空输出 → None（静默降级，仅 warning 日志），
    巡检主流程不受任何影响。cfg 非 AiSummaryConfig（如测试 stub）时
    直接返回 None——保证零侵入。
    """
    if not isinstance(cfg, AiSummaryConfig) or not cfg.enabled:
        return None
    if not api_key:
        logger.warning("[AI总结] 未配置 api_key（.env: LIHU_AI_SUMMARY__API_KEY），跳过")
        return None
    prompt = _build_prompt(summary)
    try:
        r = requests.post(
            f"{cfg.api_base.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,   # 推理模型 reasoning 计入配额，600 会被耗尽
            },
            timeout=cfg.timeout,
        )
        r.raise_for_status()
        msg = (r.json()["choices"][0].get("message") or {})
        # Mimo v2.5 为推理模型：reasoning_content 单列，reasoning 也计入
        # max_tokens——600 会被推理耗尽导致 content 为空（finish_reason=length），
        # 故放宽到 2000（每日一调，成本可忽略）；max_chars 只截最终展示文本。
        text = (msg.get("content") or "").strip()
        if not text:
            logger.warning(f"[AI总结] 模型返回空内容"
                           f"（finish_reason={r.json()['choices'][0].get('finish_reason')}），跳过")
        return text[: cfg.max_chars] or None
    except Exception as e:
        logger.warning(f"[AI总结] 生成失败: {e}")
        return None

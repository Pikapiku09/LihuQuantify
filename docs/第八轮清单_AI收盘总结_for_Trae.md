# 第八轮清单：AI 收盘总结（for Trae）

> 项目：LihuQuantify
> 定位：**纯报告层功能**——每日巡检完成后，用便宜的 LLM 把当日数据总结成一段中文收盘分析，追加到 .md 报告和 HTML 日报邮件。
> **硬边界（必须遵守）**：
> - **只读分析**：AI 输出仅用于展示，绝不参与任何信号生成/下单/止损/风控决策；
> - **冻结期安全**：不改任何策略参数、风控规则、执行顺序、信号逻辑——因此可以在 100 笔冻结期内上线；
> - **静默降级**：API 超时/失败/未配置时，巡检照常完成，仅报告里无 AI 总结段落，绝不阻断主流程。

---

## 1. 模型选型（便宜、OpenAI 兼容）

| 模型 | 接口 | 成本参考 |
|---|---|---|
| **DeepSeek `deepseek-chat`（推荐，首选）** | `https://api.deepseek.com`（OpenAI 兼容） | 约 ¥1/百万输入，每日总结 ≈ 0.002 元 |
| 通义 Qwen `qwen-turbo` | DashScope OpenAI 兼容端点 | 廉价 |
| 智谱 `glm-4-flash` | `https://open.bigmodel.cn/api/paas/v4` | 免费额度 |
| Mimo `mimo-V2.5-pro`（推荐） | `https://api.xiaomimimo.com/v1` | 自用api |

统一按 OpenAI 兼容协议实现（`POST {base}/chat/completions`，Bearer key，`messages`/`temperature`/`max_tokens`），后续换模型只改配置。

## 2. 配置（settings.yaml + config.py + .env）

```yaml
# settings.yaml 新增
ai_summary:
  enabled: false                 # 填好 key 后开 true；默认关=零侵入
  api_base: "https://api.deepseek.com"
  model: "deepseek-chat"
  timeout: 20                    # 秒
  max_chars: 300                 # 总结长度上限
```

```python
# config.py 新增 AiSummaryConfig（字段同上），api_key 不入 yaml：
#   .env: LIHU_AI_SUMMARY__API_KEY=sk-xxx（pydantic env 覆盖，绝不进 git）
```

## 3. 新模块 `src/lihu_quantify/monitor/ai_summary.py`

```python
def build_ai_summary(summary: dict, cfg: AiSummaryConfig, api_key: str) -> str | None:
    """调用 LLM 生成当日收盘总结。失败/超时/未配置返回 None。"""
    if not (cfg.enabled and api_key):
        return None
    prompt = _build_prompt(summary)          # 结构化数据 → 中文模板
    try:
        r = requests.post(f"{cfg.api_base}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}"},
                          json={
                              "model": cfg.model,
                              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                           {"role": "user", "content": prompt}],
                              "temperature": 0.3,
                              "max_tokens": 600,
                          }, timeout=cfg.timeout)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        return text[: cfg.max_chars] or None
    except Exception as e:
        logger.warning(f"[AI总结] 生成失败: {e}")
        return None
```

### SYSTEM_PROMPT（关键：约束为"总结"而非"荐股"）

```
你是 A 股量化交易系统的日报总结助手。规则：
1. 只基于给定的真实数据做客观总结，禁止编造任何数据；
2. 不预测未来涨跌，不输出"建议买入/卖出"类指令；
3. 用简洁中文输出，分四段：今日市场与账户 / 持仓点评 / 今日操作回顾 / 风险提示；
4. 结尾固定加一句"以上内容由 AI 自动生成，仅供参考，不构成投资建议。"
```

### _build_prompt 注入的数据（复用现有 rich summary，无需新取数）

- 交易日 / 市场状态（上涨·震荡·下跌）/ 过滤模式（block/reduce/off）
- 账户：总资产 / 现金 / 累计收益率 / 今日盈亏（prev_total_asset 口径）
- 持仓明细（表）：代码 / 名称 / 成本 / 现价 / 浮盈亏 / 盈亏% / 止损线
- 今日操作：买入笔数+标的 / 卖出笔数+标的+实现盈亏 / 被拒信号数+主要拒绝原因（如"铁律1：止损价不低于买入价"、资金不足）
- 风险：待执行止损列表 / 停手票 / 当日告警

## 4. 集成点（三处，全部为"追加展示"）

1. `scheduler._scan_impl`：在 `_build_daily_summary(...)` 之后调用
   `summary["ai_summary"] = build_ai_summary(summary, cfg, api_key)`（结果随 last_scan.json 自动持久化）；
2. `monitor/report.py`（.md 报告）：新增"七、AI 收盘总结"节，`ai_summary=None` 时整节省略；
3. `monitor/daily_report.py`（HTML 邮件）：在"五、市场与风险提示"之后加 AI 总结块（灰底引用样式），None 时省略。

## 5. 验收标准

1. `.env` 填 key + `ai_summary.enabled: true` 后，16:30 巡检的 `.md` 报告和邮件含 AI 总结段落（四段式、带免责声明）；
2. **AI 开关前后，信号数/成交/拦截完全一致**（证明纯展示、不影响决策——用同一交易日分别跑 enabled=false/true 对比 `last_scan.json` 的 signals/executed/rejected）；
3. 断网或 API key 错误时：巡检正常完成，报告无 AI 段，日志有 `[AI总结] 生成失败` warning，无崩溃；
4. `api_key` 只存在于 .env（`git check-ignore .env` 生效），仓库与镜像不含 key。

## 6. 明确不做

- ❌ 不让 AI 参与选股/评分/下单/止损任何决策；
- ❌ 不把 invest_run 的多角色流水线搬进来（那是重研究层，本清单只是轻量总结层）；
- ❌ 不在回测/纸面引擎里引入任何 LLM 调用（回测必须确定性）。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

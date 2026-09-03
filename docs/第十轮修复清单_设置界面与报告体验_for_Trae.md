# 第十轮修复清单：设置界面与报告体验（for Trae）

> 项目：LihuQuantify
> 背景：第九轮（热力图快照数据源/API health/AI 卡片）已落地。本轮为：①系统设置界面（API 配置+开关+建议+确认）；②Markdown 表格渲染修复；③每日巡检 AI 简要报告。
> 硬约束：展示层与配置层改造，**不改变任何交易决策逻辑**；冻结期纪律由设置界面"只读锁"强制体现。

---

## 一、系统设置界面（需求 1/2/3：API 配置 + 开关 + 建议 + 确认）

### 1.1 后端：设置读写 API

1. `web/server.py` 新增：
   - `GET /api/settings`：返回当前配置 + **元数据表**（每个开关的 `label` 中文名 / `description` 说明 / `recommendation` 建议 / `editable` 是否可改 / `group` 分组）；
   - `POST /api/settings`：仅接受带 `"confirm": true` 的请求（防误触），写文件用**原子写**（临时文件+替换），并追加一条审计记录到 `data/settings_history.jsonl`（时间/改动项/旧值→新值）。
2. **compose 挂载补充**：web 服务 volumes 增加 `- ./config:/app/config`（目前 web 只挂了 data/outputs，无法写 settings.yaml）。
3. **API key 路径统一**：新增 `data/secrets.json`（两容器都挂载 data/），存 `{"ai_summary_api_key": "...", "email_auth_code": "..."}`；`config.py` 的 ai_summary.api_key 解析顺序改为：**secrets.json → 环境变量 → 空**。设置界面填 key → 写入 secrets.json → 调度器下次巡检即生效，**不再依赖 compose env 映射**（当前 `.env` 的 key 可能根本没进容器，这也是"API 未配置完成"的原因之一）。
4. **配置热生效**：当前 `get_settings` 是 lru_cache + DailyScanner 启动时捕获 settings，改 settings.yaml 对**运行中的调度器不生效**。改法：`scan()` 开头重新加载 settings（`get_settings.cache_clear()` 或每次新建），使设置修改在**下一次巡检（16:30 或手动 --run-now）生效**，无需重启容器。

### 1.2 前端：设置页

1. 侧边栏新增"**系统设置**"页，按组展示（通知/AI 总结/热力图/资金控制/策略与风控），每组内每个开关显示：中文名 + 说明 + **建议徽章**（建议开启/建议关闭/冻结期锁定）+ 当前值；
2. 交互流程：改开关 → 点"**预览变更**"（显示 diff：改动项 旧值→新值 + 风险提示）→ 点"**确认应用**"（POST confirm=true）→ 成功提示"已保存，将于下次巡检生效"；
3. **冻结期强制**：策略参数与风控参数（`strategy.*`、`risk.*`、`universe.*`）在设置页**只读锁定**（锁图标 + "100 笔冻结期内禁止修改"）；`capital_guard.enabled/top_n_enabled` 可改但显示 ⚠️ 强警告"会改变开仓逻辑，冻结期不建议开启"；
4. API key 输入框：掩码显示（sk-***），"测试连接"按钮（调一次极小请求验证 key 有效性）。

### 1.3 建议文案（元数据示例）

| 设置 | 建议文案 |
|---|---|
| ai_summary.enabled | ✅ 建议开启（纯展示层，不影响交易，每日约 0.002 元） |
| heatmap.enabled | ✅ 建议开启（纯展示，无副作用） |
| email.enabled | ✅ 建议开启（配置授权码后，事件+日报通知） |
| heartbeat.healthchecks_url | ✅ 建议配置（进程死亡缺席告警，免费） |
| capital_guard.enabled / top_n_enabled | ⚠️ 冻结期建议关闭（改变开仓节奏/选择逻辑，100 笔评审后再评估） |
| strategy.* / risk.* / universe.* | 🔒 冻结期锁定，只读（100 笔评审后解锁） |

---

## 二、Markdown 表格渲染修复（需求 4）

**现状**：`index.html` 的 `mdToHtml()`（第 717 行起）是简化版正则转换，**完全不支持表格**（`| a | b |` 会原样显示成混乱的竖线文本）——巡检报告里大量表格因此显示混乱。

**改法**：
1. 将 `marked.min.js` 下载放入 `web/static/vendor/`（与 echarts 同目录，离线可用），index.html 引入；
2. `mdToHtml()` 改为：先对原始 md 做 HTML 转义（防注入），再 `marked.parse()` 渲染（支持表格/GitHub 风格）；
3. `.report-view` 补表格样式：边框、斑马纹、表头底色、溢出滚动（现有部分样式保留增强）；
4. 保留"返回列表"按钮与加载态行为不变。

**验收**：报告页任意含表格的 `.md` 渲染为规整表格（对齐、边框、无竖线残迹）；含 `<script>` 的恶意文本只显示文本不执行。

---

## 三、每日巡检 AI 简要报告（需求 5：可替代但不建议完全替代）

**设计原则**：AI 总结可能失败/不稳定、明细表格有审计价值，所以**不直接替换**，做"双层报告"：
- **简要版（门面）** = 日期/市场状态 + 4 个关键数字（总资产/累计收益率/今日盈亏/持仓数）+ AI 总结正文 + 今日操作一句话；
- **详细版（审计）** = 现有 `.md` 报告原样保留。

**改法**：
1. 新增**规则版简报兜底**（确定性，AI 失败时永不为空）：`scheduler._build_daily_summary` 末尾生成
   `brief_rule = "今日巡检完成：{signals} 信号，{executed} 成交，{rejected} 拦截；市场{state}（{filter}）；总资产 {total}，累计 {ret}，今日 {day}；持仓 {n} 只。"`；
2. `summary["brief"] = ai_summary or brief_rule`（AI 成功用 AI 版，失败自动回退规则版），随 last_scan.json 持久化；
3. **看板首页**："AI 收盘总结"卡片改为"**今日简报**"卡片，展示 `brief`（含日期徽章）；
4. **邮件**：HTML 日报顶部插入"今日简报"块（AI 版或规则版），明细表格下移；
5. （可选，默认关）配置 `report_mode: full|brief`——`brief` 时 `.md` 报告只写简报段落。**默认 full（不替代）**。

**验收**：AI 未配置时首页/邮件显示规则版简报（不为空）；AI 配置后显示 AI 版；详细 `.md` 报告不变。

---

## 实施顺序建议

**二（表格渲染，最小改动）→ 三（简报）→ 一（设置界面，最大改动）**

## 回归基线（不可破坏）

- 交易/风控/策略逻辑冻结；capital_guard 保持 false；AI 保持"纯展示"边界；
- 热力图快照数据源（第九轮）与持仓/指标/交易（第六轮）不回归；
- settings.yaml 原子写 + 审计记录，任何设置变更可追溯。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

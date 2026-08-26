# 第四轮清单：部署与运维改造（for Trae）

> 项目：LihuQuantify
> 背景：三轮代码修复完成，进入**模拟盘无人值守长期运行**阶段。本轮不改策略、不改风控，只做"让系统 7×24 可靠运行、出事了有人知道"的运维改造。
> 部署目标：**群晖 DS918+（Docker 容器，7×24 开机）**；未来 QMT 实盘迁回 Windows（见 §10 说明）。
> **硬约束**：策略/风控逻辑冻结（三轮成果不许动）；回测/模拟盘/实盘三口径不变；所有新增配置项必须有默认值，旧配置零修改可继续跑。

---

## 1. 邮件推送（SMTP 事件告警）

**现状**：`monitor/alerts.py` 仅 Server酱（key 为空=仅控制台）。用户需要邮件。

**改法**：
1. `alerts.py` 增加 `EmailAlerter`：`smtplib` + SSL，支持 QQ/163 授权码登录；
2. `config/settings.yaml` 增加配置段：

```yaml
alert:
  serverchan_key: ""
  email:
    enabled: true
    smtp_host: "smtp.qq.com"        # 或 smtp.163.com
    smtp_port: 465
    username: "xxx@qq.com"
    auth_code: ""                   # 授权码，非登录密码（放 .env 用 LIHU_ALERT__EMAIL__AUTH_CODE 覆盖更安全）
    to: ["xxx@qq.com"]
    send_daily_digest: true         # 每日巡检摘要邮件（开）
```

3. 触发场景接入：巡检完成摘要、Checklist 拦截、止损触发/次日执行、连亏停手、API 错误、巡检异常（对应现有 `alert_*` 方法逐个体加邮件通道）。

**验收**：本地跑一次测试邮件；人为制造一次 Checklist 拦截 → 收到邮件；每日 16:30 巡检后收到摘要邮件。

---

## 2. 缺席心跳（进程死了也要通知）

**现状**：告警全部依赖进程自己活着。进程崩溃/断电 = 静默失联。

**改法（推荐 healthchecks.io，免费）**：
1. `run_scheduler.py` 巡检任务开始/结束时各 `curl` 一次 healthchecks ping URL（配置在 settings，空则不 ping）；
2. healthchecks.io 后台设置：**每天 17:30 前未收到完成 ping → 邮件告警**；
3. 备选（NAS 原生）：群晖"任务计划"每天 17:30 运行脚本检查 `outputs/reports/` 当天报告是否存在，不存在用 DSM 邮件通知。

**验收**：手动停掉 scheduler 进程，次日收到"巡检未完成"告警。

---

## 3. 巡检时点改为 16:30

**现状**：`daily_scan_cron: "30 15 * * 1-5"`（15:30 当日日线可能未出齐）。

**改法**：`config/settings.yaml` 改为 `"30 16 * * 1-5"`，注释写明原因（Tushare 当日日线 16:00 后完整）。

**验收**：16:30 巡检取到的"最新交易日"是当天（而非昨天）。

---

## 4. Docker 化（部署到 DS918+）

**改法**：
1. 新增 `Dockerfile`：`python:3.12-slim`，按 `pyproject.toml` 安装依赖（去重：matplotlib 等仅回测需要的可放 dev 层或全装，DS918+ 空间足够）；
2. 新增 `docker-compose.yml`：

```yaml
services:
  scheduler:
    build: .
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
      - LIHU_ALERT__EMAIL__AUTH_CODE=${AUTH_CODE}
    volumes:
      - ./data:/app/data          # duckdb + paper_state + 日志（持久化）
      - ./outputs:/app/outputs    # 报告/图表
      - ./config:/app/config
    command: python run_scheduler.py --mode paper
  web:
    build: .
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./outputs:/app/outputs
    command: python -m web.server
```

3. 注意：**同一份 data/outputs 卷由两个容器共享**（web 只读展示，scheduler 写）。

**验收**：在 DSM Container Manager 导入 compose 启动；**重启 NAS 后容器自动恢复且 paper_state 不变**；局域网浏览器打开 `http://<NAS_IP>:8000` 看板正常。

---

## 5. 时区与日期纪律

**现状**：APScheduler 用配置时区，但 `loguru` 时间戳、`date.today()`（月度复盘/停手判断）用系统时区。

**改法**：容器 `TZ=Asia/Shanghai`（已入 compose）；代码内"今天"判断优先用交易日锚定（已实现）；确认 `monthly_review` 的月末判定用交易日历（第三轮修复 D 的验收项）。

**验收**：容器内 `date` 显示北京时间；日志时间戳正确。

---

## 6. 幂等保护（防重复巡检）

**现状**：同一天 cron + 手动各跑一次 scan，`held` 检查能挡重复买入，但报告会生成两份、止损待执行队列可能重复登记。

**改法**：`DailyScanner.scan()` 开头读 `data/last_scan.json`（记录 trade_date），当日已巡检 → 直接跳过；新增 `--force` 参数强制重跑（人工补跑用）。

**验收**：同一天连跑两次 scan，第二次输出"已巡检，跳过"；`--force` 可重跑。

---

## 7. 日志轮转

**现状**：loguru 默认控制台输出，无文件落盘；无人值守需要留痕。

**改法**：loguru 增加文件 sink：`data/logs/lihu_quant_{time:YYYYMMDD}.log`，`retention="30 days"`，关键操作（买卖、止损登记/执行、巡检完成）打 INFO 以上。

**验收**：跑一天后日志文件生成；30 天前的旧日志自动删除。

---

## 8. 备份策略

**现状**：`paper_state.json` / `lihu_quant.duckdb` / 决策日志没有备份——模拟盘状态是**连续验证的命根子**。

**改法**：
1. NAS 侧：Hyper Backup 任务每日备份 `data/`（含 paper_state、duckdb、logs）与 `outputs/`、`docs/决策日志.md`；
2. 或容器内 cron：每日 17:00 把上述文件压缩拷贝到另一个共享文件夹（保留 30 份滚动）。

**验收**：恢复演练一次——删除 paper_state.json，从备份恢复，看板/巡检状态与删除前一致。

---

## 9. 安全边界

1. 看板只绑定 `127.0.0.1`（NAS 上需局域网访问时改绑内网 IP），**绝不端口映射到公网**；需要远程访问用 Tailscale/ZeroTier 组网；
2. `tushareMcp.json`（含 token）不进 git（已在 .gitignore），NAS 上目录权限收窄；
3. `.env` 存邮件授权码，不进 git（已有 `.env.example`）。

**验收**：git status 干净（无 token/.env）；外网无法访问 8000 端口。

---

## 10. NAS 部署文档 + QMT 迁移说明

**改法**：新增 `docs/DEPLOY_NAS.md`：
- DS918+（DSM 7）具体步骤：Container Manager → 项目 → 导入 compose → 启动；
- 目录映射表（宿主机 ↔ 容器）；
- 日常操作：如何手动跑一次巡检、如何看容器日志、如何从备份恢复；
- 故障排查：数据没更新 → 查 token/积分/限频；报告没生成 → 查日志；看板空白 → 查 web 容器卷挂载。

**QMT 迁移说明（写进文档）**：
- NAS 只跑**纸面**；未来接**实盘必须在 Windows**（国金 QMT 客户端 + xtquant 仅支持 Windows）；
- 迁移步骤：停止 NAS 容器 → 拷贝 `data/`、`outputs/` 回 Windows → Windows 上装依赖（或继续用 Docker Desktop）→ `qmt.enabled: true` 灰度小资金。策略代码零改动。

---

## 11.（可选）Windows 备用方案

**现状**：若短期内不上 NAS，电脑需保持开机。

**改法**：Windows"任务计划程序"创建开机自启任务（`python run_scheduler.py --mode paper`，触发器=启动时，条件=不要求用户登录即可运行），`docs/DEPLOY_WINDOWS.md` 说明步骤。

**验收**：重启电脑后任务自动拉起，无需手动登录。

---

## 实施顺序建议

**3（cron 16:30，一行）→ 1（邮件）→ 2（心跳）→ 6（幂等）→ 7（日志）→ 4/5（Docker+时区）→ 8/9（备份+安全）→ 10（部署文档）**

（1-3、6、7 可在 Windows 本机先行开发验证，4 起再上 NAS。）

## 回归基线与硬约束（累计四轮，均不可破坏）

- 策略/风控逻辑冻结：CherryClaw 三层过滤、Checklist 8 项、-8% 止损、MA10 收盘判定、移动止盈、连亏停手、市场过滤（reduce/block）、T+1、不补仓；
- 三口径不变：回测=次日开盘、模拟盘=次日开盘（待执行队列）、实盘=盘中条件单；
- 决策日志纪律：纸面运行期**冻结参数**，任何修改冲动进待办，满 100 笔或 3 个月再评审；
- 数据锚定铁律：所有"最新交易日"来自 index_daily 锚定，禁止用系统时间。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

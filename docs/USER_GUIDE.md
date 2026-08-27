# LihuQuantify 用户使用指南

> A 股日线级量化交易系统：Tushare 数据订阅 → 策略信号 → 风控闸门 → 回测验证 → MiniQMT 实盘下单
> 版本：v0.1 | 更新：2026-08-26

---

## 目录

1. [系统简介](#一系统简介)
2. [环境准备](#二环境准备)
3. [快速开始](#三快速开始)
4. [回测](#四回测)
5. [模拟盘 / 实盘](#五模拟盘--实盘)
6. [定时巡检调度](#六定时巡检调度)
7. [配置说明](#七配置说明)
8. [风控铁律说明](#八风控铁律说明重要)
9. [目录结构](#九目录结构)
10. [常见问题](#十常见问题)

---

## 一、系统简介

### 1.1 这是什么

LihuQuantify 是一套**端到端 A 股日线量化系统**，核心特点是把一套经过实战验证的交易铁律（止损纪律、仓位限制、频率控制）做成**代码级强制闸门**——任何买入信号必须通过 8 项 Checklist 检查，任一拒绝即拦截，不允许"手动跳过"。

```
数据层          策略层          风控层           回测/实盘       监控层
Tushare API →  CherryClaw  →  Checklist 8项 →  事件驱动引擎 →  APScheduler
DuckDB 落库    三层过滤       三档止损         MiniQMT下单    微信告警
缓存+增量      六维诊断       仓位/频率限制    模拟盘仿真      报告归档
```

### 1.2 策略体系（来源：dsh-invest-plugin 投研流水线）

| 策略 | 说明 |
|---|---|
| CherryClaw 三层过滤 | ① MA5 上穿 MA10 金叉（新鲜度≤3天）② 量比>1.0 + 实体占比≥40% + 收红 ③ 收盘贴近 MA5 + MA20 斜率向上 |
| 市场状态过滤 | 仅上证 20 日涨幅 ≥3%（上涨段）时开新仓，震荡/下跌段空仓等待 |
| 持仓离场 | 移动止盈（高水位回撤3%）/ 收盘破10日线 / -5%执行 / -8%强制止损 |

### 1.3 回测绩效基线（50 只主板股 × 2 年）

| 指标 | 数值 |
|---|---|
| 总收益率 | +33.50% |
| 年化收益率 | +15.51% |
| 最大回撤 | -13.19% |
| 胜率 | 53.78% |
| 盈亏比 | 1.36（铁律目标 >1 达标）|

> ⚠️ 收益集中于 2024H2-2025 牛市段；策略适用域为**上涨市**，震荡市靠市场过滤空仓规避。
> 所有输出仅供参考，不构成投资建议。

---

## 二、环境准备

### 2.1 软件要求

- Python **3.10+**（开发环境 3.12.10）
- Windows（MiniQMT 实盘仅支持 Windows）
- Tushare Pro 账号（需一定积分等级，日线接口需 120 分以上）

### 2.2 安装步骤

```powershell
# 1. 进入项目目录
cd E:\LihuQuantify

# 2. 创建虚拟环境（已创建可跳过）
python -m venv .venv

# 3. 安装依赖（国内建议用清华镜像）
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2.3 配置 Tushare Token

Token 文件位置：`E:\LihuQuantify\tushareMcp.json`（已配置则跳过）

支持两种格式：

**格式一：MCP JSON（当前使用）**
```json
{
    "mcpServers": {
        "tushareMcp": {
            "url": "https://api.tushare.pro/mcp/?token=你的token"
        }
    }
}
```

**格式二：纯文本**（单行 token 字符串）
```
你的token
```

Token 获取：登录 [tushare.pro](https://tushare.pro) → 个人中心 → 复制 token。

也可用环境变量覆盖（`.env` 文件）：
```
LIHU_TUSHARE_TOKEN=你的token
```

### 2.4 验证安装

```powershell
# 跑一次全量测试（应全部通过）
.\.venv\Scripts\python.exe -m pytest -q
```

预期输出：`91 passed`

---

## 三、快速开始

跑一次最小回测（4 只大票 × 半年，验证数据链路）：

```powershell
.\.venv\Scripts\python.exe run_backtest.py
```

预期看到：交易明细、权益曲线、绩效指标、止损执行率、市场分段统计。

自定义股票池：
```powershell
.\.venv\Scripts\python.exe run_backtest.py 600036.SH 601398.SH 600276.SH
```

---

## 四、回测

### 4.1 小样本回测（冒烟测试）

```powershell
python run_backtest.py
```

### 4.2 扩大样本回测（统计有效）

```powershell
# 50 只主板股 × 2 年（默认）
python run_full_backtest.py

# 100 只 × 3 年
python run_full_backtest.py --n 100 --years 3
```

输出包含：
- 交易明细（含每笔触发原因：移动止盈/破10日线/-5%执行）
- 绩效（夏普/卡玛/胜率按轮次/盈亏比/费用占比）
- 止损执行率（按 reason 如实分类）
- 市场分段统计（上涨/震荡/下跌段各自的胜率与盈亏）

### 4.3 参数网格搜索（调参）

```powershell
python scripts/grid_search.py --n 50 --years 2
```

- 网格：`close_to_ma5_max_dev` × `金叉新鲜度` × `追高阈值`（48 组）
- 前 2/3 时间段训练、后 1/3 验证（防过拟合）
- 产出：`outputs/grid_search_results.csv` + 验证结果 + 热力图

### 4.4 辅助分析

```powershell
# 网格结果热力图重绘 + 训练/验证段市场环境诊断
python scripts/analyze_grid.py
```

### 4.5 参数选择纪律（必须遵守）

> 以下三条来自三轮修复复盘的血泪教训，违反任何一条都会导致过拟合实盘亏损。

1. **最优在边界 → 不选，扩网格再观察**
   如果网格最优参数落在网格的边界值上（如 ma5_dev=0.1 恰是搜索范围最大值），
   说明真实最优可能在网格之外。此时该单点数字不可信，正确做法是扩大网格重跑，
   而不是直接采用边界值。当前实盘参数 (0.05, 3, 0.08) 选择的是稳健区域内的点，
   而非最优点（最优点仍在边界）。

2. **稳健区域 > 单点最优**
   一个参数点只有当"自身 + 全部一阶邻域"收益都为正时才算稳健（见
   `outputs/grid_v2_*_halt_robustness.md` 的稳健点统计）。单点最优很容易是
   噪声或幸存者偏差的产物；稳健区域内的参数即使不是最优，实盘表现也更可预期。

3. **每段数据只有一次验收权**
   任何一段历史数据一旦被用于调规则（选参、改过滤阈值、改池子），就永久失去
   作为"验收证据"的资格（防数据窥探）。改任何规则前先查 `docs/决策日志.md`
   确认目标数据段未被消费。当前 2023-01~2026-08 的三段数据（训练/验证/holdout）
   均已消费，新的参数决策只能用 2023 年前的历史数据或未来 live 纸面积累。

补充：所有网格/回测收益数字均含**幸存者偏差**（池按当前上市股票构建），
属乐观口径——横向比较参数相对优劣有效，绝对值不可当作实盘收益预期。

---

## 五、模拟盘 / 实盘

### 5.0 live 验证阶段（当前阶段）—— 观察指南与验收标准

> 三轮修复全部完成后，系统进入 **live 数据积累期**。本节定义观察方法与何时做决策。
> 背景：所有历史数据段（训练/验证/holdout）均已消费，**live 纸面数据是当前唯一干净的验证来源**。

**启动方式**：

```powershell
# 方式一：常驻调度（推荐，每个交易日 15:30 自动巡检 + 月末复盘）
python run_scheduler.py --mode paper --n 50

# 方式二：手动触发一次巡检（立即可见结果）
python run_scheduler.py --run-now --mode paper --n 50

# 同时开看板（可选，60 秒自动刷新）
python -m uvicorn web.server:app --host 127.0.0.1 --port 8000
```

**每天自动发生什么**（16:30 巡检流程）：
1. 拉最新行情 → 判断市场状态（000001.SH 20日涨幅）
2. 执行昨日登记的待执行止损（今日开盘价成交，交易记录含盈亏/原因）
3. 今日收盘判断：价格止损 / MA10 破位 / 移动止盈（高水位回撤3%）→ 登记明日执行
4. 扫描分层池信号 → Checklist 8 项闸门 → 买入+止损同时挂
5. 归档 rich 日报 `outputs/reports/YYYY-MM-DD.md`（六模块：账户总览/持仓/操作/盈亏/风险/铁律自检）+ 邮件日报 + 记录过滤统计

**观察要点（看板 http://127.0.0.1:8000）**：

| 观察项 | 位置 | 健康标准 |
|---|---|---|
| 总资产 = 现金 + 持仓市值 | 仪表盘 | source=snapshot（真实权益）|
| 真实绩效（非回测值） | 仪表盘指标卡 | 累计收益率/今日盈亏/浮动盈亏（红涨绿跌）|
| 持仓明细 | 仪表盘持仓表 | 名称/现价/市值/浮盈亏/盈亏%/止损线齐全 |
| 每笔持仓都有止损线 | 仪表盘持仓表 | 无 "-"（崩溃可 --rebuild 重建）|
| 交易盈亏与原因 | 仪表盘"近10笔交易" | 买入=策略理由，卖出=移动止盈/价格止损/MA10破位 |
| 铁律执行 | 巡检报告"铁律自检" | 止损执行率 100%、无向下补仓 |
| 市场过滤命中 | 月度复盘"过滤命中统计" | 累积用于升级/降级 reduce 决策 |

> 成交流水导出（人工核对）：`python scripts/dump_trades.py`（含盈亏/原因/持有天数，`-o x.csv` 导出）
> 注意：纸面移动止盈为"高水位回撤3%离场"，与回测统计口径存在已知差异
> （见 docs/决策日志.md "移动止盈口径差异" 节，100 笔样本后统一评估）。

**验收标准与决策时间表**：

| 积累量 | 可评估的决策 | 依据 |
|---|---|---|
| ~30 个交易日 | 系统稳定性（无崩溃/无漏单）| 日报连续性、状态文件完整性 |
| ≥50 笔轮次 | 初步胜率/盈亏比 vs 回测（58.9%/1.25）| 若严重偏离（胜率<45%）→ 停下检查 |
| ≥100 笔轮次 | **市场过滤升级评估**：reduce→block 还是维持 | 决策日志"证据等级"条款 |
| 1~3 个月 | 是否上实盘（QMT 小资金灰度）| 月度复盘连续达标 |

**统计纪律提醒**：live 积累期间**禁止**回头修改策略参数/池/过滤规则——否则这段
live 数据也被污染。任何调整先记 `docs/决策日志.md`。

### 5.1 模拟盘（默认，安全）

```powershell
python run_live.py                    # 默认 paper 模式
python run_live.py --mode paper --n 50
```

流程：扫描股票池 → 市场过滤 → CherryClaw 信号 → Checklist 8 项闸门 → OMS（买入单+止损单**同时**挂出）→ 止损监控。

### 5.2 崩溃恢复

程序异常中断后重启，从当前持仓重建止损登记：

```powershell
python run_live.py --rebuild
```

### 5.3 实盘（需 MiniQMT 环境）

**前置条件：**

1. 安装券商 MiniQMT 客户端（极简模式）并登录
2. 把 QMT 的 Python 库路径加入环境变量：
   ```
   PYTHONPATH = C:\你的QMT路径\bin.x64\Lib\site-packages
   ```
3. 修改 `config/settings.yaml`：
   ```yaml
   qmt:
     enabled: true                    # 实盘总开关（安全阀）
     path: "C:/你的QMT路径/userdata_mini"
     account: "你的资金账号"
   ```

**启动：**
```powershell
python run_live.py --mode live
```

**灰度路径（务必遵守）：**

```
模拟盘跑稳 2-4 周 → 小资金（总资产 5%）→ 观察 1 个月 → 逐步放量
```

### 5.4 实盘守护的铁律（代码级强制，无法绕过）

| 铁律 | 实现 | 行为 |
|---|---|---|
| 先写止损再点买入 | OMS `place_buy_with_stop()` | 止损价未给出/高于买入价 → **拒绝买入** |
| 绝不向下补仓 | OMS 铁律2 检查 | 持仓亏损时加仓请求 → **拒绝** |
| 成本-8% 无条件离场 | 止损监控 | 程序化条件单盘中轮询，跌破自动卖 |
| 单票 ≤25% / 板块 ≤40% | Checklist 闸门 | 超限 → 拒绝 |
| 同票月内 ≤3 次 / 连亏3笔停手 | FrequencyGuard | 超限 → 拒绝 |

---

## 六、定时巡检调度

### 6.1 启动调度器（常驻）

```powershell
# 模拟盘调度（默认）
python run_scheduler.py

# 启动时先跑一次巡检再进入定时
python run_scheduler.py --run-now --n 50

# 当日已巡检但需要强制重跑（幂等保护的人工补跑口）
python run_scheduler.py --run-now --force

# 实盘调度
python run_scheduler.py --mode live
```

调度规则：**周一至周五 16:30**（Tushare 当日日线 16:00 后完整；第四轮改，
原 15:30 可能取到昨日数据）自动巡检。
**幂等保护**：当日已巡检会自动跳过（防 cron+手动重复执行导致重复报告/重复
止损登记，状态记于 `data/last_scan.json`）。
**日志**：`data/logs/scheduler_YYYYMMDD.log`，每天轮转，保留 30 天。

### 6.2 每次巡检做什么

1. 取真实最新交易日（交易日锚定，不用系统时间）
2. 判断市场状态（上涨/震荡/下跌）
3. 扫描股票池出信号
4. 市场过滤：非上涨段不开新仓（持仓止损照常）
5. Checklist 8 项闸门逐信号检查
6. OMS 执行买入+止损同挂
7. 止损检查（收盘价模式）
8. 发告警、归档报告

### 6.3 查看报告

每日报告归档在 `outputs/reports/YYYY-MM-DD.md`，包含：账户概览、持仓与止损监控、信号与执行、Checklist 拒绝明细、告警记录、铁律自检。

### 6.4 告警渠道（可选）

**微信（Server酱）**：[申请 SendKey](https://sct.ftqq.com/) 后填入 `config/settings.yaml`：

```yaml
alert:
  serverchan_key: "SCTxxxxxxxxxxxx"
```

**邮件（第四轮新增）**：QQ/163 邮箱"授权码"（非登录密码）方式：

```yaml
alert:
  email:
    enabled: true
    smtp_host: "smtp.qq.com"
    smtp_port: 465
    username: "xxx@qq.com"
    auth_code: ""          # 授权码；建议放 .env: LIHU_ALERT__EMAIL__AUTH_CODE
    to: ["xxx@qq.com"]
    send_daily_digest: true   # 每日综合日报邮件（HTML，每日一封）
```

告警场景（第五轮优化为"每日一封综合日报"）：

- **每日综合日报**（`send_daily_digest: true`，巡检完成后发送一封 HTML 邮件）：
  - **账户总览**：总资产 / 可用现金 / 持仓市值 / 今日盈亏 / 累计盈亏
  - **当前持仓**：代码 / 名称 / 持股 / 成本 / 现价 / 市值 / 浮动盈亏（红涨绿跌）/
    盈亏% / 占比 / 止损线
  - **今日操作**：买入成交 / 卖出记录（含止损执行与实现盈亏）/ 被拒信号
    （含完整风控拒绝原因，如"铁律1：止损价不低于买入价"）
  - **盈亏分析**：今日已实现盈亏 / 当前浮动盈亏 / 今日总盈亏 / 持仓盈亏分布
  - **市场与风险提示**：市场状态与过滤模式 / 待执行止损（次日开盘）/
    连亏停手票 / 当日告警汇总
- **即时邮件**：仅 ERROR 级系统故障（接口异常 / 巡检崩溃）即时发送——
  WARN/INFO（风控拦截、止损触发、连亏停手等）不再逐条发邮件，全部进日报，
  避免碎片邮件轰炸。

预览邮件样式（不实际发送）：`python scripts/preview_daily_report.py`
→ 生成 `outputs/preview_daily_report.html`，浏览器打开即可查看。

**缺席心跳（第四轮新增）**：[healthchecks.io](https://healthchecks.io)（免费）注册
Check 后把 Ping URL 填入 `heartbeat.healthchecks_url`，后台设"每日 17:30 前未
收到成功 ping → 告警"——**进程崩溃/断电也能收到通知**（告警不再依赖进程自己活着）。

### 6.5 部署方式

- **NAS（群晖 DS918+，推荐）**：Docker Compose 部署，见 `docs/DEPLOY_NAS.md`
  （含备份策略、安全边界、故障排查、QMT 实盘迁移说明）
- **Windows 备用**：任务计划程序开机自启，见 `docs/DEPLOY_WINDOWS.md`
- **备份**：`python scripts/backup_data.py`（zip 滚动 30 份，含恢复演练流程）

---

## 七、配置说明

配置文件：`config/settings.yaml`（环境变量 `LIHU_` 前缀可覆盖）。

```yaml
tushare:
  token_file: "E:/LihuQuantify/tushareMcp.json"   # token 文件
  cache_dir: "./data/cache"                         # 接口缓存（当日复用）

strategy:
  ma_periods: [5, 10, 20, 60]          # 均线周期
  golden_cross_max_freshness_days: 3   # 金叉新鲜度（网格搜索结果）
  volume_ratio_threshold: 1.0          # 量比阈值
  entity_ratio_threshold: 0.40         # 实体占比下限
  close_to_ma5_max_dev: 0.05           # 收盘乖离 MA5 容差（网格搜索结果）
  chasing_high_threshold: 0.08         # 追高阈值（乖离10日线）
  market_filter: true                  # 市场状态过滤开关（强烈建议开启）

risk:                                  # ⚠️ 铁律数值，勿放松
  max_single_position: 0.25            # 单票 ≤25%
  max_sector_position: 0.40            # 同板块 ≤40%
  stop_loss_warn: -0.03                # -3% 预警
  stop_loss_exec: -0.05                # -5% 执行
  stop_loss_force: -0.08               # -8% 强制
  trailing_profit_pullback: 0.03       # 移动止盈回撤
  max_trades_per_ticker_month: 3       # 同票月内 ≤3 次
  halt_after_consec_losses: 3          # 连亏 3 笔
  halt_days: 30                        # 停手 30 天

backtest:
  commission: 0.00025                  # 佣金 万2.5（最低5元）
  stamp_tax: 0.0005                    # 印花税 万5（2023.8起）
  slippage: 0.001                      # 滑点 0.1%

scheduler:
  daily_scan_cron: "30 15 * * 1-5"     # 巡检时间
  timezone: "Asia/Shanghai"
```

---

## 八、风控铁律说明（重要）

系统内置的交易铁律来自 `docs/交易铁律.md`（用真金白银换来的教训）：

1. **先写止损，再点买入**——买入单+止损条件单同时挂出
2. **成本 -8% 或跌破 10 日线，无条件离场**
3. **单票 ≤25%，同板块 ≤40%**
4. **绝不向下补仓**
5. **让盈利奔跑（移动止盈），让亏损快走**
6. **同一只票一个月 ≤3 次；连亏 3 笔，停手一个月**
7. **不追高**（乖离 10 日线 >8% 拒绝）
8. **不预测，只应对**——进场前写好剧本，到点就演

这些规则在代码中的位置：

| 铁律 | 代码位置 |
|---|---|
| Checklist 8 项闸门 | `src/lihu_quantify/risk/checklist.py` |
| 三档止损+移动止盈 | `src/lihu_quantify/risk/stop_loss.py` |
| 仓位/板块限制 | `src/lihu_quantify/risk/position_limit.py` |
| 频率/连亏停手 | `src/lihu_quantify/risk/frequency.py` |
| OMS 原子化买卖 | `src/lihu_quantify/execution/oms.py` |

---

## 九、目录结构

```
E:\LihuQuantify\
├── config/settings.yaml           # 全局配置
├── tushareMcp.json                # Tushare token
├── run_backtest.py                # 小样本回测入口
├── run_full_backtest.py           # 扩大样本回测入口
├── run_live.py                    # 模拟盘/实盘入口
├── run_scheduler.py               # 定时调度入口
├── scripts/
│   ├── grid_search.py             # 参数网格搜索
│   └── analyze_grid.py            # 网格分析+热力图
├── src/lihu_quantify/
│   ├── data/                      # 数据层（Tushare/DuckDB）
│   ├── indicators/                # 指标层（含自研九转序列/MACD背离）
│   ├── strategy/                   # 策略层（CherryClaw/六维诊断）
│   ├── risk/                      # 风控层（Checklist/止损/仓位/频率）
│   ├── backtest/                  # 回测引擎（事件驱动）
│   ├── execution/                 # 执行层（MiniQMT/OMS/模拟盘）
│   └── monitor/                   # 监控层（调度/告警/报告）
├── tests/                         # 91 个测试
├── outputs/
│   ├── reports/                   # 每日巡检报告
│   └── grid_*.png/csv             # 网格搜索产出
└── data/
    ├── lihu_quant.duckdb          # 行情数据库
    └── cache/                     # Tushare 响应缓存
```

---

## 十、常见问题

**Q1：跑测试报 `ModuleNotFoundError: lihu_quantify`？**
确保用虚拟环境运行：`.\.venv\Scripts\python.exe -m pytest`，且已执行 `pip install -e ".[dev]"`。

**Q2：Tushare 接口报"无权限/积分不足"？**
部分接口（如 `moneyflow_dc`、`broker_recommend`）需要更高积分。核心日线接口（daily/index_daily/stock_basic）120 分即可。

**Q3：`pip install` 很慢或超时？**
用清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`。

**Q4：实盘模式报"未找到 xtquant"？**
把 QMT 安装目录的 `bin.x64\Lib\site-packages` 加入系统 `PYTHONPATH`，并确认 QMT 极简模式客户端已启动登录。

**Q5：为什么当前没有任何买入？**
大概率是**市场过滤生效**——上证 20 日涨幅未达 +3%（非上涨段），系统主动空仓。查看报告里的"市场状态"字段。这是设计行为，不是 bug。

**Q6：怎么临时关闭市场过滤做测试？**
`settings.yaml` 里 `strategy.market_filter: false`（不建议实盘关闭）。

**Q7：报告里出现"⚠️ 需重建"止损登记？**
持仓缺少止损登记（通常是程序中断导致）。执行 `python run_live.py --rebuild` 重建。

**Q8：怎么验证系统数据是对的？**
测试套件内置回归基线（600584 长电科技的九转序列、蜡烛图预警等以真实历史报告为基准），`pytest` 全过即数据链路正确。

---

**免责声明：本系统输出的所有内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。数据来自 Tushare，可能存在延迟、缺失或错误。**

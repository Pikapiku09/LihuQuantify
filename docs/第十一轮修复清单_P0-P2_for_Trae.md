# 第十一轮修复清单（P0 资金安全 / P1 回测口径 / P2 数据层与性能）

> 来源：2026-08-29 全项目代码评审（数据层/回测/执行风控监控/Web 工程化四路并行审查，关键问题已人工核实行号）。
> 分级：P0 = 链路断裂或资金安全，立即修；P1 = 回测口径系统性偏差；P2 = 数据层/性能/健壮性，择机修。

## ⚠️ 全局纪律约束（Trae 执行前必读）

1. **不改动**：策略参数（settings.yaml `strategy:` 段）、铁律数值（`risk:` 段）、股票池配置、`capital_guard`/`ai_summary` 开关。本轮全部为 bug 修复/工程加固，非参数变更。
2. **P1 全节警示**：P1 的修复会**改变全部历史回测数字**。按第七轮先例执行：先修代码 → 重走 strat200 网格 36 组 → 决策日志登记口径切换点。代码修复与数字重走可分两次提交。
3. P0-5/P0-6 会改变纸面盘拦截行为（此前漏拦的现在会拦）——属于**恢复铁律既有语义**（checklist 8 项闸门、25% 口径），不是新规则；在决策日志 Live 积累期表各登记一行"闸门口径补全"。
4. 每项修完跑全量测试（当前 180 个），新增测试随项列出，不得删改既有测试断言来"凑通过"。

---

## P0 — 资金安全与链路（7 项，立即修）

### P0-1 补 README.md（修复发布链路 #1）
- **问题**：根目录无 README.md，但 `pyproject.toml:9`（`readme = "README.md"`）与 `Dockerfile:22`（`COPY pyproject.toml README.md ./`）都引用它 → `pip install .`、`python -m build`、`docker compose build` 全部失败，NAS 部署方案当前不可用。
- **修复**：新建 `README.md`，内容：项目一句话简介、快速开始（`run_scheduler.py` 调度 / `run_live.py --once` 单次巡检 / `run_backtest.py` 回测 / `start_dashboard.bat` 看板）、指向 `docs/USER_GUIDE.md` 与 `docs/ARCHITECTURE.md`、免责声明一句。
- **验收**：`python -m build`（或 `pip install -e .`）成功；`docker compose build` 通过 COPY 层。

### P0-2 删除死入口 cli.py 声明（修复发布链路 #2）
- **问题**：`pyproject.toml:47-48` 声明 `lihu = "lihu_quantify.cli:main"`，但 `src/lihu_quantify/cli.py` 不存在（已 glob 确认包内 38 个模块无此文件）→ pip 安装后 `lihu` 命令 `ModuleNotFoundError`。
- **修复**：删除 `[project.scripts]` 整段（最小改动，实际入口是根目录 run_*.py）。
- **验收**：`pip install -e .` 成功且不再生成 `lihu` 命令；`egg-info` 重新生成后 `entry_points.txt` 为空。

### P0-3 止损失败单被静默清空（漏单，资金安全）
- **问题**：`src/lihu_quantify/monitor/scheduler.py:902-934`。`_execute_pending_stops` 循环内卖单失败走 `continue`（注释写"保留待执行"），但循环结束后第 931-934 行**无条件** `pending_file.write_text("[]")` → 失败/取不到开盘价（`_fetch_open` 返回 0，907 行条件为假）的止损单被永久丢弃，持仓失去止损保护（已人工核实）。
- **修复**：
  1. 构建 `remaining` 列表：`result.success == True` → 从 pending 移除；失败或无开盘价 → 追加进 `remaining`；
  2. 循环后写 `json.dumps(remaining, ensure_ascii=False)`（空列表时才是 `"[]"`）；
  3. 失败条目加 `failed_count` 字段累加，`failed_count >= 3` 时升级为 ERROR 告警（触发即时邮件通道）。
- **验收**：新增回归测试：①mock `broker.sell` 返回 `success=False` → 断言 pending_stops.json 仍含该条目且 `failed_count` 递增；②成功 → 断言移除；③无开盘价 → 保留。现有止损相关测试全过。
- **注意**：不动止损判定/登记逻辑，只动执行后的持久化行为。

### P0-4 模块内 json 导入不一致导致 NameError
- **问题**：`scheduler.py` 顶部无 `import json`；7 处函数内局部导入（787/812/824/897/1016 行 `import json`；1139/1213 行 `import json as _json`）。`_append_filter_stats`（1146 行）与 `_monthly_review`（1217 行）的 except 子句写 `json.JSONDecodeError`，但作用域内只有 `_json` → filter_stats.json 损坏时月度复盘任务直接 NameError 崩溃（已人工核实）。
- **修复**：模块顶部统一 `import json`，删除全部 7 处函数内导入，`_json.loads/dumps` 改回 `json.loads/dumps`。
- **验收**：新增测试：写入损坏 JSON 到 filter_stats.json → `_append_filter_stats` 不抛 NameError（走 OSError/JSONDecodeError 分支静默重建）；全量测试无新增 import 错误。

### P0-5 补全账户快照字段，恢复 Checklist 8 项闸门
- **问题**：`scheduler.py:855-868` `_snapshot()` 构造的 AccountSnapshot 不含 `trades`、`halted_until`、`psychology_alert` → `checklist.py` 中 `_check_frequency`（151-176 行）月内交易数恒为 0、"连亏 3 笔停手"恒不触发、心理门禁（214-227 行）恒通过。8 项闸门实际只剩 5 项生效，"月内 ≤3 次"铁律被完全绕过。`run_live.py:209-222` 的 `broker_snapshot` 同样缺失。
- **修复**：
  1. `_snapshot()` 从 PaperBroker 填充：`trades=broker.trades`（PaperBroker 已有完整交易记录）、`halted_until=broker.account.halted_until`（paper_trade.py 连亏状态机已维护，读取即可）；
  2. `psychology_alert` 若无数据来源，置 `None` 并确认 checklist 该项走"未知"分支（不拦截但报告标注），不得伪造为通过；
  3. `run_live.py` 的 `broker_snapshot` 同步补齐；
  4. 抽公共函数 `build_account_snapshot(broker)` 供两处调用（消除三套构造分叉的第一步）。
- **验收**：新增集成测试：①构造本月已 3 笔交易的账户 → 第 4 笔买入被 `_check_frequency` 拒绝；②构造 `halted_until` 未到期的账户 → 买入被拒；③快照含 trades 后 `report.py` 铁律自检输出与实际一致。
- **登记**：决策日志 Live 积累期表加一行"闸门字段补全（频率/停手/心理门禁恢复生效），口径修正非规则变更"。

### P0-6 仓位 25% 限制补上现有持仓市值（口径漏洞）
- **问题**：`checklist.py:82` `pct = ctx.invest_amount / account.total_asset` 只校验本笔投入，不含该票现有市值 → 隔日/分批对同一票加仓，每笔都 ≤25% 放行，累计可远超 25%。口径正确的 `risk/position_limit.py:44-49`（`existing_mv + invest`）在 scheduler 主链路从未被调用。
- **修复**：`_check_position` 改为 `pct = (existing_mv + ctx.invest_amount) / account.total_asset`，`existing_mv` 从 AccountSnapshot.positions 取该票市值（无持仓为 0）；或直接在闸门内调用 `PositionLimiter.can_add` 复用其口径，二选一后在注释注明单一事实来源。
- **验收**：新增测试：账户已持有某票 20% 市值 → 再买 10% 被拒（旧逻辑放行）；首买 25% 通过、26% 拒绝。
- **登记**：同 P0-5，决策日志一行"仓位闸门口径补全（含存量市值）"。

### P0-7 Web 最小鉴权 + 端口绑定
- **问题**：`web/server.py` 全部端点无认证；`POST /api/settings`（813 行）可改 settings.yaml 并明文写 secrets.json（880 行），`POST /api/settings/test_ai_key`（898 行）可诱导后端携带任意 key 外连。`docker-compose.yml:42` `"8000:8000"` 映射宿主机所有网卡 → 局域网任意设备可读持仓（`/api/state` 返回 paper_state 全量）、篡改配置、覆盖 API key。
- **修复**：
  1. `server.py` 加 Bearer Token 中间件：环境变量 `LIHU_WEB_TOKEN`，空值 = 不启用（本机 127.0.0.1 直跑零配置不变）；非空时所有 `/api/*` 请求校验 `Authorization: Bearer <token>`，失败 401；
  2. 前端 `web/static/index.html`：fetch 封装处统一附带该 header，token 从 localStorage 读（首次 401 弹输入框，一次存储）；
  3. `docker-compose.yml`：端口改 `"127.0.0.1:8000:8000"`（或注释里给出内网 IP 绑定示例），并在 `.env.example` 加 `LIHU_WEB_TOKEN=` 条目说明；
  4. 展示层已有的 key 掩码/审计 `***` 逻辑不动。
- **验收**：新增 TestClient 测试：①设 token 后无 Authorization 头 → 401；②带头 → 200；③不设 token → 行为与现状完全一致；④静态文件（看板页面本身）可豁免或同样 401（选一并注明）。

---

## P1 — 回测口径修复（6 项）

> ⚠️ 本节执行顺序：先全部修完 → strat200 池 36 组网格统一重走 → 决策日志登记"第十一轮口径切换点：复权/涨跌停/限价公式/现金校验/前视偏差/量比定义"，旧数字标注口径版本。修复期间**不得**用重走数字反向调整任何参数（诊断不选参）。

### P1-1 复权处理（影响最大）
- **问题**：`run_backtest.py:48-52`、`run_full_backtest.py:107-111` 仅拉取 Tushare `daily`（未复权价），全链路无 `adj_factor`。除权日 MA/MACD/金叉计算错误，成本价止损与 MA10 破位在除权后假触发，卖出 pnl 按除权后价格对除权前成本计算——指标与盈亏双重失真。
- **修复**：
  1. data 层新增 `fetch_adj_factor(ts_code, start, end)`（Tushare `adj_factor` 接口，落 DuckDB 表 `adj_factors`，走既有 upsert 模式）；
  2. 数据准备阶段做前复权：`price_adj = price × adj_factor / 最新 adj_factor`（open/high/low/close 全套；volume 除以复权因子）；
  3. 指标计算与撮合统一用前复权价（engine 无需感知）；回测报告注明"前复权口径"。
- **验收**：选有年度大额分红的标的（如 600584），断言除权日前后 MA20 连续无跳变；对比修复前后该票信号差异并记录；全网格重走后输出对比表（命名 `*_round11_adjusted` 后缀，沿用第七轮 `_fix97` 先例不覆盖旧产物）。

### P1-2 涨跌停/停牌无法成交建模
- **问题**：`src/lihu_quantify/backtest/broker.py:64-95` `fill()` 只要拿到 next_bar 就一律成交——涨停一字板买单、跌停卖单、停牌复牌极端价均按 open±0.1% 滑点成交，高估止损执行率与打板成交率。
- **修复**：
  1. `fill()` 接收 `pre_close`（next_bar 的 `pre_close` 字段，Tushare daily 自带；缺失时用前一根 close 回退）；
  2. 计算当日停板价：主板 ±10%（`round(pre_close × 1.1, 2)` / `× 0.9`；ST/创业板先不细分，代码留 `limit_pct` 参数，注释注明简化假设）；
  3. 买单：`low >= 涨停价`（一字板）→ 拒单返回 None；卖单：`high <= 跌停价` → 拒单；
  4. 停牌：`volume == 0` 或 open 为 NaN/0 → 拒单（当日单当日废，引擎不得无限期顺延重挂——与实盘"次日开盘执行一次"对齐）。
- **验收**：新增单测：①构造一字涨停 bar → 买拒单；②一字跌停 → 卖拒单（止损单同样拒，断言 pending 清理）；③volume=0 → 拒单；④正常 bar 行为与现在完全一致（既有测试零改动通过）。

### P1-3 限价单成交价公式错误
- **问题**：`broker.py:84` 限价买 `price = min(order.limit_price, high)`、88 行限价卖 `price = max(order.limit_price, low)`。正确应为与 open 比较：买 `min(limit, open)`、卖 `max(limit, open)`。现公式在 open 优于限价时仍按（更差的）限价成交——买贵/卖便宜（已人工核实）。
- **修复**：84 行改 `price = min(order.limit_price, open_price)`；88 行改 `price = max(order.limit_price, open_price)`。触发条件（82 行 `limit < low` 拒单、86 行 `limit > high` 拒单）保持不变。
- **验收**：新增单测：①open < limit 的买单以 open 成交；②open > limit 且 low <= limit 以 limit 成交；③卖单对称两例。

### P1-4 买入现金充足性校验（防透支）
- **问题**：`engine.py:236-244` 买入股数按 T 日 close 估算，实际成交在 T+1 open+滑点（`broker.py:91-92`）；`portfolio.py:40` `apply_fill` 直接加减现金无检查 → T+1 跳空高开或多单同日撮合时现金可为负（透支）。
- **修复**：撮合路径加校验（建议在 engine 生成订单前按 T+1 预估价、broker.fill 实际成交后二次确认双保险，选一处落地即可，注明位置）：
  1. 现金不足原量 → 按 `cash // (price × 100)` 向下取整手数缩量成交；缩量后不足 1 手 → 拒单；
  2. 断言任何路径 `account.cash >= -1e-9`。
- **验收**：新增测试：①T+1 高开 5% 现金吃紧 → 缩量为整手且现金非负；②现金仅够半手 → 拒单；③多 pending buy 同日撮合不透支。

### P1-5 pre_filter 前视偏差（用未来流动性过滤历史）
- **问题**：`src/lihu_quantify/strategy/cherry_claw.py:69-72` `pre_filter` 用 `df["amount"].tail(20)`（整个回测序列的**最后** 20 根）判断 20 日均成交额，作用于全历史信号——用未来信息决定历史某段是否入选；66 行用 `len(df)`（数据窗口长度）近似上市天数，拉 2 年数据则所有股票都通过，与"上市不足 60 日剔除"语义不符。
- **修复**：
  1. 均额过滤移入信号生成路径（`_three_layer_filter` 或 scan 内）：`df["amount"].rolling(20).mean()` 逐 bar 取当日值判断；
  2. 上市天数：`stock_basic.list_date` 与该 bar 的 trade_date 差值 ≥ `min_list_days`（list_date 通过 DataFrame 元数据或参数注入）。
- **验收**：对比测试：构造前段流动性低、后段高的序列 → 旧逻辑全历史剔除、新逻辑仅剔除前期；修复前后回测信号 diff 记录进重走对比表。

### P1-6 量比定义修正（分母含当日）
- **问题**：`src/lihu_quantify/indicators/standard.py:97-98` `vol_ratio = vol / rolling(5).mean()`，分母含当根 → 当日放量时分母同步抬高，`volume_ratio_threshold=1.0` 的"放量"判定实际更难触发。标准定义是当日量 / 前 5 日均量（不含当日）。
- **修复**：分母改 `df["vol"].rolling(5).mean().shift(1)`。
- **验收**：新增单测：前 5 日量恒定 V、当日 2V → `vol_ratio == 2.0`（修复前 ≈1.67）。
- **注意**：此项会改变策略信号输出（阈值 1.0 是策略参数，不动阈值只修指标定义）——与 P1 其余项一起纳入统一口径重走，不单独重跑。

---

## P2 — 数据层与性能健壮性（9 组，择机修）

### P2-1 缓存失效机制（数据层最优先）
- **问题**：`tushare_client.py` 文件缓存无任何 TTL/失效判断（`_read_cache` 44-51 行）。后果：`data_manager.py:129-139` `fetch_income/fetch_fina_indicator` 参数只含 ts_code → 缓存键永不变化，**新财报永远不会被拉到**；`pool.py:42` 股票池缓存 `strat_pool_n{...}.json` 同理（文件名不含日期，注释说"每日 1 次"实为永久复用），股票池卡死在首次构建日。
- **修复**：
  1. `TushareClient` 加缓存 TTL：文件 mtime 超过 N 小时（默认 12h，可配 `tushare.cache_ttl_hours`）视为 miss；
  2. income/fina_indicator 类：params 加 `start_date=最近报告期` 使键随时间变化（与 TTL 双保险）；
  3. 股票池缓存文件名加日期段（如 `strat_pool_200_20260829.json`），当日已有则复用。
- **验收**：单测：①mtime 过期后重新请求 API；②同日重复调用命中缓存零请求；③新交易日财报接口返回新数据。

### P2-2 缓存键不含 fields（子集污染）
- **问题**：`tushare_client.py:90-101` 缓存路径只由 `api_name/ts_code/end_date/start_date` 决定（`_cache_path` 33-42 行），请求体可带 `fields`（100-101 行）→ 先带 `fields=["ts_code","close"]` 查询后，不带 fields 的同参数调用命中残缺缓存。
- **修复**：`_cache_path` 加入规范化 fields（`sorted(fields) or "all"` 后 hash 进文件名）；或缓存恒存全字段、返回时按 fields 切片（推荐后者，缓存命中率不受损）。
- **验收**：单测：先子集后全量查询，后者列完整不缺失。

### P2-3 HTTP 层重试与限流（yaml 宣称的 rate_limits 从未实现）
- **问题**：`tushare_client.py:104,128` `session.post().json()` 无 `raise_for_status`、无网络/JSON 异常捕获、无重试；`settings.yaml:9-13` 的 `rate_limits` 在 `config.py:35-38` 无对应字段被 pydantic 静默丢弃——配置与实现脱节。
- **修复**：
  1. 抽 `_post_with_retry`：检查状态码 → JSON 解析异常捕获 → 识别限流（`code != 0` 且 msg 含"每分钟"）指数退避（1s/2s/4s）重试至多 3 次；`query`/`query_raw` 共用（消除 104/128 两处重复）；
  2. `TushareConfig` 补 `rate_limits` 字段，`TushareClient` 实现最小限速（同一 API 相邻两次调用 sleep 间隔，默认 0.3s 可配）。
- **验收**：单测：mock 连续两次限流响应 + 第三次成功 → 重试后成功；正常响应零额外延迟。

### P2-4 DuckDB 单写多读（双容器锁冲突）
- **问题**：DuckDB 不允许两进程同时读写同一文件；当前形态 NAS 上 scheduler 容器写 + web 容器读，`docker-compose.yml` 共享 data/ 卷，看板 30s 轮询 + 巡检写入锁冲突概率极高。`duckdb_store.py:128-133` 无只读模式、无锁重试，`close()`（239 行）需手动调易泄漏。
- **修复**：
  1. `DuckDBStore.__init__` 加 `read_only: bool = False` 参数传给 `duckdb.connect`；
  2. `web/server.py` 所有直查点改用只读连接，捕获锁异常降级读 last_scan.json（已有依赖切断，改动小）；
  3. `DuckDBStore` 实现 `__enter__/__exit__` 支持 with 用法；
  4. 写侧（scheduler）捕获锁冲突异常时 0.5s 退避重试 3 次。
- **验收**：集成测试：进程 A 持写连接时进程 B 只读查询成功；B 拿不到锁时降级路径返回缓存数据。

### P2-5 ensure_daily_basic / ensure_moneyflow 本地优先
- **问题**：`data_manager.py:82-110` 两个 ensure 方法每次调用必发 API（缓存键含 end_date 跨日必 miss）→ 200 只池每天 400 次请求顶积分限流。同文件 `ensure_daily`（48-74 行）已有正确的"先查本地 max(trade_date) >= end_date 则跳过"模式。
- **修复**：复制 ensure_daily 模式到两方法：先 `store.query` 本地表判断覆盖，未覆盖才增量拉取。
- **验收**：单测：同日第二次调用零 API 请求（mock client 计数断言）；跨日仅拉增量区间。

### P2-6 回测性能：指标去重 + 策略向量化（预期提速 10-100 倍）
- **问题**：
  1. 指标算两遍：`engine.py:105` 已 `add_all_standard(df)`，无状态路径 118 行 `strategy.scan(df)` 时 `cherry_claw.py:52-55` `_prepare_indicators` 对同一 df 再算全套 MA/MACD/BOLL/RSI；
  2. `cherry_claw.py:132-156` `for i in range(len(df)): row = df_ind.iloc[i]` 逐行反模式，全部条件（freshness/ma5>ma10/vol_ratio/body_ratio/is_red/乖离/ma20_slope）都是列级布尔表达式；
  3. `candlestick.py:49-104` 逐行形态识别同理（吞没形态需 shift(1)）。
- **修复**：
  1. `StrategyBase.scan` 加 `indicators_ready: bool = False` 参数（或 engine 传 flag），`_prepare_indicators` 检测到已有列则跳过；
  2. cherry_claw 信号生成改布尔掩码一次性输出全部 Signal；
  3. candlestick 形态向量化（仅吞没形态用 shift(1)）。
- **验收**：**信号输出全等测试**：随机 20 只股票 × 修复前后信号列表逐条对比完全一致（这是硬门槛，防止向量化引入语义变化）；`scripts/grid_search_v2.py` 单组耗时对比写入重走对比表。

### P2-7 Web 事件循环阻塞 + gzip
- **问题**：`server.py` 全部 `async def` 路由内同步 IO——`test_ai_key`（911 行 requests.get timeout=10）网络不通时卡死整个事件循环 10 秒；多处 `read_text` 同理。echarts.min.js 1MB 未压缩传输（vendor 本地化理由成立，保留）。
- **修复**：①无 await 的路由 `async def` 改 `def`（FastAPI 自动丢线程池），至少 `test_ai_key` 必改；②`app.add_middleware(GZipMiddleware, minimum_size=1024)`；③顺带 `/api/reports` 列表（330 行）改按字节读前 200 字节取标题，不再全文 read_text。
- **验收**：TestClient 测试 gzip 响应头出现；`test_ai_key` mock 慢响应时并发请求其他端点不受阻塞。

### P2-8 test_execution 状态泄漏（tests/.test_state 580 个残留文件）
- **问题**：`tests/test_execution.py:27-33` `_unique_path` 把状态写到 `tests/.test_state/{prefix}_{pid}_{counter}.json` 从不清理，docstring 声称"用 tmp_path 隔离"与实现不符；36 行 `_paper(tmp_path=None)` 是死参数。每次全量跑测试新增 ~23 个文件。
- **修复**：`_paper` 改用 pytest `tmp_path` fixture（conftest.py:80 `tmp_duckdb` 是正确示范）；删除 `_unique_path` 机制与死参数；删除现存 `tests/.test_state/` 目录（已 gitignore，无入库影响）。
- **验收**：全量测试通过后 `tests/.test_state/` 不再被创建。

### P2-9 杂项清理（可并入任意轮次，逐条独立可做）
1. **配置一致性**：`config.py:89` `stamp_tax` 默认 0.0001 → 0.0005 与 `settings.yaml:71` 统一（2023.8 后卖出印花税万 5，代码默认是错值）；
2. **双定义收敛**：`chasing_high_threshold` 在 `config.py:68`（StrategyConfig）与 `config.py:84`（RiskConfig）各一份 → 保留 Risk 一处，Strategy 引用之；
3. **铁律常量收敛**：0.25/0.40 三处硬编码（`checklist.py:40-41`、`position_limit.py:17-18`、`report.py:339`）→ 收敛到单一常量（读 settings.risk），改限额只动一处；
4. **sys.path hack**：`scheduler.py:35-43` 从项目根 `from run_backtest import classify_market_state` → 把该函数移入包内（如 `lihu_quantify/market.py`），run_backtest 改为薄引用；
5. **日期纪律**：`pool.py:69` `date.today()` → 用 `store.get_latest_trade_date()` 锚定（同文件 82 行已取，上移复用）；`paper_trade.py:334`、`scheduler.py:363-366,1167` 的本地时间 → 统一 `zoneinfo.ZoneInfo(settings.scheduler.timezone)`；
6. **run_live 重复**：`run_live.py:35-87` `scan_universe` 与 `DailyScanner` 大段重复且 `CherryClaw()` 裸构造（64 行）与 148 行参数版不一致 → run_live 复用 DailyScanner；
7. **告警真实性**：`alerts.py:128-149` `send` 只要 enabled 就 `return True` → 返回各通道实际结果，ERROR 级 2 次指数退避重试，连续失败写告警自检文件；
8. **PaperBroker 落盘放大**：`scheduler.py:983-984` 循环内 `update_high_water(save=True)` 每票触发全量落盘+全持仓取价 → 循环内 `save=False`，结束统一落盘一次（O(持仓×2) 次网络查询 → 1 次）；
9. **stop_registry 漂移**：`oms.py:174` 同票二次买入覆盖旧 StopOrder、246-247 行 rebuild 对已存在条目永不刷新 → 支持同票多条（按 buy_order_id），rebuild 校验 volume 与持仓一致才复用否则重算；`oms.py:248-252` cost=0 时 `sp=round(0*0.92,2)=0` 永不触发 → sp<=0 跳过登记并 ERROR 告警；
10. **xtquant 取价守卫**：`xtquant_client.py:179-191` `get_price` 未订阅行情源静默返回 0 → 连续 N 次取价失败 ERROR 告警（不做行情订阅重构，只加守卫）；
11. **Dockerfile**：加 `HEALTHCHECK CMD curl -f http://localhost:8000/api/health`（端点已有）；
12. **热力图参数白名单**：`server.py:544` `/api/heatmap/detail` 的 code 参数只查 `"." in code` → 改 `^\d{6}\.(SH|SZ)$` 正则白名单再拼 glob。

---

## 执行顺序建议

1. **第一批（当天）**：P0-1 → P0-2 → P0-4（三个小改动，恢复构建链路）→ 全量测试。
2. **第二批（本周）**：P0-3 → P0-5 → P0-6（止损与闸门，各带回归测试）→ 决策日志登记两行。
3. **第三批**：P0-7（Web 鉴权，前后端 + compose 一起动）。
4. **第四批（P1 专项，整批完成后再统一重走网格）**：P1-1 → P1-2 → P1-3 → P1-4 → P1-5 → P1-6 → 36 组网格重走 → 决策日志登记口径切换点。
5. **第五批起（P2 按序）**：P2-1（缓存失效，数据正确性优先于性能）→ P2-3 → P2-5 → P2-4 → P2-6 → P2-7/P2-8 → P2-9 随批清理。

## 交验标准（整轮）

- 全量测试通过，新增测试 ≥ 20 个（P0-3/4/5/6/7、P1-2/3/4/6、P2-1/2/3/5/6 均有对应新测试）；
- `python -m build` 与 `docker compose build` 成功；
- P1 重走对比表产出（`*_round11_adjusted` 命名），决策日志新增"第十一轮"行 + 口径切换点 + 数据段状态更新；
- 不触碰 settings.yaml 的 strategy/risk 参数段（除 P2-9-1 的 stamp_tax 代码默认值对齐 yaml）。

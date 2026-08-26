# LihuQuantify 量化交易系统 · 架构设计文档

> 版本：v0.1 | 更新：2026-08-26
> 状态：设计阶段（待用户确认后进入实现）

---

## 1. 项目目标

构建一个端到端的 **A 股日线级量化交易系统**，覆盖从 Tushare 数据订阅到 MiniQMT 实盘下单的完整链路。

**核心命题**：把个人投研流水线（`dsh-invest-plugin`，源码见 `E:\Dsh_WorkSapce\Dify_Agents\quant-tool-handoff`）中**经过实战验证的策略体系与风控铁律**，从 LLM 驱动的 Markdown 报告形态，工程化为**代码级可回测、可实盘执行**的量化系统。

### 1.1 核心交付能力

| 能力 | 描述 |
|---|---|
| 数据订阅 | Tushare Pro 13+ 接口实时拉取 + DuckDB 落库 + 增量更新 |
| 策略执行 | CherryClaw 三层过滤、涨停回马枪、龙头首板、六维诊断 |
| 风控闸门 | Checklist 8 项强制闸门 + 三档止损 + 仓位/板块/频率限制 |
| 回测验证 | 事件驱动引擎，完整还原状态依赖逻辑 |
| 实盘下单 | MiniQMT (xtquant) 对接，模拟盘 → 小资金 → 逐步放量 |
| 监控告警 | 收盘后自动巡检 + 微信/邮件告警 + Markdown 报告归档 |

### 1.2 范围与边界

**做**：
- A 股日线级（含 weekly 周线辅助判断）
- 规则型/技术指标策略
- 完整风控闸门（Checklist 强制拦截）
- MiniQMT 实盘

**不做**：
- 实时分钟/Tick 行情（原系统也锚定日线）
- AI/ML 选股（Qlib 路线，与现有规则策略冲突）
- 高频/Tick 策略
- 多市场（港股/美股/期货）
- 投顾/荐股（输出必须带"仅供参考，不构成投资建议"）

---

## 2. 系统架构

### 2.1 分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        用户层 (CLI / 调度器)                        │
├─────────────────────────────────────────────────────────────────────┤
│  monitor   │  execution  │  backtest  │   risk    │  strategy       │
│  巡检/告警  │  OMS/实盘   │  回测引擎   │  风控闸门  │  选股/诊断      │
├─────────────────────────────────────────────────────────────────────┤
│                       indicators 指标层                              │
│       MA/MACD/BOLL/RSI (pandas-ta)  +  九转序列/背离 (自研)          │
├─────────────────────────────────────────────────────────────────────┤
│                         data 数据层                                  │
│        Tushare 客户端  →  DuckDB 落库  →  缓存/增量更新              │
├─────────────────────────────────────────────────────────────────────┤
│              基础设施 (config / loguru / pydantic / pytest)         │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流向（一次完整交易决策）

```
1. APScheduler 触发（收盘后 15:30）
        ↓
2. data: 取真实最新交易日锚点 (index_daily 000001.SH)
        ↓
3. data: 增量拉取 daily/daily_basic/moneyflow/limit_list_d/sw_daily
        ↓
4. indicators: 计算 MA/MACD/BOLL/RSI + 九转序列 + MACD 背离
        ↓
5. strategy: CherryClaw 三层过滤 → 候选名单
        ↓
6. strategy: 六维诊断 + L1-L4 目标价 + 综合评分
        ↓
7. risk: Checklist 8 项闸门（任一拒绝即拦截）
        ↓
8. risk: 仓位/板块/频率/连亏状态机校验
        ↓
   ┌────────────────────────┬────────────────────────┐
   │ 回测模式                │ 实盘模式                │
   │ ↓                      │ ↓                      │
   │ backtest: 事件驱动撮合  │ execution: xtquant 下单 │
   │ ↓                      │ ↓                      │
   │ metrics: 绩效报告       │ 同时挂买入单 + 止损条件单│
   └────────────────────────┴────────────────────────┘
        ↓
9. monitor: 生成 Markdown 报告 + SVG 图表 + 告警推送
```

---

## 3. 技术栈

| 层 | 技术 | 版本/说明 | 选型理由 |
|---|---|---|---|
| 语言 | Python | 3.10+ | 日线策略无需低延迟 |
| 数据源 | Tushare Pro | REST API | 已对齐 13 接口结构 |
| 存储 | **DuckDB** | 0.10+ | 列存 OLAP，pandas/Arrow 原生集成 |
| 指标库 | **pandas-ta** | 0.3.14b | MA/MACD/BOLL/RSI；TA-Lib Windows 装麻烦 |
| 回测引擎 | **自研事件驱动** | - | 完整还原风控状态机（见 §5.1） |
| 参数扫描（辅助） | vectorbt（可选） | 后期引入 | 不作主回测引擎 |
| 实盘接口 | **xtquant** | MiniQMT | Windows 友好，A 股主流 |
| 调度 | APScheduler | 3.10+ | 日线收盘后定时巡检 |
| 配置 | pydantic-settings | - | 类型安全 + .env 支持 |
| 日志 | loguru | - | 工业级，零配置 |
| 测试 | pytest + pytest-cov | - | 用 samples 数据作回归基线 |
| 报告 | Jinja2 + svgwrite | - | 复刻 samples/reports 格式 |

### 3.1 关键选型论证

#### 为什么不用 vectorbt 做主回测引擎

你的策略 70% 逻辑是**状态依赖**：
- 月内交易次数 ≤ 3、连亏 3 笔停手（全局状态机）
- 单票 ≤ 25%、板块 ≤ 40%（持仓快照查询）
- -3% 预警 → -5% 执行 → -8% 强制（事件触发链）
- 移动止盈回撤 +3% 或破 10 日线（条件订单）
- 九转序列逐日计数（状态机）

vectorbt 的向量化模型假设每个时点独立决策，**无法表达上述逻辑**。它适合做"金叉扫描 + MA 参数网格"这种纯信号研究，但无法仿真实盘。

#### 为什么不用 Qlib

- Qlib 是 ML/DL 选股平台，你的策略是**规则型**，框架束缚大
- 自定义六维诊断、九转序列、Checklist 闸门需侵入式改造
- 实盘衔接不友好，与 MiniQMT 对接难

#### 为什么 DuckDB 而非 SQLite

日线策略核心操作是"取某股票某时段 OHLCV → 算指标 → 回测"，典型 OLAP 负载。DuckDB 列存 + 向量化执行比 SQLite 快一个数量级，且原生支持 pandas DataFrame 查询与 Parquet 归档。

---

## 4. 数据层设计

### 4.1 Tushare 接口清单（13+ 接口，已对齐 samples/tushare/）

| 分类 | 接口 | 用途 | samples 参考文件 |
|---|---|---|---|
| 行情 | `daily` | 日线 OHLCV | daily_600584.SH_20260818.json |
| 行情 | `weekly` | 周线（辅助判断） | weekly_600584.SH_20260817.json |
| 行情 | `daily_basic` | 每日 PE/PB/换手率 | daily_basic_600584.SH_20260818.json |
| 行情 | `index_daily` | 指数（000001.SH 锚定） | index_daily_000001.SH_20260819.json |
| 行情 | `sw_daily` | 申万行业日行情 | sw_daily_20260819.json |
| 行情 | `limit_list_d` | 涨跌停列表 | limit_list_d_20260819.json |
| 资金 | `moneyflow` | 个股资金流 | moneyflow_600584.SH_20260818.json |
| 资金 | `moneyflow_dc` | 东财个股资金流（含主力净流入） | - |
| 资金 | `moneyflow_ind_dc` | 东财板块资金流 | - |
| 财务 | `income` | 利润表 | income_600584.SH_20260817.json |
| 财务 | `fina_indicator` | 财务指标（ROE/毛利率） | fina_indicator_600584.SH_20260817.json |
| 财务 | `forecast` | 业绩预告 | forecast_600584.SH_20260819.json |
| 财务 | `express` | 业绩快报 | express_600584.SH_20260819.json |
| 消息 | `major_news` | 重大新闻 | major_news_20260819.json |
| 基金 | `fund_daily` | ETF 行情 | fund_daily_159622.SZ_20260818.json |
| 概念 | `concept` / `dc_index` / `dc_member` / `ths_index` / `ths_member` | 概念板块与成分 | - |
| 金股 | `broker_recommend` | 券商月度金股 | - |
| 日历 | `trade_cal` | 交易日历 | - |

### 4.2 DuckDB Schema 设计

按"接口一表 + 主键约束"原则设计，便于增量更新。

```sql
-- 日线行情（核心表）
CREATE TABLE daily_quotes (
    ts_code     VARCHAR,        -- 600584.SH
    trade_date  DATE,           -- 2026-08-18
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    vol         DOUBLE,         -- 成交量（手）
    amount      DOUBLE,         -- 成交额（千元）
    pct_chg     DOUBLE,         -- 涨跌幅 %
    pre_close   DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);

-- 每日指标（PE/PB/换手率）
CREATE TABLE daily_basic (
    ts_code     VARCHAR,
    trade_date  DATE,
    pe          DOUBLE,
    pe_ttm      DOUBLE,
    pb          DOUBLE,
    total_mv    DOUBLE,         -- 总市值（万元）
    circ_mv     DOUBLE,         -- 流通市值
    turnover_rate DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);

-- 周线行情
CREATE TABLE weekly_quotes (
    ts_code     VARCHAR,
    trade_date  DATE,
    open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    vol         DOUBLE, amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);

-- 指数日线（含 000001.SH 锚点）
CREATE TABLE index_daily (
    ts_code     VARCHAR,        -- 000001.SH
    trade_date  DATE,
    open        DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    vol        DOUBLE, amount DOUBLE,
    pct_chg     DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);

-- 申万行业日线
CREATE TABLE sw_daily (LIKE index_daily);

-- 涨跌停列表
CREATE TABLE limit_list_d (
    trade_date  DATE,
    ts_code     VARCHAR,
    name        VARCHAR,
    close       DOUBLE,
    pct_chg     DOUBLE,
    amp         DOUBLE,         -- 振幅
    fc          DOUBLE,         -- 封成比
    flp          DOUBLE,         -- 封涨停次数
    limit       VARCHAR,        -- 字段名（已修正 limit_type→limit）
    PRIMARY KEY (trade_date, ts_code)
);

-- 资金流（个股）
CREATE TABLE moneyflow (
    ts_code     VARCHAR,
    trade_date  DATE,
    buy_sm_amount  DOUBLE, sell_sm_amount  DOUBLE,
    buy_md_amount  DOUBLE, sell_md_amount  DOUBLE,
    buy_lg_amount  DOUBLE, sell_lg_amount  DOUBLE,
    buy_elg_amount DOUBLE, sell_elg_amount DOUBLE,
    net_amount     DOUBLE,     -- 主力净流入
    PRIMARY KEY (ts_code, trade_date)
);

-- 东财个股资金流（含主力净流入）
CREATE TABLE moneyflow_dc (LIKE moneyflow);

-- 财务（利润表）
CREATE TABLE income (
    ts_code     VARCHAR,
    end_date    DATE,           -- 报告期 2025-12-31
    revenue     DOUBLE,
    n_income    DOUBLE,         -- 归母净利
    PRIMARY KEY (ts_code, end_date)
);

-- 财务指标
CREATE TABLE fina_indicator (
    ts_code     VARCHAR,
    end_date    DATE,
    roe         DOUBLE,
    grossprofit_margin DOUBLE,
    PRIMARY KEY (ts_code, end_date)
);

-- 业绩预告 / 快报
CREATE TABLE forecast (ts_code VARCHAR, end_date DATE, ann_date DATE, type VARCHAR, p_change_min DOUBLE, p_change_max DOUBLE, profit_min DOUBLE, profit_max DOUBLE);
CREATE TABLE express (ts_code VARCHAR, end_date DATE, revenue DOUBLE, n_income DOUBLE, yoy_net_profit DOUBLE);

-- 重大新闻
CREATE TABLE major_news (trade_date DATE, ts_code VARCHAR, title VARCHAR, content VARCHAR, pub_time TIMESTAMP);

-- 交易日历
CREATE TABLE trade_cal (cal_date DATE, is_open INT, pretrade_date DATE);

-- ETF 行情
CREATE TABLE fund_daily (LIKE daily_quotes);
```

### 4.3 缓存与增量更新策略

继承 `dsh-invest-plugin` 的缓存纪律：

| 接口 | 缓存粒度 | 增量策略 |
|---|---|---|
| `daily` / `weekly` / `index_daily` / `sw_daily` | 按 ts_code 全量历史 | 启动时按 `max(trade_date)` 增量拉取 |
| `daily_basic` / `moneyflow` | 按 ts_code 增量 | 同上 |
| `income` / `fina_indicator` | 按 ts_code 全量 | 每季报披露后增量 |
| `limit_list_d` / `major_news` | 按 trade_date | 当日全量替换 |
| `trade_cal` | 全量 | 年初拉一次 + 增量 |

**交易日锚定铁律**（来自原系统）：
```python
# 禁止用 datetime.now() 判断"今天"
# 必须用 index_daily(000001.SH, end_date=年末) 取 max(trade_date) 作为真实最新交易日
latest_td = data.get_latest_trade_date()
# 所有行情查询 end_date 用 latest_td，start_date 往前推 60-120 自然日
```

### 4.4 数据层接口

```python
# src/lihu_quantify/data/tushare_client.py
class TushareClient:
    def __init__(self, token: str, cache_dir: Path): ...
    def query(self, api_name: str, params: dict) -> pd.DataFrame: ...
    # 内部：缓存命中检查 → 调 API → 写缓存 → 返回

# src/lihu_quantify/data/duckdb_store.py
class DuckDBStore:
    def __init__(self, db_path: Path): ...
    def upsert(self, table: str, df: pd.DataFrame) -> int: ...   # 返回写入行数
    def query(self, sql: str, params: dict) -> pd.DataFrame: ...
    def get_latest_trade_date(self) -> date: ...
    def get_daily(self, ts_code: str, start: date, end: date) -> pd.DataFrame: ...
    def get_latest_n(self, ts_code: str, n: int) -> pd.DataFrame: ...

# src/lihu_quantify/data/data_manager.py
class DataManager:
    """对外统一入口：先查 DuckDB，未命中则拉 Tushare 增量"""
    def __init__(self, client: TushareClient, store: DuckDBStore): ...
    def ensure_daily(self, ts_code: str, days: int = 120) -> pd.DataFrame: ...
    def refresh_universe(self) -> None: ...   # 全市场股票池刷新
```

---

## 5. 指标层设计

### 5.1 标准指标（pandas-ta）

```python
# src/lihu_quantify/indicators/standard.py
import pandas as ta

def add_ma(df: pd.DataFrame, periods=(5, 10, 20, 60)) -> pd.DataFrame:
    """MA5/10/20/60 —— 三层过滤与均线系统诊断依赖"""
    for p in periods:
        df[f"ma{p}"] = ta.sma(df["close"], length=p)
    # MA20 斜率（向上判定）
    df["ma20_slope"] = df["ma20"].diff(5) / 5
    return df

def add_macd(df: pd.DataFrame, fast=12, slow=26, signal=9) -> pd.DataFrame:
    macd = ta.macd(df["close"], fast=fast, slow=slow, signal=signal)
    df[["dif", "dea", "macd_hist"]] = macd
    return df

def add_boll_rsi(df: pd.DataFrame) -> pd.DataFrame:
    boll = ta.bbands(df["close"], length=20, std=2)
    df[["boll_up", "boll_mid", "boll_low"]] = boll
    df["rsi14"] = ta.rsi(df["close"], length=14)
    return df
```

### 5.2 自研：九转序列（TD Sequential）

按原系统 prompts.js 的定义实现：

> 收盘价与 4 根前收盘价比较逐日计数
> - 连续 9 根收盘 > 4 根前收盘 → 卖出序列（计数 1-9，9 为衰竭点）
> - 连续 9 根收盘 < 4 根前收盘 → 买入序列

```python
# src/lihu_quantify/indicators/td_sequential.py
def td_sequential(close: pd.Series) -> pd.DataFrame:
    """
    返回 DataFrame:
        td_count       int   序列计数（1..9，0 表示无序列）
        td_dir         str   'buy' | 'sell' | None
        td_exhausted   bool  是否在第9根衰竭
    算法：
        比较 close[i] vs close[i-4]
        连续同向则 +1，断则重置为 1（方向反转时）
        达到 9 标记 exhausted=True，序列重置
    """
    ...
```

**回归验证基线**：长电科技 600584.SH 在 2026-08-06 ~ 2026-08-18 应输出"卖出序列 1→9 完整周期"，8/18 第 9 根衰竭点（收盘 85.42），8/19 跌破 4 日前收盘（77.74<77.82）确认序列终结。见 `samples/reports/长电科技_深度分析_含Checklist闸门.md` §10。

### 5.3 自研：MACD 背离检测

```python
# src/lihu_quantify/indicators/divergence.py
def detect_macd_divergence(df: pd.DataFrame, window: int = 60) -> list[dict]:
    """
    返回背离点列表：
        [{
            'date': date,
            'type': 'top' | 'bottom',
            'level': '本级别' | '次级别',
            'price_high': float,
            'macd_peak': float,
            'desc': '价格创新高但 MACD 峰值走低'
        }]
    算法：
        1. scipy.signal.find_peaks 在价格与 MACD 上找局部峰/谷
        2. 对比相邻峰：价格新高 + MACD 走低 = 顶背离
        3. 对比相邻谷：价格新低 + MACD 走高 = 底背离
    """
```

### 5.4 蜡烛图形态识别（最高优先级预警）

按 prompts.js 的要求实现：

```python
# src/lihu_quantify/indicators/candlestick.py
def detect_warning(df: pd.DataFrame) -> list[dict]:
    """
    返回预警列表（报告最前展示）：
        - 射击之星（上影 > 实体 3 倍）
        - 光头阴线
        - 黄昏之星
        - 看跌吞没
        - 天量滞涨
    每项返回 {date, pattern, action: '清仓'|'减仓', reason}
    """
```

---

## 6. 策略层设计

### 6.1 策略基类

回测与实盘**共用**同一接口，确保策略逻辑不重写。

```python
# src/lihu_quantify/strategy/base.py
class StrategyBase(ABC):
    @abstractmethod
    def on_bar(self, ctx: BarContext) -> Signal | None:
        """每根日线被推送时调用，返回信号或 None"""

    @abstractmethod
    def on_init(self, ctx: Context): ...
    def on_stop(self, ctx: Context): ...

@dataclass
class BarContext:
    bar: pd.Series              # 当根 OHLCV
    history: pd.DataFrame       # 历史窗口
    indicators: dict            # 已计算指标
    position: Position          # 当前持仓
    account: AccountSnapshot    # 账户状态

@dataclass
class Signal:
    kind: Literal['buy', 'sell', 'reduce', 'hold']
    ts_code: str
    suggested_price: float
    stop_loss: float            # 强制给出止损价
    take_profit: list[float]    # L1-L4 目标价
    suggested_position_pct: float  # 建议仓位（≤25%）
    reason: str
```

### 6.2 CherryClaw 三层过滤选股

```python
# src/lihu_quantify/strategy/cherry_claw.py
class CherryClaw(StrategyBase):
    """对应 prompts.js P_SELECT 的核心规则"""

    # 前置硬过滤（不可配置）
    EXCLUDED_PREFIX = ('688', '300', '301')   # 科创/创业板
    MIN_LIST_DAYS = 60
    MIN_AVG_AMOUNT_20D = 1e8                  # 20 日日均成交额 ≥ 1 亿

    def pre_filter(self, universe: pd.DataFrame) -> pd.DataFrame:
        """排除 ST/次新/低成交额/科创创业"""

    def three_layer_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        三层过滤（核心信号）：
            1. MA5 上穿 MA10 金叉 + 金叉新鲜度 ≤ 7 天
            2. 量比 > 1.0 + 实体占比 ≥ 40% + 收红
            3. 收盘贴近 MA5 + MA20 斜率向上
        """

    def rebound_spear(self, df: pd.DataFrame) -> pd.DataFrame:
        """涨停回马枪：近 10 日实体涨停 → 回调缩量（30-60%）→ 止跌买点"""

    def leader_first_board(self, df: pd.DataFrame) -> pd.DataFrame:
        """龙头首板/爆阳二板：封板时间 + 封单 + 板块梯队"""
```

### 6.3 六维诊断

```python
# src/lihu_quantify/strategy/deep_diagnose.py
class DeepDiagnose:
    """对应 prompts.js P_DEEP 的分析流程"""

    def run(self, ts_code: str) -> DeepReport:
        # 1. 蜡烛图检查（最高优先级预警）
        candle_warnings = detect_warning(history)
        # 2. 六维诊断
        six_dim = self._six_dim_diagnose(history, fundamentals, moneyflow, sector)
        # 3. 财务与估值
        valuation = self._valuate(fundamentals, daily_basic, sector_peers)
        # 4. L1-L4 目标价
        targets = self._calc_targets(history, six_dim)
        # 5. 止损方案
        stop_plan = self._stop_plan(history, six_dim)
        # 6. 综合评分 S/A/B/C/D
        rating = self._rating(six_dim, valuation, candle_warnings)
        # 7. 九转序列 + MACD 背离
        td_state = td_sequential(history['close']).iloc[-1]
        divergences = detect_macd_divergence(history)
        return DeepReport(...)
```

### 6.4 L1-L4 目标价

| 层 | 含义 | 触发条件 |
|---|---|---|
| L1 | 通道上轨（减 1/3） | 缩量企稳后收复 MA10 |
| L2 | 量度目标（再减 1/3） | 放量站稳近期高点 |
| L3 | 突破延伸 | 有效突破前高 |
| L4 | 周线机会 | 周线收复颈线区 |

每层标注**概率**与**触发条件**，参考 samples/reports 中长电科技案例。

---

## 7. 风控层设计（最重要）

### 7.1 Checklist 8 项强制闸门

**铁则**：任一栏"拒绝" → 强制拦截，当天不许以任何理由下单。

```python
# src/lihu_quantify/risk/checklist.py
class ChecklistGate:
    """对应 docs/开仓前强制Checklist.md"""

    def check(self, signal: Signal, account: AccountSnapshot) -> ChecklistResult:
        items = [
            self._check_position(signal, account),       # 1. 仓位预算 ≤25%
            self._check_sector(signal, account),        # 2. 板块合计 ≤40%
            self._check_stop_loss(signal),              # 3. 止损价必须给出
            self._check_take_profit(signal),            # 4. 止盈规则必须给出
            self._check_frequency(signal, account),     # 5. 月内 ≤3 次 + 连亏 3 停手
            self._check_chasing_high(signal),          # 6. 乖离 10 日线 ≤8%
            self._check_fundamentals(signal),           # 7. 基本面核验
            self._check_psychology(account),           # 8. 心理门禁
        ]
        approved = all(i.approved for i in items)
        return ChecklistResult(items=items, approved=approved)
```

### 7.2 三档止损机制

```python
# src/lihu_quantify/risk/stop_loss.py
class StopLossManager:
    """铁律：成本 -8% 或跌破 10 日线，无条件离场"""

    TIERS = {
        'warn':   -0.03,   # -3% 预警（贴近 MA20）
        'exec':   -0.05,   # -5% 执行
        'force':  -0.08,   # -8% 强制清仓
    }

    def evaluate(self, position: Position, bar: pd.Series) -> StopAction:
        # 比对成本、当前价、MA10、MA20
        # 任一触发 → 返回相应 action
        # 移动止盈：盈利回撤至 +3% 或破 10 日线离场
```

### 7.3 仓位与频率状态机

```python
# src/lihu_quantify/risk/position_limit.py
class PositionLimiter:
    MAX_SINGLE = 0.25        # 单票 ≤25%
    MAX_SECTOR = 0.40        # 同板块 ≤40%

    def can_add(self, ts_code: str, amount: float, account: AccountSnapshot) -> bool: ...

# src/lihu_quantify/risk/frequency.py
class FrequencyGuard:
    """月内 ≤3 次 + 连亏 3 笔停手一个月"""
    MAX_TRADES_PER_TICKER_MONTH = 3
    HALT_AFTER_CONSEC_LOSSES = 3
    HALT_DAYS = 30

    def can_trade(self, ts_code: str, account: AccountSnapshot) -> bool: ...
```

---

## 8. 回测引擎设计

### 8.1 事件驱动架构

```python
# src/lihu_quantify/backtest/engine.py
class EventDrivenEngine:
    """
    逐 bar 推送 → 策略 on_bar() → 信号 → 风控闸门 → 撮合 → 持仓更新
    完整还原状态依赖逻辑（与 vectorbt 的核心区别）
    """
    def run(self, strategy: StrategyBase, universe: list[str],
            start: date, end: date, init_capital: float) -> BacktestResult:
        # 1. 加载历史数据（按 trade_date 升序）
        # 2. 每根 bar：
        #    a. 推送给策略 on_bar()
        #    b. 若有信号 → Checklist 闸门校验
        #    c. 通过 → 进入撮合队列
        #    d. 撮合（按下一根 bar 的 open 或 close）
        #    e. 更新持仓状态机
        #    f. 检查止损/止盈触发
        # 3. 输出绩效
```

### 8.2 撮合模型

```python
# src/lihu_quantify/backtest/broker.py
class SimulatedBroker:
    COMMISSION = 0.00025       # 万 2.5（A 股佣金）
    STAMP_TAX = 0.0001         # 卖出印花税千 1
    SLIPPAGE = 0.001           # 滑点 0.1%

    def fill(self, order: Order, next_bar: pd.Series) -> Fill:
        # 市价单：next_bar.open ± slippage
        # 限价单：触及 limit_price 才成交
        # 计算手续费 + 印花税
```

### 8.3 持仓状态机

```python
# src/lihu_quantify/backtest/portfolio.py
class Portfolio:
    """维护账户、持仓、交易历史，驱动频率/连亏/仓位状态机"""

    def update(self, fill: Fill, bar_date: date): ...
    def sector_exposure(self) -> dict[str, float]: ...   # 按板块聚合
    def trade_count_this_month(self, ts_code: str) -> int: ...
    def consecutive_losses(self, ts_code: str) -> int: ...
```

### 8.4 绩效指标

```python
# src/lihu_quantify/backtest/metrics.py
def compute_metrics(equity_curve: pd.Series, trades: list[Trade]) -> dict:
    return {
        'total_return': ...,
        'annual_return': ...,        # 年化
        'sharpe': ...,                # 夏普
        'max_drawdown': ...,          # 最大回撤
        'calmar': ...,                # 卡玛
        'win_rate': ...,              # 胜率
        'profit_loss_ratio': ...,     # 盈亏比（铁律目标 >1）
        'avg_holding_days': ...,      # 平均持仓天数
        'stop_loss_exec_rate': ...,   # 止损执行率（铁律目标 100%）
        'monthly_trade_count': ...,   # 月均交易次数
    }
```

**关键校验**：绩效输出必须能填充 `docs/月度复盘模板.md` 的所有字段。

---

## 9. 执行层设计（MiniQMT 实盘）

### 9.1 xtquant 客户端

```python
# src/lihu_quantify/execution/xtquant_client.py
from xtquant import xttrader, xtdata

class MiniQMTClient:
    """MiniQMT (xtquant) 接口封装"""

    def __init__(self, path: str, account: str): ...

    def connect(self) -> bool:
        # 启动 QMT 客户端进程 + 登录

    def buy(self, ts_code: str, price: float, volume: int) -> str: ...
    def sell(self, ts_code: str, price: float, volume: int) -> str: ...
    def cancel(self, order_id: str) -> bool: ...
    def place_stop_order(self, ts_code: str, trigger_price: float) -> str:
        """条件单：跌破止损价自动卖出"""

    def query_position(self) -> list[Position]: ...
    def query_account(self) -> AccountSnapshot: ...
```

### 9.2 OMS（订单管理）

```python
# src/lihu_quantify/execution/oms.py
class OrderManagementSystem:
    """
    铁律：买入单 + 止损条件单必须同时挂出（两步合一，不许分开下单）
    """

    def place_buy_with_stop(self, signal: Signal) -> tuple[str, str]:
        """同时挂买入单 + 止损条件单，任一失败则撤单另一"""
```

### 9.3 实盘灰度路径

```
模拟盘（paper_trade.py，全逻辑仿真）
    ↓ 验证策略、风控、绩效对齐回测
小资金实盘（5% 仓位）
    ↓ 验证 xtquant 接口稳定性
中资金（20%）
    ↓ 验证执行质量、滑点、回撤
全量
```

---

## 10. 监控层设计

### 10.1 调度

```python
# src/lihu_quantify/monitor/scheduler.py
from apscheduler.schedulers.blocking import BlockingScheduler

def setup_scheduler():
    sched = BlockingScheduler(timezone='Asia/Shanghai')
    # 收盘后巡检（A 股 15:00 收盘，留 30 分钟数据延迟）
    sched.add_job(daily_scan, 'cron', hour=15, minute=30, day_of_week='mon-fri')
    # 月末复盘
    sched.add_job(monthly_review, 'cron', day='last', hour=16)
    return sched
```

### 10.2 报告生成

参考 `samples/reports/` 的格式：

```python
# src/lihu_quantify/monitor/report.py
class ReportGenerator:
    def daily_report(self, scan_result, account) -> str:
        # Markdown 报告（参考 samples/reports/*.md 格式）
        # 含：候选股票列表 / 选股逻辑 / 市场情绪 / 风险提示 / 免责声明
    def deep_report(self, deep_result) -> str:
        # 蜡烛图检查 / 六维诊断 / 财务估值 / L1-L4 / 止损 / 评分 / 多空理由 / 风险 / 九转+背离
    def chart(self, ts_code: str, kind: str) -> Path:
        # SVG 走势图（K线+MA+关键价位+九转计数）/ MACD 背离图
        # 必须有：标题 / 图例 / 坐标轴 / 关键价位水平线
```

### 10.3 告警

- 微信（Server酱）/ 邮件
- 触发：Checklist 拒绝、止损触发、连亏 3 笔、连亏停手、API 异常

---

## 11. 项目目录结构

```
e:\LihuQuantify\
├── pyproject.toml                # 依赖 + 工具配置
├── README.md
├── .env.example                  # Tushare token、QMT 路径
├── config/
│   ├── settings.yaml             # 全局配置
│   └── strategy_params.yaml      # 策略参数（金叉周期/量比阈值/止损档位）
├── docs/
│   ├── ARCHITECTURE.md           # 本文档
│   ├── 交易铁律.md                # 从 dsh-invest-plugin 同步
│   ├── 开仓前强制Checklist.md
│   └── 月度复盘模板.md
├── src/lihu_quantify/
│   ├── __init__.py
│   ├── config.py                 # pydantic-settings 配置加载
│   ├── data/                     # 数据层
│   │   ├── tushare_client.py
│   │   ├── duckdb_store.py
│   │   └── data_manager.py
│   ├── indicators/               # 指标层
│   │   ├── standard.py           # MA/MACD/BOLL/RSI (pandas-ta)
│   │   ├── td_sequential.py      # 九转序列（自研）
│   │   ├── divergence.py         # MACD 背离（自研）
│   │   └── candlestick.py        # 蜡烛图形态
│   ├── strategy/                 # 策略层
│   │   ├── base.py               # StrategyBase + on_bar 接口
│   │   ├── cherry_claw.py        # 三层过滤选股
│   │   ├── rebound_spear.py      # 涨停回马枪
│   │   ├── leader_first.py      # 龙头首板
│   │   └── deep_diagnose.py     # 六维诊断
│   ├── risk/                     # 风控层
│   │   ├── checklist.py          # 8 项闸门
│   │   ├── stop_loss.py          # 三档止损
│   │   ├── position_limit.py     # 仓位/板块限制
│   │   └── frequency.py          # 月3次/连亏3停手
│   ├── backtest/                 # 回测层
│   │   ├── engine.py             # 事件驱动
│   │   ├── broker.py             # 撮合
│   │   ├── portfolio.py          # 持仓状态机
│   │   └── metrics.py            # 绩效指标
│   ├── execution/               # 执行层
│   │   ├── xtquant_client.py    # MiniQMT
│   │   ├── oms.py                # 订单管理
│   │   └── paper_trade.py       # 模拟盘
│   ├── monitor/                  # 监控层
│   │   ├── scheduler.py          # APScheduler
│   │   ├── alerts.py             # 告警
│   │   └── report.py             # 报告生成
│   └── cli.py                    # CLI 入口
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   └── 600584.SH_20260818.json   # 复制自 samples/tushare/
│   ├── test_td_sequential.py        # 九转序列回归（基线：长电科技 8/6-8/18）
│   ├── test_divergence.py
│   ├── test_checklist.py
│   └── test_backtest.py
├── data/
│   └── lihu_quant.duckdb           # DuckDB 数据库文件
└── outputs/                         # 报告与图表归档
    ├── reports/
    └── charts/
```

---

## 12. 配置管理

```yaml
# config/settings.yaml
tushare:
  token_file: E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/tushare.token
  # 或直接 .env: TUSHARE_TOKEN=xxx

duckdb:
  path: ./data/lihu_quant.duckdb

qmt:
  path: "C:/国金QMT/bin.x64"   # MiniQMT 安装路径
  account: "your_account_id"
  enabled: false               # 默认禁用实盘，仅模拟

strategy:
  excluded_prefix: ["688", "300", "301"]
  min_list_days: 60
  min_avg_amount_20d: 100000000
  ma_periods: [5, 10, 20, 60]
  golden_cross_max_freshness_days: 7
  volume_ratio_threshold: 1.0
  entity_ratio_threshold: 0.40

risk:
  max_single_position: 0.25
  max_sector_position: 0.40
  stop_loss_warn: -0.03
  stop_loss_exec: -0.05
  stop_loss_force: -0.08
  max_trades_per_ticker_month: 3
  halt_after_consec_losses: 3
  halt_days: 30
  chasing_high_threshold: 0.08   # 乖离 10 日线 8% 视为追高

backtest:
  commission: 0.00025
  stamp_tax: 0.0001
  slippage: 0.001

scheduler:
  daily_scan: "15:30 mon-fri"
  monthly_review: "last day 16:00"
```

```python
# src/lihu_quantify/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    tushare_token: str
    duckdb_path: Path
    qmt_enabled: bool = False
    # ... 完整映射
    class Config:
        env_file = ".env"
        env_prefix = "LIHU_"
```

---

## 13. 测试策略

### 13.1 回归测试基线

用 `samples/tushare/` 中的真实数据作为黄金样本：

| 测试 | 输入 | 期望输出（来自 samples/reports） |
|---|---|---|
| 九转序列 | 600584.SH 2026-08-06 ~ 2026-08-18 日线 | 卖出序列 1→9，8/18 第 9 根衰竭（收 85.42） |
| MACD 背离 | 600584.SH 60 日数据 | 36 日窗口无标准背离（DIF 全程零轴下） |
| 蜡烛图预警 | 600584.SH 8/19 数据 | "天量长阴 + 光脚大阴线 + 减仓预警" |
| Checklist | 长电科技案例 + 用户账户快照 | "板块合计 57.1% > 40% → 拒绝" |

### 13.2 测试金字塔

- **单元测试**：指标计算、风控规则、撮合逻辑
- **集成测试**：数据层→指标层→策略层链路
- **回测验证**：在 600584 上跑完整策略，对齐 samples 报告
- **模拟盘验证**：xtquant paper trade 跑 1 个月

---

## 14. 分阶段实施路线图

### 阶段 1：MVP 数据 + 指标闭环（先做这个）

**目标**：跑通"取数据 → 算指标 → 输出九转序列+MACD背离"的最小闭环，用 600584 验证。

**交付**：
1. 项目骨架（pyproject.toml + 目录 + config 模板）
2. TushareClient + DuckDBStore + DataManager
3. 指标层：MA/MACD/BOLL/RSI（pandas-ta）+ 九转序列 + MACD背离（自研）+ 蜡烛图
4. 回归测试：600584 samples 数据，输出对齐 reports

**验证标准**：九转序列测试用例输出"8/6-8/18 卖出序列 1→9，8/18 第 9 根衰竭"。

### 阶段 2：策略 + 风控

5. 策略基类 + CherryClaw 选股（三层过滤）
6. 六维诊断 + L1-L4 目标价 + 综合评分
7. Checklist 闸门 + 三档止损 + 仓位/频率状态机
8. 全市场扫描验证

### 阶段 3：回测引擎

9. 事件驱动引擎 + 撮合 + 持仓状态机
10. 绩效指标 + 月度复盘报告填充
11. 历史回测 + 参数优化（可叠加 vectorbt 做扫描）

### 阶段 4：实盘对接

12. xtquant 客户端 + OMS
13. 模拟盘 1 个月验证
14. 小资金实盘灰度

### 阶段 5：监控告警 + 部署

15. APScheduler 巡检调度
16. 微信/邮件告警
17. Markdown 报告 + SVG 图表生成
18. Windows 任务计划程序/服务部署

---

## 15. 关键约束与铁律（必须内化为代码）

来自 `docs/交易铁律.md` 与 `docs/开仓前强制Checklist.md`：

1. **先写止损，再点买入** —— 买入单 + 止损条件单必须同时挂出
2. **成本 -8% 或跌破 10 日线，无条件离场** —— 三档止损机制
3. **单票 ≤25%，同板块 ≤40%** —— PositionLimiter 硬约束
4. **绝不向下补仓** —— OMS 禁止补仓逻辑
5. **让盈利奔跑，让亏损快走** —— 移动止盈回撤 +3% 或破 10 日线
6. **同一只票一个月 ≤3 次；连亏 3 笔，停手一个月** —— FrequencyGuard 状态机
7. **错过就错过，不追高** —— Checklist 第 6 项（乖离 10 日线 >8% 拒绝）
8. **不预测，只应对** —— 进场前写好剧本（止损/止盈/时间），到点就演

---

## 16. 待确认事项

进入阶段 1 实现前，需要确认：

1. **Tushare token 路径**：是否复用 `E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/tushare.token`？
2. **MiniQMT 安装路径**：默认 `C:/国金QMT/bin.x64` 是否正确？
3. **初始资金**：模拟盘用多少？影响仓位计算
4. **股票池范围**：全市场还是只主板的某子集？影响首期数据量
5. **策略参数**：MA 周期/量比阈值等是否沿用原系统默认值，还是有调整？

---

## 17. 免责声明

本系统输出的所有分析内容**仅供参考，不构成任何投资建议**。投资有风险，入市需谨慎。数据来自 Tushare 与公开网络信息，可能存在延迟、缺失或错误。

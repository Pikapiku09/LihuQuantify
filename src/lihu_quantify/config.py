"""配置加载：环境变量 + YAML → pydantic-settings 类型安全配置。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 第十轮需求1：密钥文件（两容器共享 data/ 卷；设置界面写入，调度器读取）。
# 相对路径与 duckdb/cache 同约定（容器内 cwd=/app）。
SECRETS_FILE = "data/secrets.json"


def read_secrets(path: str = SECRETS_FILE) -> dict:
    """读 data/secrets.json（不存在/损坏 → 空 dict，绝不抛异常）。

    存 {"ai_summary_api_key": "...", "email_auth_code": "..."}；
    与 .env 相比的优势：NAS 上两个容器都挂载 data/，改 key 无需改 compose。
    """
    try:
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


class TushareConfig(BaseModel):
    token: str = ""
    token_file: str = "E:/Dsh_WorkSapce/Dify_Agents/.dsh-invest/tushare.token"
    cache_dir: str = "./data/cache"
    # P2-9-3（第十一轮）：cache_ttl 缓存有效期、rate_limits 限速（config 与实现对齐）
    cache_ttl_seconds: int = 43200            # 12h 缓存有效期
    rate_limit_interval: float = 0.3          # 同一 API 相邻请求最小间隔（秒）


class DuckDBConfig(BaseModel):
    path: str = "./data/lihu_quant.duckdb"


class QMTConfig(BaseModel):
    enabled: bool = False
    path: str = "C:/国金QMT/bin.x64"
    account: str = ""


class UniverseConfig(BaseModel):
    pool_mode: str = "strat"   # strat=成交额分层抽样 | head=代码排序前N（旧）
    pool_size: int = 200
    pool_layers: int = 5
    pool_seed: int = 42
    exclude_prefix: list[str] = Field(default_factory=lambda: ["688", "300", "301"])
    exclude_st: bool = True
    min_list_days: int = 60
    min_avg_amount_20d: float = 1e8


class StrategyConfig(BaseModel):
    ma_periods: list[int] = Field(default_factory=lambda: [5, 10, 20, 60])
    golden_cross_max_freshness_days: int = 7
    volume_ratio_threshold: float = 1.0
    entity_ratio_threshold: float = 0.40
    close_to_ma5_max_dev: float = 0.015
    market_filter: bool = True   # 市场状态参考信号开关
    market_filter_mode: str = "reduce"   # reduce=减仓 | block=禁止（已判脆弱弃用）

    @property
    def chasing_high_threshold(self) -> float:
        """追高阈值：单一事实来源 = risk.chasing_high_threshold（P2-9 收敛双定义）。

        Strategy 不再独立持有该值，调用方仍以 `s.chasing_high_threshold` 访问
        即得 risk 段数值；改阈值只动 settings.yaml `risk:` 段。
        """
        return get_settings().risk.chasing_high_threshold


class RiskConfig(BaseModel):
    max_single_position: float = 0.25
    max_sector_position: float = 0.40
    stop_loss_warn: float = -0.03
    stop_loss_exec: float = -0.05
    stop_loss_force: float = -0.08
    trailing_profit_pullback: float = 0.03
    trailing_break_ma: int = 10
    max_trades_per_ticker_month: int = 3
    halt_after_consec_losses: int = 3
    halt_days: int = 30
    chasing_high_threshold: float = 0.08


class BacktestConfig(BaseModel):
    commission: float = 0.00025
    stamp_tax: float = 0.0005   # P2-9-1：2023.8 后卖出印花税万 5（原代码误为 0.0001，对齐 settings.yaml）
    slippage: float = 0.001


class SchedulerConfig(BaseModel):
    daily_scan_cron: str = "30 16 * * 1-5"   # 第四轮清单3：16:30（日线完整）
    monthly_review_cron: str = "0 16 * * L"
    timezone: str = "Asia/Shanghai"


class HeartbeatConfig(BaseModel):
    """第四轮清单2：healthchecks.io 缺席心跳。url 空=不 ping。"""
    healthchecks_url: str = ""


class EmailAlertConfig(BaseModel):
    """第四轮清单1：SMTP 邮件告警。"""
    enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    username: str = ""
    auth_code: str = ""
    to: list[str] = Field(default_factory=list)
    send_daily_digest: bool = True


class AlertConfig(BaseModel):
    serverchan_key: str = ""   # Server酱 SendKey（空=仅控制台）
    email: EmailAlertConfig = Field(default_factory=EmailAlertConfig)


class IndicatorsConfig(BaseModel):
    td_sequential_lookback: int = 4
    td_sequential_exhaustion: int = 9
    divergence_window: int = 60
    divergence_min_window: int = 36


class CapitalGuardConfig(BaseModel):
    """第八轮需求1：智能资金控制（买入阶段的资金守卫与 top-N 筛选）。

    仅影响买入尝试的组织方式，不改变闸门/铁律/仓位上限本身：
      - cash < min_cash_threshold       → 本轮跳过全部买入尝试（一条汇总告警，
                                           不再产生逐票"资金不足"噪音拒绝）
      - 过闸信号数 > 可买槽数（cash/单笔预算）且 top_n_enabled
                                        → 按信号评分排序，只买前 top_n 只
    """
    enabled: bool = False
    min_cash_threshold: float = 5000.0   # 可用现金低于此值 → 跳过买入
    top_n_enabled: bool = False          # 资金紧张时启用 top-N 筛选（冻结期默认关）
    top_n: int = 5                       # 保留的候选数量上限


class HeatmapConfig(BaseModel):
    """第八轮需求2：看板热力图（日级刷新，基于 DuckDB 巡检缓存）。"""
    enabled: bool = True
    refresh_seconds: int = 30            # 前端轮询间隔（数据本身为日级）


class AiSummaryConfig(BaseModel):
    """第八轮清单（AI 收盘总结）：LLM 生成日报总结段（纯展示层）。

    硬边界：AI 输出仅用于 .md 报告与邮件日报展示，绝不参与信号/下单/
    止损/风控决策；enabled=false 或 api_key 为空时零调用、零侵入。
    api_key 不入 yaml（settings.yaml 无此键），仅经环境变量注入：
        .env: LIHU_AI_SUMMARY__API_KEY=sk-xxx（pydantic 嵌套 env 覆盖）
    """
    enabled: bool = False                # 填好 key 后开 true；默认关 = 零侵入
    api_base: str = "https://api.xiaomimimo.com/v1"   # OpenAI 兼容端点
    model: str = "mimo-v2.5-pro"         # 选型：Mimo 自用 api（官方 id 小写；备选 DeepSeek
                                         # api.deepseek.com/deepseek-chat、
                                         # qwen-turbo、glm-4-flash，换模型只改这两项）
    api_key: str = ""                    # 仅 .env 注入，绝不进 git/yaml
    timeout: int = 20                    # 秒（超时静默降级，不阻断巡检）
    max_chars: int = 300                 # 总结长度上限


class Settings(BaseSettings):
    """全局配置。环境变量 LIHU_ 前缀覆盖 YAML。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LIHU_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    tushare: TushareConfig = Field(default_factory=TushareConfig)
    duckdb: DuckDBConfig = Field(default_factory=DuckDBConfig)
    qmt: QMTConfig = Field(default_factory=QMTConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    indicators: IndicatorsConfig = Field(default_factory=IndicatorsConfig)
    capital_guard: CapitalGuardConfig = Field(default_factory=CapitalGuardConfig)
    heatmap: HeatmapConfig = Field(default_factory=HeatmapConfig)
    ai_summary: AiSummaryConfig = Field(default_factory=AiSummaryConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)

    init_capital: float = 100000.0

    # 显式环境变量（不嵌套，便于 .env 配置）
    tushare_token: str = ""
    tushare_token_file: str = ""
    cache_dir: str = ""
    duckdb_path: str = ""

    def resolved_tushare_token(self) -> str:
        """解析实际 token：优先环境变量，其次 token 文件。

        token 文件支持两种格式：
        - 纯文本：单行 token 字符串（dsh-invest-plugin 风格）
        - JSON：MCP 配置 {"mcpServers":{"tushareMcp":{"url":"...?token=XXX"}}}
          （从 url 查询参数提取 token）
        """
        if self.tushare_token:
            return self.tushare_token.strip()
        token_file = self.tushare_token_file or self.tushare.token_file
        path = Path(token_file)
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            # 尝试 JSON 解析（MCP 配置格式）
            try:
                import json
                data = json.loads(text)
                url = (
                    data.get("mcpServers", {})
                    .get("tushareMcp", {})
                    .get("url", "")
                )
                if "token=" in url:
                    token = url.split("token=")[-1].split("&")[0]
                    if token:
                        return token
            except (json.JSONDecodeError, AttributeError):
                pass
            # 纯文本格式
            if text:
                return text
        return self.tushare.token.strip()

    def resolved_cache_dir(self) -> Path:
        d = self.cache_dir or self.tushare.cache_dir
        return Path(d)

    def resolved_duckdb_path(self) -> Path:
        p = self.duckdb_path or self.duckdb.path
        path = Path(p)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def resolved_ai_summary_api_key(self) -> str:
        """第十轮需求1：AI key 解析顺序 = data/secrets.json → 环境变量 → 空。

        设置界面写 secrets.json（两容器共享 data/ 卷）→ 调度器下次巡检即生效，
        不再依赖 compose env 映射（.env 的 key 可能根本没进容器）。
        """
        key = read_secrets().get("ai_summary_api_key")
        if key and str(key).strip():
            return str(key).strip()
        return (self.ai_summary.api_key or "").strip()

    def resolved_email_auth_code(self) -> str:
        """第十轮需求1：邮件授权码解析顺序 = data/secrets.json → 环境变量/yaml → 空。"""
        code = read_secrets().get("email_auth_code")
        if code and str(code).strip():
            return str(code).strip()
        return (self.alert.email.auth_code or "").strip()


def load_yaml_config(yaml_path: Path | str = "config/settings.yaml") -> dict:
    """加载 YAML 配置为 dict（供 Settings 合并）。"""
    path = Path(yaml_path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings(yaml_path: str = "config/settings.yaml") -> Settings:
    """获取全局 Settings 单例。YAML 为底，环境变量覆盖。"""
    yaml_data = load_yaml_config(yaml_path)
    # 环境变量由 pydantic-settings 自动读取；YAML 作为默认值注入
    settings = Settings(**yaml_data)
    return settings

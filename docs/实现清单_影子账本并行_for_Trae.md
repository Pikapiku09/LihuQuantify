# 实现清单：影子账本并行（评审进度提速 ×3）

> 目标：同一调度进程内并行跑 2 个影子纸面账本（不同 seed 分层池），评审参考样本积累速度 ×3，**主账本零污染、策略/闸门/铁律零改动**。
> 设计原则：影子账本与主账本共用全部基础设施（进程、DuckDB、Tushare 缓存、调度），只隔离**账本状态**（paper_state / stop_registry / pending_stops）与**池子 seed**。顺序执行（主先、影子后）保证影子命中当日缓存，每日新增 API 请求趋近于零。
> 统计纪律：**评审统计以主账本为准**；影子账本仅作跨池稳健性对照（同期账本间有相关性，不得当独立样本简单合并）。完成后决策日志登记。

## ⚠️ 执行纪律

1. 不改策略参数/风控铁律/Checklist/settings 的 strategy-risk 段。
2. `shadow_books` 默认空列表 = 现有行为零变化（所有新代码路径必须以该默认值为前提，老测试全部不改通过）。
3. 影子账本**绝不**写 `last_scan.json`、不发邮件、不调 AI 摘要、不 ping 心跳（防三份日报轰炸 + 幂等污染）。

## 底层现状（已确认，勿重复造轮子）

| 能力 | 现状 | 位置 |
|---|---|---|
| Broker 状态文件参数化 | ✅ `PaperBroker(state_file=...)` | paper_trade.py:74 |
| OMS 注册表参数化 | ✅ `OrderManagementSystem(broker, registry_file=...)` | oms.py:48-59 |
| 池 seed 参数化 + 缓存隔离 | ✅ `build_stratified_pool(seed=)`，缓存名已含 seed+日期 `strat_pool_n200_L5_seed{seed}_{date}.json` | pool.py:34,57 |
| 报告目录参数化 | ✅ `ReportGenerator(dir)` | scheduler.py:326 |
| alerter/broker 注入 | ✅ `DailyScanner(settings, broker, alerter, reporter, mode)` | scheduler.py:283-290 |
| 池 seed 来源 | `settings.universe.pool_seed` | scheduler.py:365 |

需要改的只有：scheduler 的三个硬编码文件路径（pending L981/L1061、last_scan L910/L922）+ 编排层 + 配置 + 看板聚合。

---

## 第一步：配置层（config.py + settings.yaml）

**config.py** 新增（放在 `Settings` 类定义前）：

```python
class ShadowBookConfig(BaseModel):
    """影子账本：同策略不同 seed 池的并行纸面副本（评审参考样本）。"""
    name: str = ""            # 账本名（如 s43），用于状态文件后缀与日志前缀
    seed: int = 42            # 分层池抽样种子（与主账本 42 不同）
```

`Settings` 类加字段（`init_capital` 附近）：

```python
    # 影子账本（第十二轮）：默认空=不启用；评审统计以主账本为准
    shadow_books: list[ShadowBookConfig] = Field(default_factory=list)
```

**settings.yaml** 末尾追加：

```yaml
# 影子账本（第十二轮：多池并行积累参考样本）
# 统计纪律：评审进度/胜率以主账本（paper_state.json）为准；影子仅跨池稳健性对照，
# 同期账本间有市场相关性，不得当独立样本合并。
shadow_books:
  - name: "s43"
    seed: 43
  - name: "s44"
    seed: 44
```

**验收**：`get_settings().shadow_books` 返回 2 个元素；yaml 无该段时默认空列表。

## 第二步：BookSpec + DailyScanner 参数化（scheduler.py）

1. 模块顶部（class DailyScanner 前）新增：

```python
@dataclass
class BookSpec:
    """账本规格：main=主账本（现行行为），其余为影子账本。"""
    name: str = "main"
    pool_seed: int = 42
    silent: bool = False          # 影子=True：无邮件/AI 摘要/心跳/last_scan 写入

    @property
    def is_main(self) -> bool:
        return self.name == "main"

    def suffix(self) -> str:
        return "" if self.is_main else f"_{self.name}"
```

2. `DailyScanner.__init__`（L283）加参数 `book: Optional[BookSpec] = None`，函数体内：
   - `self.book = book or BookSpec()`；
   - **broker 默认构造**（L335-337 区域）：`PaperBroker(init_capital=..., tushare_client=self.client, state_file=None if self.book.is_main else str(_ROOT / f"data/paper_state{self.book.suffix()}.json"))`；
   - **alerter**：`self.book.silent` 时不构建邮件告警（保持 None；确认所有 `self.alerter.xxx` 调用处已 None-safe——现有代码 alerter 参数本就 Optional，逐一确认，不 safe 处加 `if self.alerter:`）；
   - **heartbeat**（L321-325）：`self.book.silent` 时 `Heartbeat("")`（no-op）；
   - **reporter**（L326）：`reporter or ReportGenerator(_ROOT / "outputs" / "reports" / ("" if self.book.is_main else f"shadow_{self.book.name}"))`——注意影子目录需 `mkdir(parents=True, exist_ok=True)`（ReportGenerator 若不自动建目录则在 scanner 里建）。
   - **路径实例属性**：`self._pending_file = _ROOT / "data" / f"pending_stops{self.book.suffix()}.json"`、`self._state_label = "主" if self.book.is_main else f"影子{self.book.name}"`（日志前缀用）。

3. **三处硬编码路径替换**：
   - L981、L1061：`_ROOT / "data" / "pending_stops.json"` → `self._pending_file`；
   - `_scan_impl` 内 OMS 构造（L566）：`oms = OrderManagementSystem(self.broker)` → `oms = OrderManagementSystem(self.broker, registry_file=None if self.book.is_main else str(_ROOT / f"data/stop_registry{self.book.suffix()}.json"))`（核对 OMS.__init__ 的 registry_file 参数名与默认行为，None 必须回落到现行默认路径）；
   - last_scan 幂等（L909-922）与 summary 持久化：`self.book.silent` 时——`_read_last_scan` 返回 None 的语义会触发重复扫描，改为：**scan() 入口处影子账本直接跳过幂等检查**（编排层保证主账本当日已扫才跑影子），`_write_last_scan` 与 `_build_daily_summary` 的 last_scan 持久化段在 silent 时跳过。

4. **seed 覆盖**：`_universe` L365 `seed=getattr(u, "pool_seed", 42)` → `seed=self.book.pool_seed`（BookSpec 的 seed 由编排层从 ShadowBookConfig 传入，主账本 BookSpec(pool_seed=settings.universe.pool_seed) 保持一致）。

5. **AI 摘要跳过**：`_scan_impl` 中调用 ai_summary 的位置（第八轮集成点）加 `if not self.book.silent:`。

6. **日志前缀**：scan 相关关键日志（买入成交/止损执行/巡检完成）加 `[主]` / `[影子s43]` 前缀，便于三账本日志区分。

**验收**：`book=None` 时全部行为与现状一致（现有测试零改动通过）；构造 `BookSpec(name="s43", seed=43, silent=True)` 的 scanner：状态文件/registry/pending/report 目录均带 `_s43` 后缀、alerter 为 None。

## 第三步：编排层（setup_scheduler + run_scheduler.py）

`setup_scheduler`（L1171）改造：

```python
    scanner = setup_scanners(...)  # 保持兼容：返回第一个

    # 影子账本（第十二轮）：顺序执行，主先影子后（影子命中当日缓存，零额外 API）
    shadow_scanners = []
    for sb in getattr(settings, "shadow_books", []) or []:
        s2 = settings.model_copy(deep=True)
        s2.universe.pool_seed = sb.seed
        shadow_scanners.append(DailyScanner(
            s2, mode=mode,
            book=BookSpec(name=sb.name, pool_seed=sb.seed, silent=True),
        ))
```

cron job 的 scan 回调改为：

```python
    def _run_all_scans():
        for sc in [scanner, *shadow_scanners]:
            try:
                sc.scan(n=n)
            except Exception as e:
                logger.error(f"[{sc.book.name}] 巡检异常: {e}")
                if sc.book.is_main:
                    raise   # 主账本异常照旧上抛（心跳 fail ping / ERROR 邮件）
                # 影子异常只记日志，不影响主账本与其他影子
```

注意：`--run-now` 手动触发路径同样覆盖三账本；配置热生效（第十轮）读取 shadow_books 后**重建影子 scanner 列表**（或简单起见：容器重启生效，注释注明）。

**验收**：`--run-now` 一次跑三个账本，日志可分辨；主账本异常中断时影子也不再跑（顺序执行天然如此）；影子异常不影响主账本。

## 第四步：看板聚合（web/server.py + index.html）

1. `/api/dashboard`（L223）：新增 `review_shadows` 块——glob `data/paper_state_*.json`（排除主 `paper_state.json` 与备份 `.bak*`），对每个调 `review_stats(trades)`：

```python
        "review": _dashboard_review(state),          # 现有主账本块不动
        "review_shadows": _shadow_reviews(),          # 新增：{"s43": {...}, "s44": {...}}
```

   `_shadow_reviews()` 参照 `_dashboard_review` 写，读文件失败跳过该账本（看板永不因影子文件缺失报错）。
2. `index.html` 评审进度卡（renderDashboard 的 cards.push 处）：`sub` 行追加影子合计（存在时）：
   `主 13/100 · 影子 s43 8轮 · s44 7轮`（影子用小字号/次要色，明确"参考"地位）。
3. 顺带：`/api/state` 等主账本端点不动（看板持仓/交易表只展示主账本，避免混淆）。

**验收**：无影子文件时看板与现状一致；有影子文件时进度卡出现合计且无 JS 报错。

## 第五步：review_progress 顺带增强（可选，1 行）

`review_stats` 返回 dict 加 `"name"` 键（默认 "main"），看板聚合时透传——纯展示层。

## 第六步：测试（tests/test_shadow_books.py，≥7 用例）

1. `test_default_no_shadows`：无配置时 `setup_scheduler` 只有一个 scanner、行为与现状一致；
2. `test_bookspec_paths`：`BookSpec(name="s43").suffix() == "_s43"`、main 为空串；
3. `test_shadow_state_isolation`：两个 scanner（不同 book）各自 buy 后，`paper_state.json` 与 `paper_state_s43.json` 独立、互不影响；
4. `test_shadow_pool_seed`：mock 全市场数据下，seed 43/44 的池 != seed 42（至少部分不同）；缓存文件名含各自 seed；
5. `test_shadow_silent`：影子 scan 不写 last_scan.json、不构建邮件 alerter、不调 ai_summary（mock 断言未调用）；
6. `test_shadow_oms_registry_split`：主/影子的 stop_registry 落不同文件；
7. `test_dashboard_shadow_aggregation`：TestClient 下放两个影子 paper_state → `/api/dashboard` 的 review_shadows 键含各自轮次；无影子文件时不报错。
8. `test_shadow_scan_exception_isolated`：影子 scan 抛异常不影响主账本 scan 结果（编排层 try/except 断言）。

## 第七步：决策日志登记（docs/决策日志.md）

新增一行（参照既有格式）：
> | 2026-09-XX | 无（样本积累提速，不动策略/风控/参数） | **影子账本上线（s43/s44，分层池 seed 43/44）**：同进程顺序巡检（主先影子后，影子命中当日缓存）；影子状态文件 paper_state_s43/_s44.json 独立；无邮件/AI/心跳/last_scan。**统计纪律：评审进度与胜率仍以主账本为准（50/100 轮里程碑不变）；影子账本同期相关，仅作跨池稳健性对照，不并入评审样本数** | 不涉及数据消费 |

并在 Live 积累期表加一行同义记录。

## 交验标准（整轮）

- [ ] 全量测试通过（新增 ≥8，现有零改动）；
- [ ] `--run-now` 连跑三账本：日志三段可辨、三份状态文件独立更新、仅主账本写 last_scan/发日报；
- [ ] 首次运行耗时预期：影子池各 200 只新股 × 3 接口 ≈ 1200 请求 × 0.3s 限速 ≈ 6-8 分钟（一次性），此后每日增量趋近零额外 API；
- [ ] 看板评审进度卡显示主+影子合计；
- [ ] NAS 部署：代码同步 + `docker-compose up -d --build` 重建（涉及 Python 代码，必须重建镜像）；
- [ ] 决策日志两处登记完成。

## 风险与边界（Trae 注意）

1. `settings.model_copy(deep=True)` 后改 `pool_seed`——确认 pydantic v2 深拷贝语义（嵌套 model 可变），必要时两步 copy；
2. OMS `registry_file=None` 的回落行为先读 oms.py:48-64 确认，None 必须等价"现行默认路径"；
3. 影子 scanner 与主 scanner 共享 `self.client`（Tushare）与 `self.store`（DuckDB）实例吗？——**不共享**（各自 DailyScanner 自建），同进程顺序访问 DuckDB 无锁冲突（P2-4 单写多读已处理），但确认 DuckDBStore 连接同进程多实例可并存（duckdb 同进程多连接 OK）；
4. 配置热生效（第十轮）若在 scan 中重载 settings，注意不要用重载后的 shadow_books 增删 scanner（重启生效即可，代码注释注明）；
5. 若 alerter=None 的调用点存在非 None-safe 处，加守卫而不是造 NoopAlerter（保持最小改动）。

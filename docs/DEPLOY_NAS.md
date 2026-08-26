# NAS 部署指南（群晖 DS918+ / DSM 7）

> 第四轮清单 §10。目标：模拟盘 7×24 无人值守运行。
> **NAS 只跑纸面（paper）**；实盘必须回 Windows（见文末 QMT 迁移说明）。

---

## 一、部署步骤（DSM Container Manager）

### 1. 准备项目目录

在 NAS 上建共享文件夹（如 `/volume1/docker/lihuquantify`），把项目文件放进去。

**从 Windows 拷贝什么**：

| 内容 | 说明 |
|---|---|
| `src/` `web/` `config/` `scripts/` | 代码与配置 |
| `run_scheduler.py` `run_backtest.py` `pyproject.toml` `README.md` `Dockerfile` `docker-compose.yml` `.dockerignore` | 入口与构建文件 |
| **`data/`** | **命根子**：paper_state.json / stop_registry.json / duckdb / pending_stops.json |
| `outputs/` | 报告/图表（可只拷最近几个月） |
| `docs/决策日志.md` | 决策留痕 |
| `tushareMcp.json` | token 文件（**不进 git**，只复制到 NAS 本地） |

### 2. 导入 compose

1. DSM → **Container Manager** → **项目** → **新增**；
2. 项目名 `lihuquantify`，路径选上述目录，**使用现有 docker-compose.yml**；
3. （可选）在同目录建 `.env`：
   ```ini
   AUTH_CODE=你的邮箱授权码        # 可留空，邮件告警就不启用
   HEALTHCHECKS_URL=https://hc-ping.com/<uuid>
   ```
4. 点 **构建**（首次约 5-10 分钟拉镜像装依赖）→ **启动**。

### 3. 目录映射表（宿主机 ↔ 容器）

| 宿主机 | 容器 | 用途 |
|---|---|---|
| `./data` | `/app/data` | duckdb + paper_state + 日志（**两个容器共享，scheduler 写 web 读**） |
| `./outputs` | `/app/outputs` | 报告/图表 |
| `./config` | `/app/config` | settings.yaml（改后重启 scheduler 容器生效） |
| `./tushareMcp.json` | `/app/tushareMcp.json`（只读） | Tushare token |

时区：compose 已设 `TZ=Asia/Shanghai`（loguru 时间戳 / 月度复盘的 date.today() 都依赖它）。

### 4. 验收

- [ ] `docker exec lihu-scheduler date` 显示北京时间（CST）；
- [ ] 局域网浏览器打开 `http://<NAS_IP>:8000` 看板正常；
- [ ] **重启 NAS** → 容器自动恢复（restart: unless-stopped），总资产/持仓与重启前一致（paper_state.json 持久化）；
- [ ] 16:30 巡检后 `outputs/reports/` 出现当日报告。

---

## 二、日常操作

### 手动跑一次巡检（补跑/验证）

```bash
docker exec lihu-scheduler python run_scheduler.py --run-now --force
```

- 幂等保护：当日已巡检会自动跳过；`--force` 强制重跑。
- 日常调试也可进入容器：`docker exec -it lihu-scheduler bash`。

### 看容器日志

```bash
docker logs -f lihu-scheduler          # 运行日志（同时落盘 data/logs/scheduler_YYYYMMDD.log，保留 30 天）
docker logs -f lihu-web
```

### 从备份恢复（恢复演练）

```bash
# 在项目目录（NAS SSH 或 Container Manager 终端）
python scripts/backup_data.py --restore backups/backup_XXXXXX.zip
```

恢复前会自动把当前状态再存档一份，防误操作。

---

## 三、告警与心跳配置

### 邮件告警（第四轮清单1）

`config/settings.yaml`：

```yaml
alert:
  email:
    enabled: true
    smtp_host: "smtp.qq.com"
    smtp_port: 465
    username: "xxx@qq.com"
    auth_code: ""          # 授权码放 .env（AUTH_CODE），不写进 yaml
    to: ["xxx@qq.com"]
    send_daily_digest: true
```

事件类告警（Checklist 拦截 / 止损 / 连亏停手 / API 异常 / 巡检异常）即时发邮件；
每日巡检完成后发摘要邮件（信号/成交/拦截/总资产/报告路径）。

### 缺席心跳（第四轮清单2）

1. 注册 [healthchecks.io](https://healthchecks.io)（免费），新建 Check，Ping URL 形如 `https://hc-ping.com/<uuid>`；
2. URL 填到 `.env` 的 `HEALTHCHECKS_URL`（或 settings.yaml `heartbeat.healthchecks_url`）；
3. healthchecks 后台设 **Period=1 天，Grace=1 小时（即每日 17:30 前未收到成功 ping → 邮件告警）**；
4. 验收：停掉 scheduler 容器一天，应收到"巡检未完成"告警。

---

## 四、备份策略（第四轮清单8）

任选其一（推荐 1+2 双保险）：

1. **Hyper Backup**（NAS 原生）：每日备份整个项目目录（`data/` + `outputs/` + `docs/决策日志.md`），版本去重；
2. **脚本兜底**：DSM 任务计划每日 17:00 运行
   ```bash
   python scripts/backup_data.py --keep 30
   ```
   输出 `backups/backup_YYYYMMDD_HHMMSS.zip`，滚动保留 30 份。

**恢复演练（每季度一次）**：备份 → 删除 `data/paper_state.json` → 恢复 → 看板权益/持仓与删除前一致。

---

## 五、安全边界（第四轮清单9）

1. **看板绝不暴露公网**：`ports: "8000:8000"` 默认绑定宿主机所有网卡（仅局域网可达）。
   - 需收窄：改 `"192.168.x.x:8000:8000"`（绑定内网 IP）；
   - 远程访问：Tailscale / ZeroTier 组网，**不做路由器端口映射**。
2. `tushareMcp.json`（token）、`.env`（授权码）：不进 git（.gitignore 已含）、不进 Docker 镜像（.dockerignore 已含）；NAS 目录权限收窄到管理员。
3. 看板是只读聚合器，无交易接口。

---

## 六、故障排查

| 症状 | 排查 |
|---|---|
| 数据没更新（报告里最新交易日是昨天） | ① token/积分/限频：`docker logs lihu-scheduler` 找接口异常告警；② Tushare 当日日线 16:00 后才完整——巡检已改 16:30，若 16:30-16:40 间仍不齐属上游延迟，次日自动补 |
| 报告没生成 | `data/logs/scheduler_*.log` 查异常栈；健康链路：心跳 fail ping + 巡检异常邮件 |
| 看板空白 / 数据不刷新 | ① `docker logs lihu-web`；② 检查 web 容器是否挂了 `./data`、`./outputs` 卷；③ DuckDB 被 scheduler 写入时 web 读锁冲突——web 已有 JSON 回退（filter_stats.json），等整点后自动恢复 |
| 重复巡检 | 幂等保护：当日已巡检自动跳过；真要重跑用 `--run-now --force` |
| 容器反复重启 | `docker logs lihu-scheduler` 看启动异常；常见：settings.yaml 挂载路径错、token 文件没挂 |

---

## 七、QMT 迁移说明（实盘必须回 Windows）

**铁律**：国金 QMT 客户端 + xtquant 仅支持 Windows。NAS 只跑纸面验证；
切换实盘时**策略代码零改动**，只迁移状态与配置。

迁移步骤：

1. **停 NAS 容器**：Container Manager → 项目 → 停止（防状态分叉）；
2. **拷贝回 Windows**：`data/`（paper_state / stop_registry / duckdb）、`outputs/`；
3. **Windows 上装依赖**：`pip install -e ".[web]"`（或继续用 Docker Desktop 跑同一 compose）；
4. **开 QMT**：本机安装国金 QMT 客户端并登录；`config/settings.yaml` 设 `qmt.enabled: true`、`qmt.path` 指向 `bin.x64`；
5. **灰度小资金实盘**：`python run_scheduler.py --mode live`；
6. 三口径纪律不变：回测/模拟盘=次日开盘，实盘=盘中条件单（OMS）。

> 实盘迁移本身也应记入 docs/决策日志.md。

---

以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。

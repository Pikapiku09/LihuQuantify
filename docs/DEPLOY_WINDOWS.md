# Windows 部署指南（开机自启，不上 NAS 时的备用方案）

> 第四轮清单 §11。适用：短期内不上 NAS，用一台常开的 Windows 电脑跑模拟盘。
> 若已上 NAS，本文档作灾备备用。

---

## 一、开机自启（任务计划程序）

### 1. 创建任务

`Win+R` → `taskschd.msc` → **创建任务**（不用"基本任务"，选项更全）：

| 选项卡 | 设置 |
|---|---|
| 常规 | 名称 `LihuQuantify Scheduler`；**"不管用户是否登录都要运行"**；"使用最高权限运行"；配置 Windows 10 |
| 触发器 | 新建 → **启动时**（"开始任务"选"启动时"） |
| 操作 | 新建 → 程序 `python`，参数 `run_scheduler.py --mode paper --n 50`，起始于 `E:\LihuQuantify` |
| 条件 | 取消勾选"只有在计算机使用交流电源时才启动此任务"（笔记本部署时注意供电） |
| 设置 | 勾选"如果任务失败，按以下频率重新启动"（每 5 分钟，共 3 次） |

> 建议 `python` 用绝对路径（`where python` 查），避免 PATH 环境差异。

### 2. 看板（可选）

再建一个任务同样开机自启：
`python -m web.server`，起始于 `E:\LihuQuantify`（默认绑 127.0.0.1，仅本机访问）。
局域网访问时用 `set LIHU_WEB_HOST=0.0.0.0` 后启动（注意防火墙放行 8000 端口、仅限内网）。

### 3. 验收

重启电脑 → **不登录** → 稍等进入任务计划程序查看任务状态"正在运行"；
`data/logs/scheduler_YYYYMMDD.log` 有启动记录。

---

## 二、日常操作

```powershell
# 手动跑一次（当日已巡检会幂等跳过）
python run_scheduler.py --run-now

# 强制重跑当日
python run_scheduler.py --run-now --force

# 看日志（30 天滚动）
Get-Content data\logs\scheduler_$(Get-Date -Format yyyyMMdd).log -Tail 50 -Wait

# 备份（建议任务计划每日 17:00 跑）
python scripts/backup_data.py --keep 30
```

---

## 三、告警配置（同 NAS）

1. 邮件：`config/settings.yaml` 填 `alert.email` 段（授权码放 `.env`：`LIHU_ALERT__EMAIL__AUTH_CODE=...`）；
2. 心跳：healthchecks.io URL 填 `heartbeat.healthchecks_url`（或 `.env` 的 `LIHU_HEARTBEAT__HEALTHCHECKS_URL`），
   后台设"每日 17:30 前未收到成功 ping → 告警"——电脑关机/断电也会收到缺席通知。

---

## 四、安全边界

1. 看板默认绑 `127.0.0.1`（仅本机）；要局域网访问才改 `LIHU_WEB_HOST`，**绝不路由器端口映射**；
2. `tushareMcp.json` / `.env` 已在 `.gitignore`，勿提交到任何远端仓库；
3. 电脑休眠 = 巡检停摆：电源计划设为"从不休眠"（显示可关）。

---

以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。

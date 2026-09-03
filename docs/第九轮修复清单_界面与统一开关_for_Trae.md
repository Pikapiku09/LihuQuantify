# 第九轮修复清单：界面问题与统一开关（for Trae）

> 项目：LihuQuantify
> 背景：第八轮部署后用户反馈 4 个界面/运维问题。均为**展示层与部署层**，不涉及策略/风控/交易逻辑，冻结期安全。
> 硬约束：不改任何信号/下单/止损逻辑；只动 web 展示、数据快照与部署脚本。

---

## 问题 1：AI 收盘总结在 Web 界面看不到

**现状**：第八轮只把 `ai_summary` 写进了 `.md` 报告和 HTML 邮件，**Web 看板没有任何 AI 总结展示**（`index.html` / `/api/dashboard` 均未接入），所以用户在界面上看不到 Mimo 总结。

**改法**：
1. `web/server.py` `/api/dashboard` 增加 `"ai_summary": (last_scan.summary or {}).get("ai_summary")`；
2. `index.html` 首页新增 **"AI 收盘总结"卡片**（放在指标卡下方），内容为纯文本渲染（`textContent`，不套 markdown 解析），含交易日期标注；`ai_summary` 为空时显示"暂无（未配置 key 或生成失败）"；
3. 报告页确认 `.md` 里的"七、AI 收盘总结"节能被 `mdToHtml` 正常渲染（若已被 report.py 写入）。

**验收**：16:30 巡检后刷新首页能看到 Mimo 总结段；`.env` 未配 key 时显示"暂无"，不报错。

---

## 问题 2：热力图部分乱码

**现状与排查结论**：热力图数据链路 = web 读 `data/cache/daily_*.json`（行情）+ **DuckDB `stock_basic`**（名称/行业映射）。乱码的可能来源：
1. **DuckDB 写锁**：scheduler 常驻持有写锁 → web 的只读连接失败 → 映射缺失，显示异常（降级路径）；
2. **数据文件编码不一致**：缓存/映射文件写入与读取编码需统一（UTF-8 + `ensure_ascii=False`）；
3. **`web/static/vendor/echarts.min.js` 可能损坏/编码问题**（1MB 文件，需核对完整性）；
4. 前端 treemap 的 `rich` 标签 formatter 中 `{n|...}` 若 name 含 `{ } |` 等特殊字符会破坏富文本渲染。

**改法**：
1. 彻底切断 web 对 DuckDB 的依赖（见问题 3 的快照方案），名称/行业全部来自 scheduler 写的 UTF-8 JSON 快照——编码链路单一可控；
2. 重新从官方源下载 echarts 5.x `echarts.min.js` 替换 vendor 版本（核对体积约 1MB，若当前文件异常则替换）；
3. 前端 formatter 对 name 做 `{}`/`|` 字符转义后再拼 rich 标签。

**验收**：热力图所有标签正常显示中文，无 □□/乱码；刷新多次稳定。

---

## 问题 3：热力图只显示股票代码、不显示名称/板块/行业

**根因（已定位）**：`web/server.py` `_heatmap_rows()` 第 483-497 行，名称/行业映射读 **DuckDB `stock_basic`**；而 **scheduler 进程常驻持有 DuckDB 写锁** → web 的只读连接必然失败 → `except` 静默降级 → `industry_map` 为空 → 名称回退为代码、行业显示"未知"。**这是锁库导致的降级，不是功能没实现。**

**改法（核心修复）**：
1. scheduler 巡检时新增写入 `data/heatmap_snapshot.json`（UTF-8、`ensure_ascii=False`），内容 = 股票池最新交易日行情快照：`[{ts_code, name, industry, close, pct_chg, amount, trade_date}]`（name/industry 来自 `_universe` 的 name_map + sector_map，**scheduler 侧数据齐全，无需读库**）；
2. `web/server.py` `_heatmap_rows()` 改为只读该 JSON（读失败/不存在 → 503 提示"等待巡检生成"），删除 DuckDB 依赖段；
3. 行业分组、涨跌幅/成交额分档、`/api/heatmap/detail` 弹窗全部改用该 JSON 数据。

**验收**：**scheduler 运行期间**（DuckDB 被锁）热力图仍显示每只票的名称与行业；点击行业块下钻、点击个股弹详情均正常。

---

## 问题 4：统一开关（桌面 .url 打开 web 时程序自动启动）

**现状**：web/scheduler 是 NAS 上的 Docker 容器（`restart: unless-stopped`）；`.url` 只是浏览器入口，无启动能力。程序是否运行取决于 NAS 的 Docker 套件是否自启。

**改法（三档，按需）**：

1. **NAS 自启配置（必做，一次性，非代码）**：
   - DSM → Container Manager → 设置 → 启用"开机自动启动"；
   - 确认两个容器 `restart: unless-stopped`；
   - 效果：NAS 一开机，scheduler + web 自动运行，`.url` 永远可用——**这是"统一开关"的最优解**。
2. **Windows 一键脚本（可选，代码=两个 .bat）**：
   - `start_lihu.bat`：
     ```bat
     @echo off
     ssh root@NANDH "cd /volume2/Lihu_Quantify && docker-compose up -d" 
     start http://192.168.123.203:8000
     ```
   - `stop_lihu.bat`：
     ```bat
     @echo off
     ssh root@NANDH "cd /volume2/Lihu_Quantify && docker-compose stop"
     ```
   - 配合 SSH 免密（Windows `ssh-keygen` 生成密钥，公钥追加到 NAS `~/.ssh/authorized_keys`），双击即可，不再输入密码；
   - 把 `start_lihu.bat` 放桌面替代 `.url`。
3. **健康检查（可选增强）**：web 增加 `GET /api/health`（返回 `{"ok": true}`），`start_lihu.bat` 先 `curl -s http://192.168.123.203:8000/api/health`，通了才开浏览器，否则提示"NAS 或容器未就绪"。

**验收**：NAS 重启后双击 `.url`/`start_lihu.bat` 直接可用；stop 脚本能停容器；不依赖手动 SSH。

---

## 实施顺序建议

**3（快照数据源，同时解决 2 的乱码根源）→ 1（AI 总结卡片）→ 2 余项（echarts 替换/转义）→ 4（自启配置 + 脚本）**

## 回归基线（不可破坏）

- 策略/风控/交易逻辑冻结；100 笔纸面样本期间不改参数（capital_guard 保持 false）；
- AI 总结保持"纯展示、不参与决策"边界；
- 看板持仓/指标/交易等第六轮功能不回归。

---
**免责声明：以上内容仅供参考，不构成任何投资建议。投资有风险，入市需谨慎。**

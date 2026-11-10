"""一键部署 LihuQuantify 到 NAS：预检 → 备份 → 同步 → 重建镜像 → 验证 → 补跑巡检。

设计原则：
  - 每步可回退：同步前把 NAS 的 src/ config/ 打成带时间戳的 tar 包放 REMOTE/backups/
  - 只覆盖代码与配置，**绝不触碰 data/ 与 outputs/**（NAS 侧生产状态）
  - 验证不靠"命令没报错"，而是进镜像内探测新逻辑是否真的存在
  - 口令只从环境变量或 .env 读取，不出现于命令行

凭据（二选一）：
  环境变量 NAS_PWD，或项目根 .env 写一行  NAS_PWD=你的NAS口令
  （.env 已在 .gitignore 且未被 git 跟踪）

用法：
  python scripts/deploy_to_nas.py --dry-run     # 只预检，不改动 NAS
  python scripts/deploy_to_nas.py --yes         # 全流程（含重建 + 补跑巡检）
  python scripts/deploy_to_nas.py --yes --skip-scan   # 只部署，不补跑巡检
  python scripts/deploy_to_nas.py --yes --rollback backups/deploy_20261110_193000.tgz
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _nas_sftp_sync as sync  # noqa: E402  (复用打包逻辑：DIRS/FILES/EXCLUDE 规则一致)

ROOT = Path(__file__).resolve().parents[1]
REMOTE = sync.REMOTE
HOST, USER, PORT = sync.HOST, sync.USER, sync.PORT
CHUNK = sync.CHUNK

CONTAINER_SCHEDULER = "lihu-scheduler"


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def connect() -> paramiko.SSHClient:
    pwd = sync._nas_pwd()
    if not pwd:
        raise SystemExit("NAS_PWD 未设置：请设置环境变量，或在项目根 .env 写 NAS_PWD=...")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=pwd, port=PORT, timeout=15)
    return c


def run(ssh, cmd: str, *, sudo: bool = False, timeout: int = 900) -> tuple[int, str, str]:
    """执行远端命令。sudo=True 时用 sudo -S 并把口令喂给 stdin（不进命令行）。"""
    pwd = sync._nas_pwd()
    if sudo:
        full = "sudo -S sh -c " + sh_quote(cmd)
    else:
        full = cmd
    stdin, stdout, stderr = ssh.exec_command(full, timeout=timeout)
    if sudo:
        stdin.write(pwd + "\n")
        stdin.flush()
    rc = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return rc, out, err


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 预检
def precheck(ssh) -> bool:
    log("1/7 预检")
    rc, out, err = run(ssh, f"ls -d {REMOTE} && ls {REMOTE} | head -20")
    if rc != 0:
        log(f"  ✗ 目标目录不可达: {REMOTE}  {err.strip()}")
        return False
    log(f"  目标目录: {REMOTE}")
    for line in out.strip().splitlines()[1:]:
        log(f"    {line}")

    rc, out, _ = run(ssh, "docker version --format '{{.Server.Version}}'", sudo=True)
    log(f"  docker: {out.strip().splitlines()[0] if out.strip() else '未知'}")

    rc, out, _ = run(ssh, "docker ps --format '{{.Names}}|{{.Status}}' | head -10", sudo=True)
    log("  运行中容器:")
    for line in out.strip().splitlines() or ["(无)"]:
        log(f"    {line}")

    rc, out, _ = run(ssh, "docker-compose version 2>&1 | head -1", sudo=True)
    log(f"  compose: {out.strip().splitlines()[0] if out.strip() else '未知'}")

    # NAS 侧 settings 关键字段（防覆盖特有配置）
    rc, out, _ = run(ssh, f"grep -E '^(main_strategy|init_capital)|^  (market_filter|market_filter_mode|pool_mode|pool_size):' "
                          f"{REMOTE}/config/settings.yaml 2>&1")
    log("  NAS 现有 settings 关键项:")
    for line in out.strip().splitlines() or ["(读取失败)"]:
        log(f"    {line}")

    rc, out, _ = run(ssh, f"grep -n 'latest_signal' {REMOTE}/src/lihu_quantify/strategy/base.py 2>&1 | head -3")
    log("  NAS 现有 base.py（部署前旧版）:")
    for line in out.strip().splitlines() or ["(无)"]:
        log(f"    {line}")
    return True


# ---------------------------------------------------------------- 备份
def backup(ssh) -> str | None:
    log("2/7 备份 NAS 现有代码与配置")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    remote_tgz = f"{REMOTE}/backups/deploy_{stamp}.tgz"
    cmd = (f"mkdir -p {REMOTE}/backups && tar czf {remote_tgz} -C {REMOTE} src config "
           f"&& ls -lh {remote_tgz}")
    rc, out, err = run(ssh, cmd, sudo=True)
    if "deploy_" not in out:
        log(f"  ✗ 备份失败: {(err or out).strip()[:300]}")
        return None
    log(f"  备份成功: {remote_tgz}")
    log(f"  {out.strip().splitlines()[-1]}")
    return remote_tgz


# ---------------------------------------------------------------- 同步
def push(ssh) -> bool:
    log("3/7 打包并上传代码")
    tgz = sync.build_tgz()
    b64 = base64.b64encode(tgz).decode()
    log(f"  b64 长度 {len(b64)}，分 {(len(b64) + CHUNK - 1) // CHUNK} 块")

    run(ssh, "rm -f /tmp/_sync.b64 /tmp/_sync.tgz")
    for i in range(0, len(b64), CHUNK):
        chunk = b64[i:i + CHUNK]
        rc, _, err = run(ssh, f"printf '%s' '{chunk}' >> /tmp/_sync.b64")
        if rc != 0:
            log(f"  ✗ 上传失败 @{i}: {err.strip()[:200]}")
            return False

    cmd = (f"base64 -d /tmp/_sync.b64 > /tmp/_sync.tgz && "
           f"tar xzf /tmp/_sync.tgz -C {REMOTE} && rm -f /tmp/_sync.b64 /tmp/_sync.tgz && echo EXTRACT_OK")
    rc, out, err = run(ssh, cmd, sudo=True)
    if "EXTRACT_OK" not in out:
        log(f"  ✗ 解包失败: {(err or out).strip()[:300]}")
        return False
    log("  解包完成")

    # token_file 容器路径修正（本地是 Windows 路径，直接覆盖会让容器读不到 token）
    cmd = (f"sed -i 's|token_file: .*|token_file: /app/tushareMcp.json|' {REMOTE}/config/settings.yaml && "
           f"grep -E 'token_file:' {REMOTE}/config/settings.yaml")
    rc, out, _ = run(ssh, cmd, sudo=True)
    if "/app/tushareMcp.json" not in out:
        log(f"  ✗ token_file 修正失败: {out.strip()[:200]}")
        return False
    log(f"  token_file → /app/tushareMcp.json ✓")
    return True


# ---------------------------------------------------------------- 重建
def rebuild(ssh) -> bool:
    log("4/7 重建镜像（src 是 COPY 进镜像的，只重启无效）")
    # DSM 自带 docker-compose 1.28（无 `docker compose` 子命令），两者择可用者
    rc, out, _ = run(ssh, "docker-compose version >/dev/null 2>&1 && echo OK_HYPHEN || echo NO_HYPHEN",
                     sudo=True)
    compose = "docker-compose" if "OK_HYPHEN" in out else "docker compose"
    log(f"  compose 命令: {compose}")
    cmd = f"cd {REMOTE} && {compose} up -d --build 2>&1 | tail -40"
    rc, out, err = run(ssh, cmd, sudo=True, timeout=2400)
    tail = (out or err).strip().splitlines()[-8:]
    for line in tail:
        log(f"    {line}")
    if rc != 0:
        log(f"  ✗ 重建失败 rc={rc}")
        return False
    log("  重建完成")
    return True


def current_image_id(ssh) -> str:
    """记录部署前 lihu-quantify:latest 的镜像 ID（重建后用于回滚 retag）。"""
    rc, out, _ = run(ssh, "docker images lihu-quantify:latest --format '{{.ID}}'", sudo=True)
    lines = [l.strip() for l in out.strip().splitlines() if l.strip() and "Password" not in l]
    return lines[0] if lines else ""


def wait_healthy(ssh, timeout: int = 180) -> bool:
    """轮询容器健康状态。

    2026-11-10 事故教训：重建后容器立即处于 restarting 时，直接跑 docker exec 探测
    会得到空输出（误判为"验证失败"而非"容器崩了"）。改为先等健康，并把
    Restarting 视为硬失败，交由调用方自动回滚。
    """
    log("  等待容器就绪（最多 %ds）…" % timeout)
    deadline = time.time() + timeout
    need = (CONTAINER_SCHEDULER, "lihu-web")
    last = ""
    while time.time() < deadline:
        rc, out, _ = run(ssh, "docker ps --format '{{.Names}}|{{.Status}}'", sudo=True)
        last = out.replace("Password: ", "").strip()
        names = {}
        for ln in last.splitlines():
            if "|" in ln:
                k, v = ln.split("|", 1)
                names[k.strip()] = v.strip()
        if any(n in names and "Restarting" in names[n] for n in need):
            log(f"  ✗ 检测到重启循环: {last}")
            return False
        if all(n in names and "healthy" in names[n] for n in need):
            log("  容器已健康")
            return True
        time.sleep(6)
    log(f"  ✗ 健康等待超时: {last}")
    return False


# ---------------------------------------------------------------- 验证
def verify(ssh) -> bool:
    log("5/7 验证新逻辑是否真的进了镜像")
    probes = [
        ("依赖可 import（duckdb 等原生库）",
         "import duckdb,numpy,pandas,scipy;print('OK')"),
        ("latest_signal 只认当日",
         # 注意：不可用「源码不含 signals[-1]」做判据——新实现的 docstring 会描述旧行为，
         # 照样含该字符串，会假阴性（2026-11-10 第二次部署即因此误回滚）。
         # 改判唯一属于新实现的那行调用。
         "import inspect,lihu_quantify.strategy.base as b;"
         "s=inspect.getsource(b.StrategyBase.latest_signal);"
         "print('OK' if '_same_date(s.trade_date, last_date)' in s else 'OLD')"),
        ("因子方向已翻正",
         "import inspect,lihu_quantify.strategy.intraday_reversal as m;"
         "s=inspect.getsource(m.IntradayReversal._evaluate);"
         "print('OK' if '1.0 - self.entry_pct' in s else 'OLD')"),
        ("闸门含趋势反转确认",
         "import inspect,lihu_quantify.market as m;"
         "s=inspect.getsource(m.classify_market_state);"
         "print('OK' if 'ma5_above' in s else 'OLD')"),
        ("策略构造已收敛",
         "import inspect,lihu_quantify.monitor.scheduler as s2;"
         "print('OK' if hasattr(s2.DailyScanner,'_build_strategy') else 'OLD')"),
    ]
    ok = True
    for desc, code in probes:
        rc, out, err = run(ssh, f"docker exec {CONTAINER_SCHEDULER} python -c {sh_quote(code)} 2>&1", sudo=True)
        res = out.strip().splitlines()[-1] if out.strip() else "?"
        flag = "✓" if res == "OK" else "✗"
        if res != "OK":
            ok = False
        log(f"  {flag} {desc}: {res}")
    return ok


# ---------------------------------------------------------------- 补跑
def force_scan(ssh) -> bool:
    """补跑当日巡检。

    两个已知坑（2026-11-10 实测）：
      1) DuckDB 单写锁：常驻调度进程持有 lihu_quant.duckdb 写锁，直接 docker exec
         起第二个进程必然 IOException（Conflicting lock）。必须先停常驻容器。
      2) run_scheduler.py --run-now 跑完不会退出，而是进入 setup_scheduler 的等待态，
         导致 docker-compose run 挂住。故用 timeout 兜底，跑完由 timeout 收掉。
    市场已收盘时执行；finally 语义保证一定恢复常驻调度。
    """
    log("6/7 补跑当日巡检（停常驻 → 单次 --run-now --force → 恢复常驻）")
    try:
        rc, out, err = run(ssh, f"cd {REMOTE} && docker-compose stop scheduler 2>&1 | tail -3",
                           sudo=True, timeout=300)
        log(f"  停常驻: {(out or err).strip().splitlines()[-1] if (out or err).strip() else ''}")

        cmd = (f"cd {REMOTE} && docker-compose run --rm --no-deps -T scheduler "
               f"timeout 420 python run_scheduler.py --run-now --force 2>&1 | tail -40")
        rc, out, err = run(ssh, cmd, sudo=True, timeout=1200)
        text = out or err
        for line in text.strip().splitlines()[-8:]:
            log(f"    {line}")
        for kw in ("扫描信号", "当日有效", "过期信号丢弃", "市场状态", "巡检完成", "ERROR", "Error"):
            for line in text.splitlines():
                if kw in line:
                    log(f"  ★ {line.strip()[:220]}")
        return True
    finally:
        log("  恢复常驻调度")
        rc, out, err = run(ssh, f"cd {REMOTE} && docker-compose up -d scheduler 2>&1 | tail -3",
                           sudo=True, timeout=600)
        log(f"    {(out or err).strip().splitlines()[-1] if (out or err).strip() else ''}")
        wait_healthy(ssh)
        # 清理可能残留的 run 容器（timeout 正常情况下已退出）
        rc, out, _ = run(ssh, "docker ps -a --format '{{.Names}}|{{.Status}}' | grep -i '_run_' || true",
                         sudo=True)
        for ln in out.replace("Password: ", "").strip().splitlines():
            if ln.strip():
                name = ln.split("|")[0].strip()
                log(f"    清理残留容器 {name}")
                run(ssh, f"docker rm -f {name}", sudo=True)


def show_logs(ssh) -> None:
    log("7/7 近期日志（关键词）")
    cmd = (f"docker logs --tail 300 {CONTAINER_SCHEDULER} 2>&1 | "
           f"grep -E '扫描信号|当日有效|丢弃|市场状态|异常' | tail -15")
    rc, out, _ = run(ssh, cmd, sudo=True)
    for line in out.strip().splitlines() or ["(无匹配)"]:
        log(f"    {line}")


# ---------------------------------------------------------------- 回滚
def rollback(ssh, tgz_path: str | None = None, image_id: str | None = None) -> bool:
    """回滚：可选恢复文件（tgz）+ 可选 retag 回旧镜像，然后重启并等健康。"""
    log("回滚开始")
    if tgz_path:
        log(f"  恢复文件：{tgz_path}")
        rc, out, err = run(ssh, f"tar xzf {tgz_path} -C {REMOTE} && echo RESTORE_OK", sudo=True)
        if "RESTORE_OK" not in out:
            log(f"  ✗ 文件恢复失败: {(err or out).strip()[:300]}")
            return False
        log("  文件已恢复")
    if image_id:
        log(f"  镜像 retag：{image_id} → lihu-quantify:latest")
        rc, out, err = run(ssh, f"docker tag {image_id} lihu-quantify:latest && echo TAG_OK", sudo=True)
        if "TAG_OK" not in out:
            log(f"  ✗ retag 失败: {(err or out).strip()[:300]}")
            return False
    log("  重启容器（--no-build）")
    rc, out, err = run(ssh, f"cd {REMOTE} && docker-compose up -d --no-build 2>&1 | tail -8",
                       sudo=True, timeout=900)
    log(f"    {(out or err).strip().splitlines()[-1] if (out or err).strip() else ''}")
    return wait_healthy(ssh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只预检")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    ap.add_argument("--skip-rebuild", action="store_true", help="不同步/不重建（仅预检）")
    ap.add_argument("--skip-scan", action="store_true", help="部署后不补跑巡检")
    ap.add_argument("--rollback", metavar="TGZ", help="用指定备份包回滚（文件 + 镜像）")
    ap.add_argument("--rollback-image", metavar="ID", help="回滚时一并 retag 的旧镜像 ID")
    ap.add_argument("--no-auto-rollback", action="store_true",
                    help="失败时不自动回滚（默认自动回滚以尽快恢复服务）")
    args = ap.parse_args()

    ssh = connect()
    try:
        if args.rollback or args.rollback_image:
            ok = rollback(ssh, args.rollback, args.rollback_image)
            log("回滚完成" if ok else "✗ 回滚后容器仍未健康，请人工介入")
            raise SystemExit(0 if ok else 9)

        if not precheck(ssh):
            raise SystemExit(1)
        if args.dry_run:
            log("预检完成（--dry-run，未改动 NAS）")
            return
        if not args.yes:
            ans = input("\n确认执行备份+同步+重建？输入 YES 继续: ").strip()
            if ans != "YES":
                log("已取消")
                return

        prev_img = current_image_id(ssh)
        log(f"  部署前镜像 ID: {prev_img or '(未知)'}")

        bak = backup(ssh)
        if not bak:
            log("✗ 备份失败，中止部署（不冒险覆盖生产代码）")
            raise SystemExit(2)

        if args.skip_rebuild:
            log("已跳过同步/重建")
            return

        if not push(ssh):
            log(f"✗ 同步失败，可回滚：--rollback {bak} --rollback-image {prev_img}")
            raise SystemExit(3)

        if not rebuild(ssh):
            log("✗ 重建失败，自动回滚…")
            if not args.no_auto_rollback:
                rollback(ssh, bak, prev_img)
            raise SystemExit(4)

        # 2026-11-10 事故教训：重建后必须先等容器健康，否则 docker exec 探测拿不到输出
        if not wait_healthy(ssh):
            log("✗ 容器未健康（疑似崩溃循环），自动回滚到部署前镜像…")
            if not args.no_auto_rollback:
                ok = rollback(ssh, bak, prev_img)
                log("  自动回滚" + ("成功，服务已恢复" if ok else "失败，需人工介入"))
            raise SystemExit(4)

        if not verify(ssh):
            log("✗ 验证未通过")
            if not args.no_auto_rollback:
                log("  自动回滚…")
                ok = rollback(ssh, bak, prev_img)
                log("  自动回滚" + ("成功" if ok else "失败，需人工介入"))
            raise SystemExit(5)

        if not args.skip_scan:
            force_scan(ssh)
        show_logs(ssh)
        log(f"部署完成。回滚备份：{bak}｜回滚镜像：{prev_img}")
    finally:
        ssh.close()


if __name__ == "__main__":
    main()

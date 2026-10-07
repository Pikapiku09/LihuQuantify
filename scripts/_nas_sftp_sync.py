"""LihuQuantify → NAS 同步（SSH tar+base64 版，2026-10-07）。

背景：SMB 凭据失效、Synology SFTP 子系统禁用 → 走纯 SSH exec：
  本地 Python tarfile 打包（MIR 范围）→ base64 分块 exec 传输 → 远端解包覆盖。
范围与 sync_to_nas.ps1 一致：目录 src/config/scripts/web（排除 pyc/cache/node_modules）
+ 单文件清单 + token_file 容器路径修正。铁律：绝不推 data/ outputs/。
"""
from __future__ import annotations

import base64
import io
import os
import sys
import tarfile
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
HOST, USER, PORT = "192.168.123.203", "931570981", 22
REMOTE = "/volume2/Lihu_Quantify"
DIRS = ["src", "config", "scripts", "web"]
FILES = ["run_scheduler.py", "run_backtest.py", "run_full_backtest.py", "run_live.py",
         "pyproject.toml", "README.md", "Dockerfile", "docker-compose.yml",
         ".dockerignore", "docs/决策日志.md"]
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "node_modules"}
EXCLUDE_EXT = {".pyc", ".log"}
CHUNK = 48 * 1024


def build_tgz() -> bytes:
    buf = io.BytesIO()
    n = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for d in DIRS:
            base = ROOT / d
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [x for x in dirnames if x not in EXCLUDE_DIRS]
                for fn in filenames:
                    if Path(fn).suffix in EXCLUDE_EXT:
                        continue
                    lp = Path(dirpath) / fn
                    arc = str(lp.relative_to(ROOT)).replace(os.sep, "/")
                    tf.add(str(lp), arcname=arc)
                    n += 1
        for f in FILES:
            lp = ROOT / f
            if lp.exists():
                tf.add(str(lp), arcname=f.replace(os.sep, "/"))
                n += 1
    print(f"打包 {n} 个文件 → {len(buf.getvalue())} 字节")
    return buf.getvalue()


def main() -> None:
    pwd = os.environ.get("NAS_PWD")
    if not pwd:
        print("NAS_PWD 必填", file=sys.stderr)
        sys.exit(1)
    tgz = build_tgz()
    b64 = base64.b64encode(tgz).decode()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pwd, port=PORT, timeout=15)

    def run(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
        _, so, se = ssh.exec_command(cmd, timeout=timeout)
        out = so.read().decode(errors="replace")
        err = se.read().decode(errors="replace")
        return so.channel.recv_exit_status(), out, err

    # 1) 清理 + 分块上传 b64
    run("rm -f /tmp/_sync.b64 /tmp/_sync.tgz")
    for i in range(0, len(b64), CHUNK):
        chunk = b64[i:i + CHUNK]
        st, _, err = run(f"printf '%s' '{chunk}' >> /tmp/_sync.b64")
        if st != 0:
            print(f"chunk 上传失败@{i}: {err}", file=sys.stderr)
            sys.exit(1)
    print(f"b64 上传完成（{len(b64)} 字符，{ (len(b64)+CHUNK-1)//CHUNK } 块）")

    # 2) 远端解包覆盖（不解压 data/outputs——包里没有）
    st, out, err = run(
        f"base64 -d /tmp/_sync.b64 > /tmp/_sync.tgz && "
        f"tar xzf /tmp/_sync.tgz -C {REMOTE} && rm -f /tmp/_sync.b64 /tmp/_sync.tgz && echo EXTRACT_OK")
    print("解包:", out.strip() or err.strip())
    if "EXTRACT_OK" not in out:
        print("解包失败", file=sys.stderr)
        sys.exit(1)

    # 3) token_file 容器路径修正
    st, out, _ = run(f"sed -i 's|token_file: .*|token_file: /app/tushareMcp.json|' {REMOTE}/config/settings.yaml && grep -m1 token_file {REMOTE}/config/settings.yaml")
    print("token_file:", out.strip())

    # 4) 校验关键内容到位
    for probe, desc in [
        (f"grep -c main_strategy {REMOTE}/config/settings.yaml", "settings.yaml main_strategy"),
        (f"grep -c wbr25 {REMOTE}/config/settings.yaml", "settings.yaml wbr25"),
        (f"grep -c WeeklyBandReversal {REMOTE}/src/lihu_quantify/monitor/scheduler.py", "scheduler weekly分支"),
        (f"grep -c priority_sort {REMOTE}/src/lihu_quantify/backtest/engine.py", "engine priority_sort"),
        (f"grep -c healthcheck {REMOTE}/docker-compose.yml", "compose healthcheck"),
    ]:
        st, out, _ = run(probe)
        print(f"  {desc}: {out.strip()}")

    ssh.close()
    print("同步完成。src 需重建镜像：docker-compose up -d --build")


if __name__ == "__main__":
    main()

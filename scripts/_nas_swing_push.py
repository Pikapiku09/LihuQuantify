"""波段池文件推送 NAS（SSH exec base64，与 _nas_sftp_sync.py 同通道；2026-10-16）。

推送到 /volume2/Lihu_Quantify/data/swing_pool/（容器内 /app/data/swing_pool/，
供 lihu-scheduler 周五 21:00 的 swing_weekly 周报读取）：
  .dsh-invest/波段池_持仓.md               -> 波段池_持仓.md
  dsh-invest-plugin/docs/波段池纪律.md      -> 波段池纪律.md
  invest-outputs/*波段池*/报告.md（最新）   -> 波段池_最近报告.md
用法：NAS_PWD=... python scripts/_nas_swing_push.py
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import paramiko

HOST, USER, PORT = "192.168.123.203", "931570981", 22
REMOTE_DIR = "/volume2/Lihu_Quantify/data/swing_pool"
REMOTE_OUT = "/volume2/Lihu_Quantify/outputs"
WORKSPACE = Path(r"E:\Dsh_WorkSapce\Dify_Agents")
LIHU = Path(r"E:\LihuQuantify")


def items():
    """[(本地文件, 远端目录, 远端文件名)]。"""
    out = []
    pool = WORKSPACE / ".dsh-invest" / "波段池_持仓.md"
    rule = WORKSPACE / "dsh-invest-plugin" / "docs" / "波段池纪律.md"
    if pool.exists():
        out.append((pool, REMOTE_DIR, "波段池_持仓.md"))
    if rule.exists():
        out.append((rule, REMOTE_DIR, "波段池纪律.md"))
    reports = sorted(
        (WORKSPACE / "invest-outputs").glob("*波段池*/报告.md"),
        key=lambda p: p.parent.name, reverse=True)
    if reports:
        out.append((reports[0], REMOTE_DIR, "波段池_最近报告.md"))
    # 回测结果 → 看板数据源（outputs/ 卷）
    bt = LIHU / "outputs" / "swing_backtest.json"
    if bt.exists():
        out.append((bt, REMOTE_OUT, "swing_backtest.json"))
    rp = LIHU / "outputs" / "reports"
    bt_reports = sorted(rp.glob("*波段池回测报告.md")) if rp.exists() else []
    if bt_reports:
        out.append((bt_reports[-1], REMOTE_OUT + "/reports", bt_reports[-1].name))
    return out


def main() -> None:
    pwd = os.environ.get("NAS_PWD")
    if not pwd:
        print("NAS_PWD 必填", file=sys.stderr)
        sys.exit(1)
    files = items()
    if not files:
        print("无可推送文件", file=sys.stderr)
        sys.exit(1)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=pwd, port=PORT, timeout=15)

    def run(cmd, timeout=60):
        _, so, se = ssh.exec_command(cmd, timeout=timeout)
        out = so.read().decode(errors="replace")
        err = se.read().decode(errors="replace")
        return so.channel.recv_exit_status(), out, err

    dirs = sorted({d for _, d, _ in files})
    for d in dirs:
        st, _, err = run(f"mkdir -p {d}")
        if st != 0:
            print(f"建目录失败 {d}: {err}", file=sys.stderr)
            ssh.close()
            sys.exit(1)
    for lp, remote_dir, remote_name in files:
        b64 = base64.b64encode(lp.read_bytes()).decode()
        st, _, err = run(f"printf '%s' '{b64}' | base64 -d > '{remote_dir}/{remote_name}'")
        if st != 0:
            print(f"推送失败 {remote_name}: {err}", file=sys.stderr)
            ssh.close()
            sys.exit(1)
        print(f"推送 {lp.name} -> {remote_dir}/{remote_name}（{lp.stat().st_size} 字节）")
    st, out, _ = run(f"ls -la {REMOTE_DIR} && echo PUSH_OK")
    print(out.strip()[-800:])
    ssh.close()
    if "PUSH_OK" not in out:
        print("校验失败", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

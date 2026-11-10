"""NAS SSH 一次性操作（密码经环境变量 NAS_PWD 传入；NAS_SUDO=1 时走 sudo -S）。

NAS_HOST 可用环境变量覆盖：该 NAS 的 DHCP 地址已漂移两次（.204 → .203 → .204），
硬编码极易在漂移后静默失效。建议在 DSM 里为 NAS 绑定固定 IP。

凭据来源优先级：环境变量 NAS_PWD → 项目根 .env 的 NAS_PWD=...（.env 已在 .gitignore）。
口令不会出现在命令行/日志中。
"""
import os, sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
host = os.environ.get("NAS_HOST", "192.168.123.204")
user = "931570981"


def _nas_pwd() -> str:
    pwd = os.environ.get("NAS_PWD")
    if pwd:
        return pwd
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("NAS_PWD=") and not s.startswith("#"):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(
        "NAS_PWD 未设置：请放入环境变量，或在项目根 .env 写一行 NAS_PWD=你的NAS口令（.env 不入 git）"
    )


pwd = _nas_pwd()
cmd = sys.argv[1]
use_sudo = os.environ.get("NAS_SUDO", "0") == "1"

def shq(s):
    return chr(39) + s.replace(chr(39), chr(39) + "\\" + chr(39) + chr(39)) + chr(39)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=pwd, timeout=15)
full = ("sudo -S sh -c " + shq(cmd)) if use_sudo else cmd
stdin, stdout, stderr = c.exec_command(full, timeout=int(os.environ.get("CMD_TIMEOUT", "600")))
if use_sudo:
    stdin.write(pwd + chr(10)); stdin.flush()
rc = stdout.channel.recv_exit_status()
out = stdout.read().decode("utf-8", "replace")
err = stderr.read().decode("utf-8", "replace")
c.close()
print(out[-3000:] if out else "", flush=True)
if err.strip():
    print("[stderr]", err[-1500:], flush=True)
print("[exit %d]" % rc)
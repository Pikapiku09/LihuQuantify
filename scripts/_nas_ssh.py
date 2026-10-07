"""NAS SSH 一次性操作（密码经环境变量传入；NAS_SUDO=1 时走 sudo -S）。"""
import os, sys
import paramiko

host, user, pwd = "192.168.123.203", "931570981", os.environ["NAS_PWD"]
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
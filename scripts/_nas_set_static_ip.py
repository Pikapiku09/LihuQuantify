"""NAS eth0 静态 IP 切换/回退工具（经 DSM Web API，等同图形界面操作）。

安全设计：
  1) 先做 no-op set（use_dhcp=true，值与现状完全一致）验证 configs 结构；失败即中止。
  2) 再只翻转 use_dhcp → false；ip/mask/gateway/mtu/vlan 全部沿用现值。
  3) 应用后立即校验：DSM 记录、内核地址、网关连通、DNS 解析、容器内解析 Tushare。
  4) 任一校验失败 → 自动回滚为 DHCP。
shell 校验一律经 SSH 在 NAS 上执行（本机网络环境不同，不能本地跑）。
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_to_nas as d

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://192.168.123.204:5000/webapi"
ACCOUNT = "931570981"
IFNAME = "eth0"
EXPECT_IP = "192.168.123.204"
GW = "192.168.123.1"


def nas_pwd() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("NAS_PWD=") and not s.startswith("#"):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("未找到 NAS_PWD")


def call(params: dict, sid: str | None = None) -> dict:
    p = dict(params)
    if sid:
        p["_sid"] = sid
    url = f"{BASE}/entry.cgi?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def login() -> str:
    r = call({"api": "SYNO.API.Auth", "version": "6", "method": "login",
              "account": ACCOUNT, "passwd": nas_pwd(), "session": "Core", "format": "sid"})
    if not r.get("success"):
        raise SystemExit(f"登录失败: {r.get('error')}")
    return r["data"]["sid"]


def logout(sid: str) -> None:
    try:
        call({"api": "SYNO.API.Auth", "version": "6", "method": "logout", "session": "Core"}, sid)
    except Exception:
        pass


def get_eth(sid: str) -> dict:
    r = call({"api": "SYNO.Core.Network.Ethernet", "version": "2", "method": "get",
              "ifname": IFNAME}, sid)
    if not r.get("success"):
        raise RuntimeError(f"读取接口配置失败: {r.get('error')}")
    return r["data"]


def set_eth(sid: str, use_dhcp: bool, cur: dict, dns: str = "") -> dict:
    cfg = {
        "ifname": IFNAME,
        "type": cur.get("type", "lan"),
        "use_dhcp": use_dhcp,
        "ip": cur.get("ip", EXPECT_IP),
        "mask": cur.get("mask", "255.255.255.0"),
        "gateway": cur.get("gateway", GW),
        "dns": dns,
        "mtu": cur.get("mtu", 1500),
        "vlan_id": cur.get("vlan_id", 0),
        "enable_vlan": cur.get("enable_vlan", False),
    }
    return call({"api": "SYNO.Core.Network.Ethernet", "version": "2", "method": "set",
                 "configs": json.dumps([cfg], ensure_ascii=False)}, sid)


def ssh_run(ssh, cmd: str, timeout: int = 120) -> str:
    rc, out, err = d.run(ssh, cmd, sudo=True, timeout=timeout)
    return (out + err).replace("Password: ", "").strip()


def verify(ssh, expect_dhcp: bool) -> tuple[bool, list[str]]:
    notes, ok = [], True
    sid = None
    try:
        sid = login()
        eth = get_eth(sid)
        notes.append(f"DSM 记录: ip={eth.get('ip')} use_dhcp={eth.get('use_dhcp')}")
        if eth.get("ip") != EXPECT_IP:
            ok = False; notes.append(f"✗ IP 不符（期望 {EXPECT_IP}）")
        if eth.get("use_dhcp") != expect_dhcp:
            ok = False; notes.append(f"✗ use_dhcp 不符（期望 {expect_dhcp}）")
    except Exception as e:
        ok = False; notes.append(f"✗ DSM API 校验异常: {type(e).__name__}: {e}")
    finally:
        if sid:
            logout(sid)

    out = ssh_run(ssh, "ip -4 addr show eth0 | grep 'inet '")
    notes.append(f"内核地址: {out}")
    if EXPECT_IP not in out:
        ok = False

    gw = ssh_run(ssh, f"ping -c 2 -W 2 {GW} 2>&1 | tail -2")
    notes.append(f"网关 ping: {gw.replace(chr(10), ' / ')}")
    if "0% packet loss" not in gw:
        ok = False

    dns = ssh_run(ssh, "getent hosts api.tushare.pro || nslookup api.tushare.pro 2>&1 | tail -3")
    notes.append(f"主机解析 Tushare: {dns.replace(chr(10), ' / ')[:160]}")
    if not re.search(r"\d+\.\d+\.\d+\.\d+", dns):
        ok = False

    inner = ssh_run(ssh, "docker exec lihu-scheduler python -c "
                         "\"import socket;print(socket.gethostbyname('api.tushare.pro'))\" 2>&1 | tail -2")
    notes.append(f"容器内解析 Tushare: {inner}")
    if not re.search(r"^\d+\.\d+\.\d+\.\d+$", inner.strip().splitlines()[-1] if inner.strip() else ""):
        ok = False

    web = ssh_run(ssh, "docker ps --format '{{.Names}}|{{.Status}}' | head -4")
    notes.append(f"容器: {web.replace(chr(10), ' / ')}")
    if "healthy" not in web:
        ok = False
    return ok, notes


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="NAS eth0 静态 IP 切换/回退")
    ap.add_argument("--revert", action="store_true", help="回退为 DHCP（默认：切换为静态）")
    args = ap.parse_args()

    ssh = d.connect()
    try:
        sid = login()
        cur = get_eth(sid)
        print("=== 当前配置 ===")
        for k in ("ifname", "type", "use_dhcp", "ip", "mask", "gateway", "dns", "mtu", "vlan_id"):
            print(f"  {k}: {cur.get(k)}")

        if args.revert:
            if cur.get("use_dhcp"):
                print("\n当前已是 DHCP，无需回退。")
                return
            print("\n=== 回退为 DHCP ===")
            r = set_eth(sid, True, cur, dns=cur.get("dns", ""))
            print(f"  结果: {json.dumps(r, ensure_ascii=False)}")
            logout(sid)
            time.sleep(15)
            ok, notes = verify(ssh, expect_dhcp=True)
            for n in notes:
                print(f"  {n}")
            print("\n" + ("✓ 已回退为 DHCP" if ok else "✗ 回退后仍有异常，请用 DSM 图形界面检查"))
            return

        if not cur.get("use_dhcp"):
            print("\n当前已是静态 IP，无需修改。")
            return

        print("\n=== 步骤1：no-op set（同值、use_dhcp=true）校验字段结构 ===")
        r = set_eth(sid, True, cur, dns=cur.get("dns", ""))
        print(f"  结果: {json.dumps(r, ensure_ascii=False)}")
        if not r.get("success"):
            print("✗ no-op 校验失败，中止（未做任何改动）")
            return
        print("  ✓ 结构校验通过，配置未改动")
        time.sleep(3)

        print("\n=== 步骤2：切换为静态（IP/掩码/网关沿用现值）===")
        r = set_eth(sid, False, cur, dns="")
        print(f"  结果: {json.dumps(r, ensure_ascii=False)}")
        if not r.get("success"):
            print("✗ 切换失败，配置未生效")
            return
        logout(sid)

        print("  等待网络应用（20s）…")
        time.sleep(20)

        print("\n=== 步骤3：验证 ===")
        ok, notes = verify(ssh, expect_dhcp=False)
        for n in notes:
            print(f"  {n}")

        if ok:
            print("\n✓✓ 已固定为静态 IP，全部校验通过")
        else:
            print("\n✗ 校验未通过，自动回滚为 DHCP…")
            sid2 = login()
            r2 = set_eth(sid2, True, cur, dns=cur.get("dns", ""))
            print(f"  回滚结果: {json.dumps(r2, ensure_ascii=False)}")
            logout(sid2)
            time.sleep(15)
            ok2, notes2 = verify(ssh, expect_dhcp=True)
            for n in notes2:
                print(f"  [回滚后] {n}")
            print("  回滚" + ("成功" if ok2 else "后仍有异常，请用 DSM 图形界面检查"))
    finally:
        ssh.close()


if __name__ == "__main__":
    main()

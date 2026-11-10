"""从 NAS 单向导入生产状态到本机（用于本地迭代/复现）。

方向：NAS → 本机，**只有这一个方向**。
      本机的 `data/` 永远不会被 syncto_nas / 部署脚本推送回 NAS
      （`sync_to_nas.ps1` / `_nas_sftp_sync.py` 的 DIRS 均不含 data、outputs）。

安全设计：
  1) 先在本机把将被覆盖的文件打包备份到 backups/local_state_before_import_<ts>.zip
  2) 远端打包后下载单个归档，解到临时 staging 目录
  3) 校验 JSON 可解析、报告数量>0，再逐个覆盖（只覆盖同名文件，不动本机独有的研究数据）
  4) 写 data/_IMPORTED_FROM_NAS.json 标记（来源/时间/文件数），
     防止日后把"导进来的生产数据"误当成"本机自有状态"

用法：
    python scripts/import_from_nas.py --dry-run      # 只列清单，不下载不改动
    python scripts/import_from_nas.py                # 导入核心状态 + 报告
    python scripts/import_from_nas.py --with-db      # 额外导入 duckdb 行情库
    python scripts/import_from_nas.py --with-logs    # 额外导入运行日志
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_to_nas as d  # noqa: E402

REMOTE = d.REMOTE
STAGING = ROOT / "_nas_import_staging"
BACKUP_DIR = ROOT / "backups"
MARKER = ROOT / "data" / "_IMPORTED_FROM_NAS.json"

# 核心：账本 + 止损 + 巡检状态 + 闸门统计 + 热力图 + 股票池
CORE_PATTERNS = [
    "data/paper_state*.json",
    "data/stop_registry*.json",
    "data/pending_stops.json",
    "data/last_scan.json",
    "data/filter_stats.json",
    "data/heatmap_snapshot.json",
    "data/strat_pool_*.json",
    "data/swing_pool",
    "outputs/reports",
    "outputs/*.json",
]
DB_PATTERNS = ["data/lihu_quant.duckdb", "data/lihu_quant.duckdb.wal"]
LOG_PATTERNS = ["data/logs"]


def log(msg: str) -> None:
    print(f"[import] {msg}", flush=True)


def remote_tar(ssh, patterns: list[str], tgz_name: str) -> int:
    """远端打 tar 包（data/ 属 root，需 sudo）。返回包大小。"""
    pats = " ".join(patterns)
    cmd = (f"cd {REMOTE} && tar czf /tmp/{tgz_name} --ignore-failed-read {pats} 2>/dev/null; "
           f"ls -l /tmp/{tgz_name} | awk '{{print $5}}'")
    rc, out, err = d.run(ssh, cmd, sudo=True, timeout=900)
    txt = (out + err).replace("Password: ", "").strip()
    try:
        return int(txt.splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def backup_local(targets: list[Path]) -> Path | None:
    existing = [p for p in targets if p.exists()]
    if not existing:
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    zp = BACKUP_DIR / f"local_state_before_import_{stamp}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in existing:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(ROOT).as_posix())
            else:
                zf.write(p, p.relative_to(ROOT).as_posix())
    return zp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列远端清单，不下载不改动")
    ap.add_argument("--with-db", action="store_true", help="额外导入 lihu_quant.duckdb 行情库")
    ap.add_argument("--with-logs", action="store_true", help="额外导入 data/logs 运行日志")
    args = ap.parse_args()

    patterns = list(CORE_PATTERNS)
    if args.with_db:
        patterns += DB_PATTERNS
    if args.with_logs:
        patterns += LOG_PATTERNS

    ssh = d.connect()
    try:
        R = REMOTE
        print("=== 1. 远端清单预览 ===")
        for pat in patterns:
            rc, out, err = d.run(ssh, f"cd {R} && du -sh {pat} 2>/dev/null | tail -3",
                                 sudo=True, timeout=180)
            for line in (out + err).replace("Password: ", "").strip().splitlines():
                if line.strip():
                    print(f"  {line}")
        if args.dry_run:
            log("--dry-run：仅预览，未下载、未改动本机")
            return

        print("\n=== 2. 远端打包 ===")
        tgz_name = f"_pull_{int(time.time())}.tgz"
        size = remote_tar(ssh, patterns, tgz_name)
        if size <= 0:
            raise SystemExit("远端打包失败")
        log(f"远端包 {size/1024/1024:.2f} MB")

        print("\n=== 3. 下载并解到 staging ===")
        if STAGING.exists():
            shutil.rmtree(STAGING)
        STAGING.mkdir(parents=True)
        # 该 NAS 未启用 SFTP 子系统（ssh.open_sftp 报 Channel closed），
        # 改走 base64 over SSH（与 _nas_sftp_sync 的上传方向对称）。
        rc, b64, err = d.run(ssh, f"base64 /tmp/{tgz_name}", sudo=True, timeout=1800)
        payload = (b64 or "").strip()
        if not payload:
            raise SystemExit(f"远端读取失败: {(err or '').strip()[:200]}")
        local_tgz = STAGING / "pull.tgz"
        local_tgz.write_bytes(base64.b64decode(payload))
        d.run(ssh, f"rm -f /tmp/{tgz_name}", sudo=True)
        log(f"下载 {local_tgz.stat().st_size/1024/1024:.2f} MB")
        with tarfile.open(local_tgz, "r:gz") as tf:
            members = [m for m in tf.getmembers()
                       if not m.name.startswith(("/", "..")) and ".." not in Path(m.name).parts]
            tf.extractall(STAGING, members=members)
        local_tgz.unlink(missing_ok=True)
        staged = [p for p in STAGING.rglob("*") if p.is_file()]
        log(f"解出 {len(staged)} 个文件")

        print("\n=== 4. 校验（JSON 可解析 + 报告数）===")
        bad = []
        for p in staged:
            if p.suffix == ".json":
                try:
                    json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:
                    bad.append(f"{p.relative_to(STAGING)}: {type(e).__name__}")
        reports = [p for p in staged if p.suffix == ".md" and "reports" in p.parts]
        log(f"JSON 校验：{len([p for p in staged if p.suffix=='.json'])} 个，异常 {len(bad)}")
        for b in bad[:5]:
            log(f"  ✗ {b}")
        log(f"报告 md：{len(reports)} 份")
        if bad:
            raise SystemExit("存在损坏 JSON，中止导入（本机未被修改）")
        if not reports and not args.with_db:
            raise SystemExit("未取到任何报告，疑似打包异常，中止")

        print("\n=== 5. 备份本机将被覆盖的文件 ===")
        targets = []
        for p in staged:
            rel = p.relative_to(STAGING)
            targets.append(ROOT / rel)
        zp = backup_local(targets)
        log(f"备份：{zp}" if zp else "本机无同名文件，无需备份")

        print("\n=== 6. 落盘（只覆盖同名，保留本机独有研究数据）===")
        n_copied = 0
        for p in staged:
            rel = p.relative_to(STAGING)
            dst = ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            n_copied += 1
        log(f"覆盖 {n_copied} 个文件")

        print("\n=== 7. 写导入标记 ===")
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps({
            "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": f"{d.HOST}:{REMOTE}",
            "files": n_copied,
            "with_db": args.with_db,
            "with_logs": args.with_logs,
            "warning": "本机 data/ 为 NAS 生产数据的只读副本（快照）。"
                       "禁止把本机 data/ 反向推送回 NAS；NAS 侧 data/ 才是生产权威状态。",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"标记已写入 {MARKER.relative_to(ROOT)}")

        print("\n=== 8. 导入后抽查 ===")
        ps = ROOT / "data" / "paper_state.json"
        if ps.exists():
            j = json.loads(ps.read_text(encoding="utf-8"))
            log(f"paper_state.trade_day = {j.get('trade_day')}，"
                f"持仓 {len(j.get('positions', {}))} 只，成交 {len(j.get('trades', []))} 笔")
        ls = ROOT / "data" / "last_scan.json"
        if ls.exists():
            j = json.loads(ls.read_text(encoding="utf-8"))
            log(f"last_scan = {j.get('trade_date')}，市场状态 "
                f"{(j.get('summary') or {}).get('market_state')}")
        log(f"清理 staging：{STAGING}")
        shutil.rmtree(STAGING, ignore_errors=True)
    finally:
        ssh.close()


if __name__ == "__main__":
    main()

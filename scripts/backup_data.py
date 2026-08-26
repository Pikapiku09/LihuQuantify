"""每日备份（第四轮清单8）：模拟盘状态是连续验证的命根子。

备份内容：
    - data/           paper_state.json / stop_registry.json / pending_stops.json /
                      last_scan.json / filter_stats.json / duckdb / logs
                      （duckdb 为数据缓存，损坏可从 Tushare 重建，一并打包仅为省事）
    - outputs/        报告 / 图表
    - docs/决策日志.md

输出：backups/backup_YYYYMMDD_HHMMSS.zip，滚动保留最近 N 份（默认 30）。

用法：
    python scripts/backup_data.py                              # 备份
    python scripts/backup_data.py --keep 30                   # 保留份数
    python scripts/backup_data.py --restore backups/xxx.zip   # 恢复（恢复演练用）

调度建议：
    - Windows：任务计划程序每日 17:00（巡检 16:30 完成后）
    - NAS：DSM 任务计划每日 17:00；或直接用 Hyper Backup 备份整个项目目录
      （Hyper Backup 有版本去重，优先；本脚本为零依赖兜底方案）

注意：备份时若 scheduler 正在写 duckdb，zip 内的 duckdb 可能不一致——
      状态文件（paper_state 等 JSON 均为原子写）才是关键，duckdb 可重建。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 备份清单（相对 ROOT）
BACKUP_TARGETS = [
    "data",
    "outputs",
    "docs/决策日志.md",
]


def do_backup(keep: int) -> Path:
    """打包 → backups/，滚动删除最旧（超 keep 份）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"backup_{stamp}.zip"

    n_files, skipped = 0, []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for target in BACKUP_TARGETS:
            p = ROOT / target
            if not p.exists():
                print(f"[跳过] 不存在: {target}")
                continue
            if p.is_file():
                files = [p]
                arcnames = [target]
            else:
                files = sorted(p.rglob("*"))
                arcnames = [f.relative_to(ROOT) for f in files]
            for f, arc in zip(files, arcnames):
                if not f.is_file():
                    continue
                try:
                    zf.write(f, arc)
                    n_files += 1
                except PermissionError:
                    # 常见原因：duckdb 被运行中的 scheduler 锁定。
                    # duckdb 本就是可从 Tushare 重建的缓存，跳过不影响关键状态。
                    skipped.append(str(arc))

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[备份完成] {out.name}（{n_files} 个文件，{size_mb:.1f} MB）")
    if skipped:
        print(f"[注意] {len(skipped)} 个文件被占用未备份（duckdb 等可重建缓存）:")
        for s_ in skipped[:5]:
            print(f"    - {s_}")
        if len(skipped) > 5:
            print(f"    ... 等共 {len(skipped)} 个")

    # 滚动保留
    backups = sorted(backup_dir.glob("backup_*.zip"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"[滚动清理] 删除 {old.name}")
    return out


def do_restore(zip_path: str) -> None:
    """恢复（覆盖前自动把当前 data/ 再备份一份，防误操作）。"""
    src = Path(zip_path)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        print(f"[错误] 备份文件不存在: {src}")
        sys.exit(1)

    # 恢复前先保护当前状态
    safety = do_backup(keep=999)
    print(f"[恢复前] 当前状态已存档 → {safety.name}")

    print(f"[恢复] {src} → 项目根目录（覆盖同名文件）")
    with zipfile.ZipFile(src) as zf:
        names = zf.namelist()
        confirm = input(f"共 {len(names)} 个文件，确认覆盖？(y/N) ").strip().lower()
        if confirm != "y":
            print("[取消] 未做任何修改")
            return
        zf.extractall(ROOT)
    print("[恢复完成] 请刷新看板核对 paper_state / 权益曲线是否与删除前一致")


def main():
    parser = argparse.ArgumentParser(description="LihuQuantify 备份/恢复（第四轮清单8）")
    parser.add_argument("--keep", type=int, default=30, help="滚动保留份数（默认 30）")
    parser.add_argument("--restore", type=str, default="", help="从指定 zip 恢复（恢复演练用）")
    args = parser.parse_args()

    if args.restore:
        do_restore(args.restore)
    else:
        do_backup(args.keep)


if __name__ == "__main__":
    main()

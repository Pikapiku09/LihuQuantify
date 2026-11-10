"""提交/推送前的密钥扫描器（防复发）。

背景：2026-11-10 发现 docs/决策日志.md 中的 NAS 明文口令已随提交 701df28
推送到公开仓库并暴露约 16 天。此脚本用于在提交前拦截同类问题。

用法：
    python scripts/check_secrets.py            # 扫描将被提交的内容（默认）
    python scripts/check_secrets.py --staged   # 只扫暂存区（pre-commit 用）
    python scripts/check_secrets.py --history  # 额外扫描全部提交历史
    python scripts/check_secrets.py --all      # staged + history

退出码：0 = 干净；2 = 发现疑似密钥（阻止提交/推送）

注意：Windows 上 git ls-files 默认会把非 ASCII 路径转义成
      "docs/\\345\\206\\263..."，直接用 Path 拼接会全部读不到而漏报。
      本脚本显式使用 core.quotepath=false 并做二次校验。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".tgz", ".gz",
                   ".duckdb", ".parquet", ".wal", ".pdf", ".woff", ".woff2", ".so", ".pyd"}

GENERIC_PATTERNS = {
    "私钥块": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub Token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS AccessKey": re.compile(r"AKIA[0-9A-Z]{16}"),
    "URL 内嵌 token": re.compile(r"[?&](token|access_token|key)=[A-Za-z0-9_\-]{20,}"),
    # 只抓"文档/代码里写死的字面量口令"。刻意不收 `pwd = os.environ[...]` 这类
    # 变量赋值——那是正确用法，否则会淹没真实告警（2026-11-10 实测误报 12 处）。
    "写死的口令字面量": re.compile(
        r"(?:密码|口令|password|passwd)\s*[:=＝]\s*[\"'][^\"'\s]{6,}[\"']", re.I),
    # 中文文档体：「用户名 xxx / 密码 yyy」
    "文档凭据对": re.compile(
        r"(?:用户名|账号|account)\s*[:：]?\s*\S{2,}?\s*/\s*(?:密码|口令)\s*[:：]?\s*[A-Za-z0-9!@#$%^&*_\-]{6,}",
        re.I),
}

# 出现在这些上下文里的赋值属于正确用法，跳过
SAFE_CONTEXT = ("os.environ", "getenv", "sys.argv", "_nas_pwd(", "input(", "$env:",
                "${", "******", "<口令已隐去>", "已移除")


def _is_safe_line(line: str) -> bool:
    return any(tok in line for tok in SAFE_CONTEXT)


def git(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, shell=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def load_env_secrets() -> dict[str, str]:
    """从 .env / tushareMcp.json 读取真实密钥，用于字面量比对。"""
    out: dict[str, str] = {}
    envp = ROOT / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if len(v) >= 6:
                    out[f".env:{k.strip()}"] = v
    tp = ROOT / "tushareMcp.json"
    if tp.exists():
        m = re.search(r"token=([A-Za-z0-9]{8,})",
                      tp.read_text(encoding="utf-8", errors="replace"))
        if m:
            out["tushareMcp.json:token"] = m.group(1)
    return out


def list_files(staged_only: bool) -> list[str]:
    flag = "--cached" if staged_only else ""
    p = git(f"git -c core.quotepath=false diff --cached --name-only {flag}"
            if staged_only else "git -c core.quotepath=false ls-files")
    files = [ln for ln in p.stdout.splitlines() if ln.strip()]
    # 二次校验：过滤不存在的路径，避免因转义导致静默跳过
    exists = [f for f in files if (ROOT / f).is_file()]
    if len(exists) != len(files):
        missing = [f for f in files if not (ROOT / f).is_file()]
        print(f"[warn] {len(missing)} 个路径未能在磁盘定位（可能是删除或转义问题）：",
              file=sys.stderr)
        for m in missing[:5]:
            print(f"        {m}", file=sys.stderr)
    return exists


def mask(s: str) -> str:
    return f"{s[:2]}***{s[-2:]}"


def scan_files(files: list[str], secrets: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for rel in files:
        p = ROOT / rel
        if p.suffix.lower() in BINARY_SUFFIXES or p.stat().st_size > 8 * 1024 * 1024:
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for name, val in secrets.items():
            if val in txt:
                problems.append(f"真实密钥命中 {name}（{mask(val)}）: {rel}")
        for label, pat in GENERIC_PATTERNS.items():
            for lineno, line in enumerate(txt.splitlines(), 1):
                if pat.search(line) and not _is_safe_line(line):
                    problems.append(f"{label} 疑似（行 {lineno}）: {rel}")
    return problems


def scan_history(secrets: dict[str, str]) -> list[str]:
    problems: list[str] = []
    shas = [x for x in git("git rev-list --all").stdout.split() if x]
    for name, val in secrets.items():
        for sha in shas:
            if git(f'git grep -l -F -e "{val}" {sha} -- 2>nul').stdout.strip():
                problems.append(f"历史泄露 {name}（{mask(val)}）出现在 {sha[:8]}")
                break
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="只扫暂存区（pre-commit）")
    ap.add_argument("--history", action="store_true", help="额外扫描全部提交历史")
    ap.add_argument("--all", action="store_true", help="staged + history")
    args = ap.parse_args()

    secrets = load_env_secrets()
    print(f"[scan] 已加载 {len(secrets)} 个真实密钥用于比对"
          if secrets else "[scan] 未找到 .env / tushareMcp.json，仅做通用模式扫描")

    staged_only = args.staged or args.all
    files = list_files(staged_only)
    print(f"[scan] 扫描 {len(files)} 个文件（{'暂存区' if staged_only else '全部跟踪文件'}）")

    problems = scan_files(files, secrets)
    if args.history or args.all:
        print("[scan] 扫描提交历史…")
        problems += scan_history(secrets)

    if problems:
        print("\n✗ 发现疑似密钥，已阻止：\n")
        for p in problems:
            print(f"  - {p}")
        print("\n处置建议：改用环境变量或 .env（勿入库），并把明文从历史中清除"
              "（git filter-repo --replace-text）。")
        sys.exit(2)

    print("✓ 未发现密钥，可以提交/推送")
    sys.exit(0)


if __name__ == "__main__":
    main()

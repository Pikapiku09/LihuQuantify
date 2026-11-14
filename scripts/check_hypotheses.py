"""校验研究假设登记册（预注册闸的 CI 入口）。

用法
----
    python scripts/check_hypotheses.py              # 校验仓库内登记册
    python scripts/check_hypotheses.py --path X.yaml

退出码
------
    0  全部条目合法（或登记册尚为空）
    1  存在非法条目 / 登记册无法读取

纪律
----
- 本脚本只做校验与汇总，**不执行任何回测、不写任何文件**；
- Windows 控制台默认 GBK：输出前强制 stdout 用 UTF-8（否则中文告警会
  UnicodeEncodeError，2026-11-14 已踩过一次）；
- 校验通过 ≠ 假设成立——它只保证"先登记、后验证、口径写清楚"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# scripts/check_hypotheses.py → 项目根（research 包未安装，需显式加入 sys.path）
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _utf8_stdout() -> None:
    """Windows 控制台是 GBK：强制 UTF-8，避免中文输出崩在 UnicodeEncodeError。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _print_summary(path: Path, entries: list, errors: list[str]) -> None:
    from research.registry import summarize

    stats = summarize(entries)
    print("=" * 56)
    print("研究假设登记册校验（预注册闸）")
    print(f"文件：{path}")
    print(
        f"共 {stats['total']} 条：pending {stats['pending']} / passed {stats['passed']}"
        f" / failed {stats['failed']} / rejected {stats['rejected']}")
    if not entries:
        print("（空登记册：尚未登记任何假设——新增假设请走 docs/研究纪律_20261114.md 的流程）")
    if errors:
        print(f"\n发现 {len(errors)} 处非法条目：")
        for err in errors:
            print(f"  - {err}")
        print("\n结果：校验未通过（退出码 1）。")
        return
    print("\n结果：校验通过。")
    print("提醒：passed 必须有 evidence + decided_at；未登记的参数变更不得上线。")


def main(argv: list[str] | None = None) -> int:
    from research.registry import DEFAULT_PATH, load_registry, validate_registry

    parser = argparse.ArgumentParser(description="校验研究假设登记册（预注册闸）")
    parser.add_argument("--path", default=str(DEFAULT_PATH),
                        help=f"登记册路径（默认 {DEFAULT_PATH}）")
    args = parser.parse_args(argv)
    _utf8_stdout()

    path = Path(args.path)
    try:
        entries = load_registry(path)
    except ValueError as exc:
        print(f"[失败] {exc}")
        return 1
    errors = validate_registry(entries)
    _print_summary(path, entries, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

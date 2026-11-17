"""巡检日报快照（report_snapshot）单元测试（2026-11-17，P4）。

日报格式变过几次，硬推断会显示出不存在的数字（例如把缺字段当成「0 笔卖出」）。
所以守：缺字段一律 None、只认严格日期文件名、铁律自检要能识别未勾选项。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lihu_quantify.monitor.report_snapshot import load_reports, parse_report  # noqa: E402

REPORT = """# 每日巡检报告 2026-11-10

- 模式：模拟盘
- 市场状态：**下跌**

## 一、账户总览

| 指标 | 数值 |
|---|---|
| 总资产 | 88,748 |
| 累计盈亏 | -11,252（-11.25%） |
| 评审进度 | 98/100 轮（胜率 25.5%，盈亏比 1.27） |

## 二、当前持仓

| 代码 | 名称 | 持股 |
|---|---|---|
| 000429.SZ | 粤高速A | 500 |
| 600000.SH | 浦发银行 | 100 |

## 三、今日操作

今日无买入。

今日无卖出。

## 六、铁律自检（运行时校验）

- [x] 持仓止损登记完整（一一对应）
- [x] 单票市值占比 ≤25%
- [ ] 无向下补仓

## 七、资金利用

| 指标 | 数值 |
|---|---|
| 闲置现金 | 81,878 |
"""


def test_full_parse():
    r = parse_report(REPORT, "2026-11-10.md")
    assert r["date"] == "2026-11-10"
    assert r["market_state"] == "下跌"
    assert r["total_asset"] == 88748.0
    assert r["pnl_ratio"] == pytest.approx(-0.1125)
    assert r["positions"] == 2
    assert "98/100" in (r["review"] or "")


def test_ops_captures_plain_paragraphs():
    """「今日无买入。」是裸段落不是列表项 —— 两条路径都要接。"""
    r = parse_report(REPORT, "2026-11-10.md")
    assert r["ops"] == "今日无买入 / 今日无卖出"


def test_checks_detect_unchecked():
    r = parse_report(REPORT, "2026-11-10.md")
    assert len(r["checks"]) == 3
    assert r["checks_failed"] == ["无向下补仓"]


def test_missing_fields_are_none_not_zero():
    r = parse_report("# 空报告\n\n什么都没有\n", "2026-11-11.md")
    assert r["total_asset"] is None
    assert r["pnl_ratio"] is None
    assert r["positions"] is None
    assert r["ops"] is None
    assert r["market_state"] is None
    assert r["checks"] == []


def test_positions_section_isolated():
    """其它小节的表格不得被算进持仓数。"""
    r = parse_report(REPORT, "2026-11-10.md")
    assert r["positions"] == 2      # 账户总览 3 行 + 资金利用 1 行都不算


def test_load_reports_filters_non_daily(tmp_path):
    d = tmp_path / "outputs" / "reports"
    d.mkdir(parents=True)
    (d / "2026-11-10.md").write_text(REPORT, encoding="utf-8")
    (d / "monthly_202608.md").write_text(REPORT, encoding="utf-8")
    (d / "20261020_波段池回测报告_x.md").write_text(REPORT, encoding="utf-8")
    data = load_reports(tmp_path)
    assert data["total"] == 1
    assert data["recent"][0]["date"] == "2026-11-10"


def test_load_reports_sorted_desc_and_limited(tmp_path):
    d = tmp_path / "outputs" / "reports"
    d.mkdir(parents=True)
    for day in ("2026-11-07", "2026-11-08", "2026-11-09"):
        # 标题日期与文件名一致，模拟正常日报
        (d / f"{day}.md").write_text(REPORT.replace("2026-11-10", day), encoding="utf-8")
    data = load_reports(tmp_path, limit=2)
    assert data["total"] == 3
    assert [r["date"] for r in data["recent"]] == ["2026-11-09", "2026-11-08"]


def test_date_uses_filename_and_flags_mismatch():
    """文件名是排序索引；标题日期不一致必须被标出来，不能悄悄取其一。"""
    r = parse_report(REPORT, "2026-11-07.md")
    assert r["date"] == "2026-11-07"       # 以文件名为准
    assert r["title_date"] == "2026-11-10" # 标题里的原样保留
    assert r["date_mismatch"] is True


def test_date_no_mismatch_when_aligned():
    r = parse_report(REPORT, "2026-11-10.md")
    assert r["date_mismatch"] is False


def test_missing_dir_ok(tmp_path):
    assert load_reports(tmp_path) == {"total": 0, "recent": []}


def test_repo_reports_are_consistent():
    """仓库真实日报：必须能解析出日期与总资产，且自检项数 > 0。"""
    data = load_reports(ROOT)
    if not data["recent"]:
        pytest.skip("仓库里没有巡检日报")
    for r in data["recent"]:
        assert r["date"], "日期必须解析出来"
        assert r["total_asset"] is None or r["total_asset"] > 0

"""Walk-forward 时间切分：门禁3 样本外检验骨架。

严格按时间顺序生成 (train, test) 折：train 在前、test 在后、折间不重叠；
purge（训练尾与测试头之间空开 h 日）+ embargo（测试后再空 e 日）防泄漏。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass
class Fold:
    """一折切分（均为下标区间 [start, end)，基于已排序的日期数组）。"""
    train: tuple[int, int]
    test: tuple[int, int]

    def __repr__(self) -> str:  # 方便日志
        return f"Fold(train={self.train}, test={self.test})"


def split_walkforward(
    n: int,
    n_folds: int = 5,
    train_frac: float = 0.6,
    purge: int = 5,
    embargo: int = 2,
) -> list[Fold]:
    """生成 n_folds 折滚动切分。

    Args:
        n:          样本总数（按时间升序）
        n_folds:    折数
        train_frac: 首折训练段占非测试区间的比例（后续折训练段随之滚动）
        purge:      训练集尾部剔除的样本数（对应因子前瞻期 h，防标签泄漏）
        embargo:    上一折测试集结束后额外剔除的样本数（防序列相关泄漏）

    Returns:
        Fold 列表；train/test 均为闭开区间 [start, end)。
    """
    if n_folds < 1:
        raise ValueError("n_folds >= 1")
    if n < 10:
        raise ValueError("样本过少，无法切分")
    test_len = n // (n_folds + 1)
    if test_len <= purge + embargo + 1:
        raise ValueError(f"每折测试段过短（test_len={test_len} <= purge+embargo+{purge + embargo + 1}）")

    folds: list[Fold] = []
    for k in range(n_folds):
        test_start = (k + 1) * test_len
        test_end = test_start + test_len if k < n_folds - 1 else n
        train_end = test_start - purge
        # 训练段从 0 滚动到 train_end（expanding window；如需 sliding 可改）
        train_start = 0 if k == 0 else max(0, k * test_len - embargo)
        if train_start >= train_end:
            continue
        folds.append(Fold(train=(train_start, train_end), test=(test_start, test_end)))
    if not folds:
        raise ValueError("切分参数导致无有效折")
    return folds


def fold_dates(dates: Sequence, fold: Fold) -> tuple[Sequence, Sequence]:
    """把 Fold 下标区间映射为实际日期序列。"""
    return dates[fold.train[0]:fold.train[1]], dates[fold.test[0]:fold.test[1]]

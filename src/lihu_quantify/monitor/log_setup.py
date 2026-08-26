"""loguru 文件日志（第四轮清单7：无人值守留痕 + 30 天滚动）。

- 落盘 data/logs/{name}_{time:YYYYMMDD}.log，每天 0 点轮转
- retention="30 days"：30 天前的旧日志自动删除
- level=INFO：买卖、止损登记/执行、巡检完成等关键操作全部落盘
- 不同进程用不同 name（scheduler / web），避免多进程写同一文件

用法（进程入口调用一次即可，控制台 sink 不受影响）：
    from lihu_quantify.monitor.log_setup import setup_file_logging
    setup_file_logging("scheduler")
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

_ROOT = Path(__file__).resolve().parents[3]

# 幂等标记（同进程重复调用不重复添加 sink）
_added: set[str] = set()


def setup_file_logging(name: str = "lihu_quant", retention_days: int = 30) -> None:
    """初始化文件 sink。name 用于区分进程（scheduler/web），也用作文件名前缀。"""
    if name in _added:
        return
    log_dir = _ROOT / "data" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_dir / f"{name}_{{time:YYYYMMDD}}.log"),
            rotation="00:00",            # 每天 0 点新建文件
            retention=f"{retention_days} days",
            encoding="utf-8",
            level="INFO",
            backtrace=False,
            diagnose=False,              # 生产环境不落本地路径等敏感上下文
        )
        _added.add(name)
        logger.debug(f"[日志] 文件 sink 已启用: {log_dir}/{name}_YYYYMMDD.log（保留 {retention_days} 天）")
    except Exception as e:  # 磁盘满/权限等不阻断主流程
        logger.warning(f"[日志] 文件 sink 启用失败（仅控制台输出）: {e}")

"""日志模块：同时输出到控制台与文件。"""

from __future__ import annotations

import logging
import os
import sys


def setup_logger(name: str = "wxsearch", level: str = "INFO", log_file: str = "logs/collector.log") -> logging.Logger:
    """创建并配置日志器。重复调用不会叠加 handler。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出（用 stdout，保证中文正常）
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 文件输出
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger

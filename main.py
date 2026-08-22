"""程序入口。

用法：
    python main.py                     # 使用 config.json 中的关键词
    python main.py -k 人工智能 大模型    # 命令行指定关键词（覆盖配置）
    python main.py -c my_config.json   # 指定配置文件
    python main.py --unattended        # 无人值守长跑：领关键词→采集→上报→休眠→下一轮
"""

from __future__ import annotations

import argparse
import os
import sys

from wxsearch.collector import Collector
from wxsearch.config import load_config
from wxsearch.logger import setup_logger


def _anchor_cwd() -> None:
    """将工作目录锚定到本文件所在目录（包根）。

    config.json、data/、logs/ 等路径均相对包根解析。若调用方未先 cd 到部署目录
    （例如直接 `python /abs/path/main.py`）就会解析到错误目录：config 回退默认值、
    日志/库写到别处，且异常发生在建库/建目录时会以 exit=1 静默退出。锚定 cwd
    到脚本目录可让任意启动方式都健壮（run.bat 的 cd 仍保留，双保险）。
    """
    try:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))
    except Exception:  # noqa: BLE001
        pass


def parse_args():
    parser = argparse.ArgumentParser(description="微信 PC 搜一搜公众号文章采集器")
    parser.add_argument("-c", "--config", default="config.json", help="配置文件路径")
    parser.add_argument("-k", "--keywords", nargs="+", help="搜索关键词（可多个，覆盖配置文件）")
    parser.add_argument(
        "--unattended", action="store_true",
        help="无人值守长跑模式（领关键词→采集→上报→休眠→下一轮，绝不整体退出）",
    )
    return parser.parse_args()


def main() -> int:
    _anchor_cwd()
    args = parse_args()
    cfg = load_config(args.config)
    if args.keywords:
        cfg.keywords = args.keywords

    logger = setup_logger("wxsearch", cfg.log_level, cfg.log_file)

    # 无人值守长跑模式：领关键词→采集→上报→休眠→下一轮，异常自愈、绝不整体退出。
    # 关键词来自 worker 端调度（非本地 config.keywords），故不校验 keywords。
    if args.unattended:
        from wxsearch.unattended import UnattendedRunner
        logger.info("启动无人值守模式。请确保微信 PC 客户端已启动并登录。")
        try:
            return UnattendedRunner(cfg, logger).run_forever()
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"无人值守异常退出：{exc}")
            return 1

    if not cfg.keywords:
        logger.error("未配置任何关键词，请在 config.json 中填写 keywords 或使用 -k 指定。")
        return 2

    logger.info(f"待采集关键词：{cfg.keywords}")
    logger.info("请确保微信 PC 客户端已启动并登录，采集过程中请勿操作鼠标键盘。")

    try:
        Collector(cfg, logger).run()
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"程序异常退出：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""后台入口。

用法：
    python web.py                 # 启动本地后台，默认 http://127.0.0.1:5000
    python web.py -p 8080         # 指定端口
    python web.py -c my.json      # 指定配置文件

后台提供网页界面管理关键词/筛选条件/采集参数，并可一键启动采集、查看实时日志。
配置写入 config.json，与命令行 `python main.py` 共用同一份配置。
"""

from __future__ import annotations

import argparse
import sys

from wxsearch.webapp import create_app


def parse_args():
    parser = argparse.ArgumentParser(description="微信搜一搜采集器 · 本地 Web 后台")
    parser.add_argument("-c", "--config", default="config.json", help="配置文件路径")
    parser.add_argument("-H", "--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("-p", "--port", type=int, default=5000, help="监听端口")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = create_app(args.config)
    url = f"http://{args.host}:{args.port}"
    print(f"后台已启动：{url}  （Ctrl+C 停止）")
    # 采集依赖 UI 自动化，须单进程运行，关闭 reloader 与多线程重载
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

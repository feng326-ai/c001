"""Flask 本地后台。

提供网页界面管理三类配置——关键词、筛选条件（排序/类型/时间/范围）、
采集参数——并支持一键启动采集与实时查看日志。所有配置读写都落到
config.json，命令行 `python main.py` 与后台共用同一份配置。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import List, Optional

from flask import Flask, jsonify, render_template, request

from .collector import Collector
from .config import load_config, save_config
from .db import Database
from .logger import setup_logger


class _BufferLogHandler(logging.Handler):
    """把日志追加到内存环形缓冲，供后台页面轮询展示。"""

    def __init__(self, maxlen: int = 800):
        super().__init__()
        self.buffer = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            msg = record.getMessage()
        self.buffer.append(msg)

    def snapshot(self) -> List[str]:
        return list(self.buffer)


class CollectRunner:
    """管理后台采集线程与运行状态（单例）。

    采集过程会占用鼠标键盘（UI 自动化），故同一时刻只允许一个任务运行。
    每次运行都重新读取 config.json，保证后台刚保存的配置立即生效。
    """

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.running = False
        self.started_at = ""
        self.finished_at = ""
        self.last_error = ""
        self.log_handler = _BufferLogHandler()
        self.log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )

    def start(self, keywords: Optional[List[str]] = None) -> bool:
        """启动后台采集；已在运行则返回 False。"""
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = ""
            self.last_error = ""
            self.log_handler.buffer.clear()
        self._thread = threading.Thread(target=self._run, args=(keywords,), daemon=True)
        self._thread.start()
        return True

    def _run(self, keywords: Optional[List[str]]) -> None:
        logger = logging.getLogger("wxsearch")
        try:
            cfg = load_config(self.config_path)
            if keywords:
                cfg.keywords = keywords
            logger = setup_logger("wxsearch", cfg.log_level, cfg.log_file)
            if self.log_handler not in logger.handlers:
                logger.addHandler(self.log_handler)
            if not cfg.keywords:
                logger.error("未配置任何关键词，请先在后台填写关键词。")
                return
            logger.info(f"待采集关键词：{cfg.keywords}")
            logger.info("请确保微信 PC 客户端已登录，采集过程中请勿操作鼠标键盘。")
            Collector(cfg, logger).run()
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.exception(f"采集异常退出：{exc}")
        finally:
            self.running = False
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def status(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_error": self.last_error,
            "logs": self.log_handler.snapshot(),
        }


def _config_view(config_path: str) -> dict:
    """读取当前配置，整理成后台页面需要的结构。"""
    cfg = load_config(config_path)
    sel = cfg.selectors
    return {
        "keywords": cfg.keywords,
        "result_type": cfg.result_type,
        "filters": {
            "sort": sel.filter_sort,
            "type": sel.filter_type,
            "time": sel.filter_time,
            "scope": sel.filter_scope,
        },
        "collect": {
            "max_items_per_keyword": cfg.collect.max_items_per_keyword,
            "max_scrolls": cfg.collect.max_scrolls,
            "scroll_pause_sec": cfg.collect.scroll_pause_sec,
            "stop_after_no_new_rounds": cfg.collect.stop_after_no_new_rounds,
            "fetch_url": cfg.collect.fetch_url,
        },
    }


def _stats(config_path: str) -> dict:
    cfg = load_config(config_path)
    try:
        db = Database(cfg.db_path)
        total = db.count()
        db.close()
    except Exception:  # noqa: BLE001
        total = 0
    return {"total": total, "db_path": cfg.db_path}


def create_app(config_path: str = "config.json") -> Flask:
    app = Flask(__name__)
    runner = CollectRunner(config_path)
    app.config["RUNNER"] = runner

    @app.route("/")
    def index():
        return render_template("admin.html")

    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(_config_view(config_path))

    @app.route("/api/config", methods=["POST"])
    def post_config():
        data = request.get_json(force=True, silent=True) or {}
        updates: dict = {}

        if "keywords" in data:
            kws = data["keywords"]
            if isinstance(kws, str):
                kws = [k.strip() for k in kws.replace("\n", ",").split(",")]
            updates["keywords"] = [k for k in (kws or []) if str(k).strip()]

        if "result_type" in data:
            updates["result_type"] = str(data["result_type"] or "article")

        filters = data.get("filters") or {}
        sel_updates = {}
        if "sort" in filters:
            sel_updates["filter_sort"] = str(filters["sort"] or "")
        if "type" in filters:
            sel_updates["filter_type"] = str(filters["type"] or "")
        if "time" in filters:
            sel_updates["filter_time"] = str(filters["time"] or "")
        if "scope" in filters:
            sel_updates["filter_scope"] = str(filters["scope"] or "")
        if sel_updates:
            updates["selectors"] = sel_updates

        collect = data.get("collect") or {}
        col_updates = {}
        for key, caster in (
            ("max_items_per_keyword", int),
            ("max_scrolls", int),
            ("scroll_pause_sec", float),
            ("stop_after_no_new_rounds", int),
        ):
            if key in collect and collect[key] not in ("", None):
                try:
                    col_updates[key] = caster(collect[key])
                except (TypeError, ValueError):
                    pass
        if "fetch_url" in collect:
            col_updates["fetch_url"] = bool(collect["fetch_url"])
        if col_updates:
            updates["collect"] = col_updates

        save_config(updates, config_path)
        return jsonify({"ok": True, "config": _config_view(config_path)})

    @app.route("/api/stats", methods=["GET"])
    def get_stats():
        return jsonify(_stats(config_path))

    @app.route("/api/collect/start", methods=["POST"])
    def start_collect():
        if runner.running:
            return jsonify({"ok": False, "msg": "采集正在进行中，请等待完成。"}), 409
        data = request.get_json(force=True, silent=True) or {}
        keywords = data.get("keywords") or None
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace("\n", ",").split(",") if k.strip()]
        ok = runner.start(keywords)
        return jsonify({"ok": ok})

    @app.route("/api/collect/status", methods=["GET"])
    def collect_status():
        return jsonify(runner.status())

    return app

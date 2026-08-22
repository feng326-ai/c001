"""
数据备份层（阶段四·无人值守 第 3 步）——默认安全、可开关、失败不崩。

定位：由 celery-beat 每日定时触发（在 worker 执行），用 pg_dump 生成 PostgreSQL
的标准可恢复备份，落到宿主机挂载卷，并按份数轮转。设计与调度层/告警层一致：
  - 总开关 BACKUP_ENABLED（默认 true）；关闭时 run_backup() 直接跳过；
  - 输出目录 BACKUP_DIR（默认 /app/backups，compose 挂到宿主机 ./backups）；
  - 轮转保留最近 BACKUP_KEEP 份（默认 7），多余的按时间删除；
  - 永不抛异常：备份失败只 log，绝不拖垮 worker。

依赖：worker 镜像内的 pg_dump（见 Dockerfile 的 postgresql-client）。
备份格式：pg_dump 自定义格式（-Fc，自带压缩），恢复用
    pg_restore -d <db> <file>   或   pg_restore --list <file> 查看内容。

环境变量：
  BACKUP_ENABLED : 1/true/yes/on 开启（缺省 true）。
  BACKUP_DIR     : 备份输出目录（缺省 /app/backups）。
  BACKUP_KEEP    : 保留最近份数（缺省 7，<=0 视为不轮转）。
  DATABASE_URL   : postgresql://user:pwd@host:port/db（解析连接参数）。
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from typing import List, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

BACKUP_PREFIX = "wx_search_"
BACKUP_SUFFIX = ".dump"


def _truthy(val) -> bool:
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Backuper:
    """PostgreSQL 备份与轮转。用 pg_dump 自定义格式导出，落盘后保留最近 N 份。"""

    def __init__(self, enabled: bool = True, backup_dir: str = "/app/backups",
                 keep: int = 7, database_url: str = ""):
        self.enabled = bool(enabled)
        self.backup_dir = backup_dir or "/app/backups"
        self.keep = keep
        self.database_url = database_url or ""

    @classmethod
    def from_env(cls) -> "Backuper":
        return cls(
            enabled=_truthy(os.getenv("BACKUP_ENABLED", "true")),
            backup_dir=os.getenv("BACKUP_DIR", "/app/backups"),
            keep=_int_env("BACKUP_KEEP", 7),
            database_url=os.getenv("DATABASE_URL", ""),
        )

    # ---- 连接参数解析 ----
    def _conn(self) -> dict:
        """从 DATABASE_URL 解析 pg_dump 所需连接参数。"""
        p = urlsplit(self.database_url)
        return {
            "host": p.hostname or "localhost",
            "port": str(p.port or 5432),
            "user": p.username or "admin",
            "password": p.password or "",
            "database": (p.path or "/wx_search").lstrip("/") or "wx_search",
        }

    # ---- 入口：beat 任务调用 ----
    def run_backup(self) -> dict:
        """执行一次备份：pg_dump -Fc → 落盘 → 轮转。任何失败只 log 不抛。"""
        if not self.enabled:
            return {"skipped": "backup_disabled"}

        try:
            os.makedirs(self.backup_dir, exist_ok=True)
        except OSError as e:
            log.error(f"[备份] 创建目录失败 {self.backup_dir}: {e}")
            return {"error": f"mkdir_failed: {e}"}

        conn = self._conn()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{BACKUP_PREFIX}{ts}{BACKUP_SUFFIX}"
        filepath = os.path.join(self.backup_dir, filename)

        cmd = [
            "pg_dump",
            "-h", conn["host"],
            "-p", conn["port"],
            "-U", conn["user"],
            "-d", conn["database"],
            "-Fc",              # 自定义格式，自带压缩、支持 pg_restore 选择性恢复
            "-f", filepath,
        ]
        env = dict(os.environ)
        env["PGPASSWORD"] = conn["password"]

        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, timeout=600
            )
        except FileNotFoundError:
            log.error("[备份] 未找到 pg_dump，请确认 worker 镜像已装 postgresql-client")
            return {"error": "pg_dump_not_found"}
        except subprocess.TimeoutExpired:
            log.error("[备份] pg_dump 超时（>600s）")
            self._safe_remove(filepath)
            return {"error": "timeout"}
        except Exception as e:  # noqa: BLE001
            log.error(f"[备份] pg_dump 执行异常：{e}")
            return {"error": str(e)}

        if proc.returncode != 0:
            log.error(f"[备份] pg_dump 失败(rc={proc.returncode}): {proc.stderr.strip()}")
            self._safe_remove(filepath)
            return {"error": f"pg_dump_rc_{proc.returncode}", "stderr": proc.stderr.strip()}

        size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        rotated = self._rotate()
        log.info(f"✅ [备份] 完成 {filename}（{size} 字节），轮转删除 {len(rotated)} 份旧备份")
        return {"backup": filename, "path": filepath, "size": size, "rotated": rotated}

    # ---- 轮转：保留最近 keep 份 ----
    def _rotate(self) -> List[str]:
        """按修改时间保留最近 keep 份，删除多余旧备份。返回被删文件名列表。"""
        if self.keep is None or self.keep <= 0:
            return []
        try:
            files = [
                f for f in os.listdir(self.backup_dir)
                if f.startswith(BACKUP_PREFIX) and f.endswith(BACKUP_SUFFIX)
            ]
        except OSError as e:
            log.error(f"[备份] 列目录失败：{e}")
            return []

        # 按 mtime 降序（新→旧），保留前 keep 份
        files.sort(
            key=lambda f: os.path.getmtime(os.path.join(self.backup_dir, f)),
            reverse=True,
        )
        removed: List[str] = []
        for f in files[self.keep:]:
            if self._safe_remove(os.path.join(self.backup_dir, f)):
                removed.append(f)
        return removed

    @staticmethod
    def _safe_remove(path: str) -> bool:
        try:
            if os.path.exists(path):
                os.remove(path)
                return True
        except OSError as e:
            log.error(f"[备份] 删除失败 {path}: {e}")
        return False

    def list_backups(self) -> List[str]:
        """列出现有备份文件（新→旧），供运维/后续告警查询。"""
        try:
            files = [
                f for f in os.listdir(self.backup_dir)
                if f.startswith(BACKUP_PREFIX) and f.endswith(BACKUP_SUFFIX)
            ]
        except OSError:
            return []
        files.sort(
            key=lambda f: os.path.getmtime(os.path.join(self.backup_dir, f)),
            reverse=True,
        )
        return files

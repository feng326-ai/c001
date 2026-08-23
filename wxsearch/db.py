"""SQLite 存储模块：负责建表、去重入库、查询统计。

去重策略：优先用文章真实 URL 作为唯一指纹（最可靠）；
若未取到 URL，则回退到 (title + account) 内容指纹。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class Article:
    """一条公众号文章采集结果。"""

    keyword: str
    title: str
    account: str = ""
    publish_time: str = ""
    summary: str = ""
    content: str = ""
    url: str = ""
    source: str = "wechat_pc"

    def fingerprint(self) -> str:
        """生成去重指纹：有真实链接用链接，否则用 标题+公众号。"""
        if self.url.startswith("http"):
            raw = self.url.strip()
        else:
            raw = f"{self.title.strip()}|{self.account.strip()}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT    NOT NULL UNIQUE,
    keyword      TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    account      TEXT    DEFAULT '',
    publish_time TEXT    DEFAULT '',
    summary      TEXT    DEFAULT '',
    content      TEXT    DEFAULT '',
    url          TEXT    DEFAULT '',
    source       TEXT    DEFAULT 'wechat_pc',
    collected_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keyword ON articles(keyword);
CREATE INDEX IF NOT EXISTS idx_account ON articles(account);
"""


class Database:
    """本地 SQLite 落库实现。

    ``dedup`` 目前只用于保持与分布式 Sink 一致的构造契约；本地库继续使用
    URL（或标题+公众号）唯一指纹做精确去重。无论走哪种落库实现，调用方都
    可以通过 ``last_reason`` 判断本次保存结果。
    """

    def __init__(self, path: str, dedup: Any = None):
        self.dedup = dedup
        self.last_reason = ""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """兼容旧库：缺 content 列时补加。"""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(articles)")}
        if "content" not in cols:
            self.conn.execute("ALTER TABLE articles ADD COLUMN content TEXT DEFAULT ''")

    def save(self, article: Article) -> bool:
        """保存一条记录。返回 True 表示新增，False 表示已存在（去重跳过）。"""
        self.last_reason = ""
        fp = article.fingerprint()
        try:
            self.conn.execute(
                """
                INSERT INTO articles
                    (fingerprint, keyword, title, account, publish_time, summary, content, url, source, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fp,
                    article.keyword,
                    article.title,
                    article.account,
                    article.publish_time,
                    article.summary,
                    article.content,
                    article.url,
                    article.source,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            self.conn.commit()
            self.last_reason = "new"
            return True
        except sqlite3.IntegrityError:
            self.last_reason = "exact_duplicate"
            return False

    def count(self, keyword: Optional[str] = None) -> int:
        if keyword:
            cur = self.conn.execute("SELECT COUNT(*) FROM articles WHERE keyword = ?", (keyword,))
        else:
            cur = self.conn.execute("SELECT COUNT(*) FROM articles")
        return int(cur.fetchone()[0])

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

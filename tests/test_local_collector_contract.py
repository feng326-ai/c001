"""Windows 单机采集 v2 的本地落库契约。"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

try:
    import pyperclip  # noqa: F401
except ModuleNotFoundError:
    sys.modules["pyperclip"] = MagicMock()

try:
    import uiautomation  # noqa: F401
except ModuleNotFoundError:
    sys.modules["uiautomation"] = MagicMock()

from wxsearch import collector as collector_module
from wxsearch.ai_filters.rule_filter import RuleBasedFilter
from wxsearch.db import Article, Database


def test_local_database_matches_sink_contract(tmp_path):
    db = Database(str(tmp_path / "canary.db"), SimpleNamespace(mode="smart"))
    article = Article(keyword="测试", title="同一篇文章", account="测试公众号")

    assert db.save(article) is True
    assert db.last_reason == "new"
    assert db.save(article) is False
    assert db.last_reason == "exact_duplicate"
    assert db.count() == 1
    db.close()


def test_collector_can_start_with_distributed_disabled(tmp_path, monkeypatch):
    class DummyDriver:
        def __init__(self, _config, _logger):
            pass

    monkeypatch.setattr(collector_module, "WeChatSearchDriver", DummyDriver)
    monkeypatch.setattr(collector_module, "RuleBasedFilter", lambda _config: object())

    config = SimpleNamespace(
        distributed=SimpleNamespace(enabled=False),
        unattended=SimpleNamespace(channel="souyisou"),
        channel="souyisou",
        db_path=str(tmp_path / "collector.db"),
        dedup=SimpleNamespace(mode="smart"),
    )
    collector = collector_module.Collector(config, logging.getLogger("collector-contract"))

    assert isinstance(collector.db, Database)
    collector.db.close()


def test_rule_filter_accepts_lightweight_pc_article_without_account_id():
    article = Article(
        keyword="测试",
        title="正常行业文章",
        content="有效正文" * 80,
        publish_time="2026年8月23日 12:00",
        url="https://mp.weixin.qq.com/s/example",
    )

    assert RuleBasedFilter().filter(article) == (True, "pass")


def test_unattended_blocked_generic_keyword_never_opens_search():
    collector = object.__new__(collector_module.Collector)
    collector.log = logging.getLogger("blocked-keyword")
    collector._open_and_filter = lambda _keyword: (_ for _ in ()).throw(
        AssertionError("blocked keyword must not open search")
    )

    assert collector.collect_keyword("全国 先进 推荐") == 0

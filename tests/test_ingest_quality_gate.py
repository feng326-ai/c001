"""实时采集入口质量门禁回归。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from wxsearch.db import Article as CollectorArticle
from wxsearch.distributed_sink import DistributedSink
from wxsearch.ingest_quality import evaluate_article, evaluate_keyword
from wxsearch.models import Article


NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _quality_gate_enabled(monkeypatch):
    monkeypatch.setenv("INGEST_QUALITY_GATE_ENABLED", "true")


def _article(**overrides):
    values = {
        "title": "关于开展2026年度品牌评选活动的通知",
        "content": "主办单位现面向全市企业征集报名，参评单位请在截止日前提交报名表。" * 8,
        "url": "https://mp.weixin.qq.com/s?__biz=test&mid=1&idx=1",
        "source_channel": "wechat_pc",
        "keyword": "评选征集",
        "account": "活动组委会",
        "publish_time": (NOW - timedelta(hours=4)).isoformat(),
    }
    values.update(overrides)
    return Article(**values)


def test_realtime_business_notice_is_accepted():
    decision = evaluate_article(_article(), now=NOW)
    assert decision.accepted is True


def test_common_recommendation_words_do_not_count_as_business_intent():
    article = _article(
        title="关于全国先进个人推荐工作的通知",
        content="请各单位积极参与推荐，按要求报送先进个人材料。" * 10,
        keyword="全国 先进 推荐",
    )
    decision = evaluate_article(article, now=NOW)
    assert decision.accepted is False
    assert decision.reason == "blocked_generic_keyword"


def test_explicitly_blocked_generic_keywords_are_rejected_before_search():
    for keyword in ("全国", "先进", "推荐", "全国 先进 推荐", " 全国   推荐 "):
        decision = evaluate_keyword(keyword)
        assert decision.accepted is False
        assert decision.reason == "blocked_generic_keyword"

    assert evaluate_keyword("推荐申报").accepted is True
    assert evaluate_keyword("先进典型评选").accepted is True


def test_realtime_requires_parseable_and_fresh_publish_time():
    missing = evaluate_article(_article(publish_time=""), now=NOW)
    stale = evaluate_article(
        _article(publish_time=(NOW - timedelta(days=5)).isoformat()), now=NOW
    )
    assert missing.reason == "missing_or_invalid_publish_time"
    assert stale.reason.startswith("rule_filter:") or stale.reason == "stale_realtime_article"


def test_historical_backfill_keeps_relevant_old_evidence():
    article = _article(publish_time=(NOW - timedelta(days=1095)).isoformat())
    decision = evaluate_article(article, mode="historical_backfill", now=NOW)
    assert decision.accepted is True


def test_known_noise_topic_is_rejected_without_direct_online_vote():
    article = _article(
        title="城市扑克赛事报名通知",
        content="本次扑克比赛由俱乐部主办，参赛选手请扫码报名。" * 10,
        keyword="全国 赛事 报名",
    )
    decision = evaluate_article(article, now=NOW)
    assert decision.accepted is False
    assert decision.reason == "known_noise_topic"


def test_known_noise_topic_with_explicit_online_vote_is_kept_for_llm():
    article = _article(
        title="扑克形象大使网络投票开启",
        content="主办方已开启网络投票，请通过投票通道参与候选人票选。" * 10,
    )
    decision = evaluate_article(article, now=NOW)
    assert decision.accepted is True


def test_rule_filter_exception_fails_closed():
    class BrokenFilter:
        def filter(self, article):
            raise RuntimeError("boom")

    decision = evaluate_article(_article(), now=NOW, rule_filter=BrokenFilter())
    assert decision.accepted is False
    assert decision.reason == "rule_filter_error"


def test_semantic_rollback_does_not_disable_rule_filter(monkeypatch):
    class BrokenFilter:
        def filter(self, article):
            raise RuntimeError("boom")

    monkeypatch.setenv("INGEST_QUALITY_GATE_ENABLED", "false")
    decision = evaluate_article(_article(), now=NOW, rule_filter=BrokenFilter())
    assert decision.accepted is False
    assert decision.reason == "rule_filter_error"


def test_distributed_payload_declares_realtime_mode():
    collector_article = CollectorArticle(
        keyword="评选征集",
        title="评选活动报名通知",
        content="活动主办方正在征集报名。" * 10,
        publish_time=(NOW - timedelta(hours=4)).isoformat(),
    )
    payload = json.loads(DistributedSink._to_payload(collector_article))
    assert payload["_ingest_meta"] == {
        "schema": "wxsearch.ingest/1",
        "collection_mode": "realtime_signal",
    }


def test_single_and_batch_tasks_reject_before_opening_database():
    from wxsearch.tasks import process_article_task, process_batch_articles

    bad_payload = json.dumps(
        _article(
            title="全国先进人物推荐材料",
            content="请积极参与推荐并报送人物事迹。" * 10,
            keyword="全国 先进 推荐",
        ).__dict__,
        ensure_ascii=False,
        default=str,
    )
    single = process_article_task.run(bad_payload)
    batch = process_batch_articles.run([bad_payload, "not-json"])

    assert single["success"] is False
    assert single["reason"] == "quality_rejected:blocked_generic_keyword"
    assert batch["accepted"] == 0
    assert batch["rejected"] == 2
    assert batch["new"] == 0

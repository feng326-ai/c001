"""采集入口质量门禁。

这层只负责在入库和模型调用之前拦截明显噪声，不承担最终商机判断。
最终业务可见性仍由 LLM 质量状态决定（见 D-038/D-039）。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from wxsearch.ai_filters.rule_filter import RuleBasedFilter


REALTIME_MODE = "realtime_signal"
HISTORICAL_MODE = "historical_backfill"
ALLOWED_MODES = {REALTIME_MODE, HISTORICAL_MODE}

# 业务已明确停用的宽泛词。只拦精确词或完全由这些词组成的组合，避免误伤
# “推荐申报”“先进典型评选”等带明确业务意图的长词。
BLOCKED_GENERIC_KEYWORDS = frozenset({"全国", "先进", "推荐"})


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AdmissionDecision:
    accepted: bool
    reason: str


def evaluate_keyword(keyword: str) -> AdmissionDecision:
    """在打开搜一搜之前拒绝业务已停用的宽泛关键词。"""
    normalized = " ".join(str(keyword or "").split())
    if not normalized:
        return AdmissionDecision(False, "missing_keyword")
    tokens = normalized.split(" ")
    if normalized in BLOCKED_GENERIC_KEYWORDS or (
        len(tokens) > 1 and all(token in BLOCKED_GENERIC_KEYWORDS for token in tokens)
    ):
        return AdmissionDecision(False, "blocked_generic_keyword")
    return AdmissionDecision(True, "keyword_allowed")


# “推荐、全国、先进、参与”等普通词故意不在意图词内；单独出现不能证明存在评选业务。
INTENT_SIGNALS = (
    "评选", "评比", "评优", "推选", "海选", "选拔", "评奖",
    "投票", "票选", "打榜", "征集", "征稿", "征文", "报名", "申报",
    "遴选", "大赛", "比赛", "竞赛", "赛事", "展评", "展演",
)

ACTION_SIGNALS = (
    "报名", "申报", "参与", "参评", "征集", "候选", "启动", "开启",
    "开始", "截止", "通知", "公告", "方案", "办法", "主办", "承办",
    "组委会", "联系电话", "咨询电话", "二维码", "通道", "入口", "链接",
    "报名表", "提交作品", "组织开展", "关于开展", "举办",
)

STRONG_PHRASES = (
    "评选活动", "投票活动", "网络评选", "网络投票", "线上投票", "微信投票",
    "评选通知", "评选公告", "评选方案", "评选办法", "评选申报", "评选征集",
    "征集评选", "征集公告", "申报通知", "报名通知", "推荐申报", "候选征集",
    "投票通道", "投票入口", "投票链接", "投票开启", "投票启动", "正在投票",
)

# 已确认会制造大量无关结果的内容类型。若文章明确存在网络/线上投票，则保留给 LLM 终审。
NOISE_TITLE_PATTERNS = (
    "干部任前公示", "人事任免", "先进事迹", "人物回忆", "人物追忆",
    "扑克", "棋牌", "麻将", "彩票开奖", "娱乐新闻",
)

DIRECT_VOTING_SIGNALS = (
    "网络投票", "线上投票", "微信投票", "投票通道", "投票入口", "正在投票",
)


def _parse_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
        "%Y/%m/%d %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _age_hours(published: datetime, now: datetime) -> float:
    if published.tzinfo is not None:
        reference = now.astimezone(published.tzinfo) if now.tzinfo else now.replace(tzinfo=published.tzinfo)
    else:
        reference = now.replace(tzinfo=None) if now.tzinfo else now
    return (reference - published).total_seconds() / 3600.0


def _rule_filter_for(mode: str, max_age_hours: int) -> RuleBasedFilter:
    days = 1461 if mode == HISTORICAL_MODE else max(1, math.ceil(max_age_hours / 24))
    return RuleBasedFilter(SimpleNamespace(valid_days_limit=days))


def evaluate_article(
    article,
    *,
    mode: str = REALTIME_MODE,
    now: Optional[datetime] = None,
    max_age_hours: Optional[int] = None,
    rule_filter: Optional[RuleBasedFilter] = None,
) -> AdmissionDecision:
    """返回文章是否允许进入原文库。

    `historical_backfill` 仅放宽时效；业务意图和低质门禁仍然生效。
    """
    if mode not in ALLOWED_MODES:
        return AdmissionDecision(False, "invalid_collection_mode")

    title = str(getattr(article, "title", "") or "").strip()
    content = str(getattr(article, "content", "") or "").strip()
    keyword = str(getattr(article, "keyword", "") or "").strip()
    if not title:
        return AdmissionDecision(False, "missing_title")
    if not keyword:
        return AdmissionDecision(False, "missing_keyword")
    keyword_decision = evaluate_keyword(keyword)
    if not keyword_decision.accepted:
        return keyword_decision

    try:
        configured_age = int(
            max_age_hours
            if max_age_hours is not None
            else os.getenv("INGEST_REALTIME_MAX_AGE_HOURS", "72")
        )
    except (TypeError, ValueError):
        return AdmissionDecision(False, "invalid_max_age_config")
    if configured_age <= 0:
        return AdmissionDecision(False, "invalid_max_age_config")

    generic_filter = rule_filter or _rule_filter_for(mode, configured_age)
    try:
        generic_ok, generic_reason = generic_filter.filter(article)
    except Exception:  # noqa: BLE001 - 门禁自身异常必须 fail closed
        return AdmissionDecision(False, "rule_filter_error")
    if not generic_ok:
        return AdmissionDecision(False, f"rule_filter:{generic_reason}")

    published = _parse_datetime(getattr(article, "publish_time", None))
    if mode == REALTIME_MODE:
        if published is None:
            return AdmissionDecision(False, "missing_or_invalid_publish_time")
        hours = _age_hours(published, now or datetime.now().astimezone())
        if hours < -12:
            return AdmissionDecision(False, "publish_time_in_future")
        if hours > configured_age:
            return AdmissionDecision(False, "stale_realtime_article")

    # 紧急回滚只关闭新增的语义判断；字段、低质规则和实时发布时间门禁始终
    # fail closed，不能借回滚开关恢复旧的“过滤异常即放行”。
    if not _truthy(os.getenv("INGEST_QUALITY_GATE_ENABLED", "true")):
        return AdmissionDecision(True, "semantic_gate_disabled")

    plain_content = re.sub(r"<[^>]+>", " ", content)
    text = f"{title}\n{plain_content}"

    if any(word in title for word in NOISE_TITLE_PATTERNS) and not any(
        word in text for word in DIRECT_VOTING_SIGNALS
    ):
        return AdmissionDecision(False, "known_noise_topic")

    if any(phrase in text for phrase in STRONG_PHRASES):
        return AdmissionDecision(True, "strong_business_phrase")

    intent_hits = [word for word in INTENT_SIGNALS if word in text]
    if not intent_hits:
        return AdmissionDecision(False, "missing_business_intent")
    action_hits = [word for word in ACTION_SIGNALS if word in text]
    if not action_hits:
        return AdmissionDecision(False, "missing_business_action")

    return AdmissionDecision(True, "intent_and_action")

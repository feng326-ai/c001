"""规则评分器（AIAnalyzer 的 rule 后端）——离线、免费、无外呼。

定位：文章通过三层去重成功入库后，用纯规则给正文/标题打分，判定它是否是一条
「评选 / 投票 / 征集 / 活动」类的高价值线索，并产出与 articles_core 的 AI 列一一
对应的结构（意图分类 / 价值评分 / 优先级 / 线索判定 / 评分明细 / 理由）。

设计原则：
  - 纯 Python、零外部依赖、零成本，任何输入都不抛异常（AIAnalyzer 再兜一层）；
  - 评分完全透明：每个信号的得分写进 scoring_breakdown，事后可解释「为什么这篇是 P0」；
  - 与 LLM 后端同构（都返回 AIResult），将来切 LLM 时上层无需改动。

两维正交口径（方向2：优先级与资源等级彻底解耦）：
  - 线索相关性 relevance（决定 is_lead）：意图基础分 + 行动信号 + 内容丰富度
    + 标题命中 - 广告惩罚；意图属 campaign 类且 relevance >= LEAD_MIN_SCORE 即线索。
  - 优先级 priority_score（“先看哪条”，仅时效紧迫度）：发布时效分 + 报名截止临近分，
    分级 >=urgency_p0 → P0，>=urgency_p1 → P1，其余 P2。与规模无关。
  - 资源等级 resource_level（“活动大不大”，仅规模/权威）：命中不同规模信号分组数达标
    → excellent，否则 normal。与时间无关。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from wxsearch.ai_filters.ai_analyzer import AIResult

log = logging.getLogger(__name__)

# 规则配置文件路径（可被环境变量 RULE_CONFIG_PATH 覆盖）。
_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "rule_config.json"
)


def load_rule_config(path: Optional[str] = None) -> dict:
    """读取规则配置 JSON。读不到 / 解析失败一律返回空 dict（RuleScorer 用内置默认兜底），
    绝不因配置问题阻断评分。"""
    p = path or os.getenv("RULE_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"规则配置读取失败（用内置默认）：{exc}")
        return {}


class RuleScorer:
    """基于规则的离线线索评分器。"""

    # 具体意图关键词（优先判定，命中一个即归入）。命中标题额外加权。
    # 顺序即优先级（评选 > 投票 > 征集），平局时取声明在前者。
    SPECIFIC_INTENTS = {
        "评选": ["评选", "评比", "评优", "推选", "海选", "选拔", "评奖", "评审", "评定"],
        "投票": ["投票", "票选", "打榜", "网络投票", "人气榜", "点赞排名"],
        "征集": ["征集", "征稿", "征文", "招募", "报名", "申报", "遴选", "候选人推荐"],
    }
    
    # 赛事类“活动”关键词（仅当具体意图都未命中时才作为兜底）。
    # 不收录裸“活动”二字：它太泛，会把大量普通报道误判为线索。
    EVENT_KEYWORDS = ["大赛", "比赛", "竞赛", "赛事", "展评", "展演"]

    # 属于「有商机价值」的意图类别（其余归为资讯/其他，不判定为线索）。
    LEAD_INTENTS = {"评选", "投票", "征集", "活动"}

    # 行动信号：越多越像一则真实、可参与的活动通知（而非泛泛报道）。
    ACTION_SIGNALS = [
        "报名", "参与", "扫码", "截止", "详情", "主办", "承办", "组委会",
        "咨询", "联系电话", "报名表", "参评", "申报", "附件", "二维码", "报名方式",
    ]

    # 广告/营销负面信号：命中则大幅扣分（这些通常是推广而非线索）。
    ADVERT_KEYWORDS = [
        "广告投放", "品牌推广", "招商加盟", "代理加盟", "限时优惠", "会员充值",
        "刷单返利", "投资理财", "贷款", "信用卡套现", "砍一刀", "免费领取",
    ]

    # 核心公文/评选文书词：命中说明是正式活动通知，相关性基分抬到 50。
    CORE_DOC_WORDS = ["方案", "通知", "申报", "办法", "实施方案", "征集公告", "评选办法", "评选方案"]

    # 非业务征集（纯政务/公益，无投票商机）：命中 -35 并判非线索。
    # 注意：征文/摄影/征稿【不在此列】——这类常带网络投票，是核心商机，保留为候选交 LLM。
    NON_BUSINESS_COLLECT = [
        "意见征集", "提案征集", "线索征集", "志愿者征集", "志愿服务征集", "志愿者招募",
        "民意征集", "建议征集", "问题线索",
    ]

    # 高价值筹备特征（红头文件/方案印发/申报启动）：命中 +25，是规则层给 P1 的强信号。
    PREP_SIGNALS_DEFAULT = [
        "印发", "的通知", "申报指南", "实施方案", "现将申报", "关于开展", "组织开展", "评选办法",
    ]

    # 分级阈值
    P0_MIN = 70.0
    P1_MIN = 45.0
    LEAD_MIN_SCORE = 45.0

    def __init__(self, config: Optional[dict] = None):
        """可传入配置 dict（缺省自动读 rule_config.json）。配置里的阈值/规模信号
        会覆盖内置默认，便于人工调参与反馈微调模块写回。

        两维正交（方向2）：
          - 优先级 priority = 时效紧迫度（越新 / 越临近报名截止越靠前），与规模无关；
          - 资源等级 resource_level = 规模价值（大型 / 权威活动），与时间无关。
        """
        cfg = config if config is not None else load_rule_config()
        self.config = cfg or {}
        th = self.config.get("thresholds", {})
        # 线索相关性门槛（决定 is_lead，与时间/规模无关）
        self.lead_min_score = float(th.get("lead_min_score", self.LEAD_MIN_SCORE))
        # 负向黑名单（人在回路·阶段B）：标题或正文命中任一词 → 硬拒，不晋级、不进 LLM。
        self._negative_keywords = [w for w in (self.config.get("negative_keywords") or []) if w]

        # ---- 维度一：资源等级（规模/价值，与时间无关）----
        rl = self.config.get("resource_level", {})
        self._signal_groups = rl.get("signal_groups", {}) or {}
        self._enabled_groups = rl.get("enabled_signal_groups", []) or []
        self._min_groups = int(rl.get("large_event_min_groups", 2))

        # ---- 维度二：优先级 = 时效紧迫度（与规模无关）----
        # 重构：时效不再主导（禁止“新鲜”直送 P0）。新分桶：近3天15/近7天10/近15天5/>15天0。
        pr = self.config.get("priority", {})
        rs = pr.get("recency_scores", {}) or {}
        self._recency_scores = {
            "d3": float(rs.get("d3", 15)),
            "d7": float(rs.get("d7", 10)),
            "d15": float(rs.get("d15", 5)),
            "over": float(rs.get("over", 0)),
            "unknown": float(rs.get("unknown", 5)),
        }
        # 真临期信号（剔除“报名方式/报名须知/报名时间”这类常态词，只留真截止）。
        self._deadline_signals = pr.get("deadline_signals", []) or [
            "截止", "报名截止", "申报截止", "即将截止", "倒计时", "最后"]
        self._deadline_per_hit = float(pr.get("deadline_per_hit", 10))
        self._deadline_cap = float(pr.get("deadline_cap", 20))
        # 筹备特征信号 +25（可配）
        self._prep_signals = pr.get("prep_signals", []) or list(self.PREP_SIGNALS_DEFAULT)
        self._prep_bonus = float(pr.get("prep_bonus", 25))
        # 已结束标题域信号（仅标题/首段命中降权），从独立配置段读，不混入全文黑名单。
        ets = self.config.get("ended_title_signals", {}) or {}
        self._ended_title_signals = ets.get("signals", []) if isinstance(ets, dict) else list(ets)
        self._ended_penalty = float((ets.get("penalty", -40) if isinstance(ets, dict) else -40))
        # 新阈值：P0≥70 / P1≥45 / P2<45（规则层默认保守，P0 由 LLM 提拔）。
        self._urgency_p0 = float(pr.get("urgency_p0_min", 70))
        self._urgency_p1 = float(pr.get("urgency_p1_min", 45))

    def _judge_resource_level(self, text: str, is_lead: bool):
        """资源等级判定（规模/价值维度，与时间无关）：基础条件是 is_lead。
        在此基础上，命中 enabled_signal_groups 中不同规模分组数 >= large_event_min_groups
        → excellent(优)，否则 normal(普通)。非线索一律 normal。
        返回 (resource_level, matched_groups)。"""
        if not is_lead:
            return "normal", []
        matched = []
        for group in self._enabled_groups:
            words = self._signal_groups.get(group, [])
            if any(w and w in text for w in words):
                matched.append(group)
        level = "excellent" if len(matched) >= self._min_groups else "normal"
        return level, matched

    def score(self, article) -> AIResult:
        """给一篇文章打分。永不抛异常：任何意外都返回中性的“已分析”结果。"""
        try:
            return self._score_impl(article)
        except Exception as exc:  # noqa: BLE001
            log.warning(f"规则评分异常，返回中性结果：{exc}")
            return AIResult(analyzed=True, intent_category="其他",
                            reasoning=f"rule_error:{exc}",
                            scoring_breakdown={"method": "rule", "error": str(exc)})

    # ==================== 内部实现 ====================

    def _score_impl(self, article) -> AIResult:
        title = str(getattr(article, "title", "") or "")
        # 正文优先用已清洗的纯文本；回填场景可传 content_clean。
        content = str(getattr(article, "content_clean", "") or getattr(article, "content", "") or "")
        text = f"{title}\n{content}"

        # 1) 意图分类（标题命中权重 x2）
        intent_category, intent_hits = self._classify_intent(title, text)

        breakdown: dict = {"method": "rule"}

        # ===================================================================
        # 维度① 线索相关性 relevance：决定“是不是一条线索”（与时间/规模均无关）
        #   意图基分(命中核心公文词抗50) + 行动信号 + 内容丰富度 + 标题命中 - 广告/非业务征集惩罚
        # ===================================================================
        # 基分重构：线索意图且命中核心公文词(方案/通知/申报/办法) → 50；仅泛投票/泛活动 → 30。
        core_doc_hit = any(w in text for w in self.CORE_DOC_WORDS)
        if intent_category in self.LEAD_INTENTS:
            intent_base = 50 if core_doc_hit else {"评选": 40, "投票": 30, "征集": 40, "活动": 30}.get(intent_category, 30)
        else:
            intent_base = {"资讯": 8, "其他": 0}.get(intent_category, 0)
        matched_signals = [s for s in self.ACTION_SIGNALS if s in text]
        action_score = min(len(matched_signals), 6) * 5
        richness = 0
        if len(content) >= 300:
            richness += 8
        elif len(content) >= 120:
            richness += 4
        if str(getattr(article, "account", "") or ""):
            richness += 4
        title_bonus = 8 if (intent_category in self.LEAD_INTENTS and intent_hits.get("in_title")) else 0
        advert_hit = next((k for k in self.ADVERT_KEYWORDS if k in text), None)
        advert_penalty = -30 if advert_hit else 0
        # 非业务征集（意见/提案/志愿者…）：-35 且判非线索；征文/摄影/征稿不在此列，不打压。
        nonbiz_hit = next((w for w in self.NON_BUSINESS_COLLECT if w in text), None)
        nonbiz_penalty = -35 if nonbiz_hit else 0
        if nonbiz_hit:
            breakdown["non_business_collect"] = nonbiz_hit
        # 负向黑名单命中（人在回路·阶段B）：硬拒信号，直接判非线索。
        blacklist_hit = next((k for k in self._negative_keywords if k in text), None)
        if blacklist_hit:
            breakdown["blacklist_hit"] = blacklist_hit

        relevance = float(max(0, min(100, intent_base + action_score + richness + title_bonus + advert_penalty + nonbiz_penalty)))
        breakdown["relevance"] = {
            "intent_base": intent_base, "action_signals": action_score,
            "richness": richness, "title_bonus": title_bonus,
            "advert_penalty": advert_penalty, "non_business_penalty": nonbiz_penalty,
            "total": relevance,
        }

        is_lead = ((intent_category in self.LEAD_INTENTS) and (relevance >= self.lead_min_score)
                   and not advert_hit and not blacklist_hit and not nonbiz_hit)
        lead_type = intent_category if is_lead else None

        # ===================================================================
        # 维度② 优先级 priority = 时效紧迫度（低时效占比 + 筹备信号 + 真临期 - 已结束标题）
        #   回答“先看哪条”，与活动规模无关。规则层默认保守，P0 交 LLM 提拔。
        # ===================================================================
        head = f"{title}\n{content[:120]}"  # 标题 + 首段（已结束信号仅在此域判）
        priority_score, priority_level, urgency_bd = self._urgency(
            getattr(article, "publish_time", None), text, head)
        breakdown["urgency"] = urgency_bd

        # ===================================================================
        # 维度③ 资源等级 resource_level = 规模/价值（大型/权威活动）
        #   回答“这活动大不大”，与时间无关。
        # ===================================================================
        resource_level, matched_groups = self._judge_resource_level(text, is_lead)
        breakdown["resource_level"] = resource_level
        if matched_groups:
            breakdown["scale_groups"] = matched_groups

        reasoning = self._build_reasoning(
            intent_category, matched_signals, urgency_bd, advert_hit,
            priority_score, priority_level, is_lead, resource_level, matched_groups,
        )
        tags = ([intent_category] if intent_category != "其他" else []) + matched_signals[:6]

        return AIResult(
            analyzed=True,
            is_lead=is_lead,
            intent_category=intent_category,
            lead_type=lead_type,
            priority_score=priority_score,
            priority_level=priority_level,
            resource_level=resource_level,
            reasoning=reasoning,
            scoring_breakdown=breakdown,
            tags=tags,
        )

    def _classify_intent(self, title: str, text: str):
        """返回 (intent_category, {'in_title': bool})。

        先判具体意图（评选/投票/征集），取加权命中最高者（平局按声明顺序）；
        都未命中才看是否赛事类“活动”；再无 → 有正文归“资讯”，否则“其他”。
        命中计分：正文出现 +1，标题出现额外 +2（标题信号更强）。
        """
        def weigh(words):
            w, hit_title = 0, False
            for kw in words:
                if kw and kw in text:
                    w += 1
                if kw and kw in title:
                    w += 2
                    hit_title = True
            return w, hit_title

        best_cat, best_weight, in_title = None, 0, False
        for cat, words in self.SPECIFIC_INTENTS.items():
            weight, hit_title = weigh(words)
            if weight > best_weight:
                best_weight, best_cat, in_title = weight, cat, hit_title
        if best_cat is not None:
            return best_cat, {"in_title": in_title}

        event_weight, event_in_title = weigh(self.EVENT_KEYWORDS)
        if event_weight > 0:
            return "活动", {"in_title": event_in_title}

        return ("资讯" if len(text.strip()) >= 120 else "其他"), {"in_title": False}

    def _urgency(self, publish_time, text: str, head: str = ""):
        """维度② 优先级 = 时效紧迫度（重构：时效降权，不再主导）。

        组成：
          - 时效分 recency：近3天15/近7天10/近15天5/>15天0（未知给中性小分）；
          - 筹备特征分 prep：命中印发/的通知/申报指南/实施方案等强筹备信号 +prep_bonus(25)；
          - 真临期分 deadline：命中真截止词，每个 +per_hit，封顶 cap；
          - 已结束惩罚：标题/首段(head)命中已结束信号 → ended_penalty(-40)，打入 P2。
        针对性地让规则层 P0 极稀缺（需 近期+筹备词+真截止 才可能近 70），P0 由 LLM 提拔。
        返回 (priority_score, priority_level, breakdown)。
        """
        head = head or text
        # ---- 时效分（复用 store 的中文时间解析，支持 datetime 直通与中文串）----
        pub = None
        if publish_time is not None:
            try:
                from wxsearch.smart_dedup_store import parse_publish_time
                pub = parse_publish_time(publish_time)
            except Exception:  # noqa: BLE001
                pub = publish_time if isinstance(publish_time, datetime) else None
        if not isinstance(pub, datetime):
            recency, recency_note = self._recency_scores["unknown"], "时间未知"
        else:
            now = datetime.now(pub.tzinfo) if pub.tzinfo is not None else datetime.now()
            days = (now - pub).total_seconds() / 86400.0
            if days <= 3:
                recency, recency_note = self._recency_scores["d3"], "3天内"
            elif days <= 7:
                recency, recency_note = self._recency_scores["d7"], "7天内"
            elif days <= 15:
                recency, recency_note = self._recency_scores["d15"], "15天内"
            else:
                recency, recency_note = self._recency_scores["over"], "超15天"

        # ---- 筹备特征分 ----
        prep_hits = [s for s in self._prep_signals if s and s in text]
        prep_score = self._prep_bonus if prep_hits else 0.0

        # ---- 真临期分 ----
        deadline_hits = [s for s in self._deadline_signals if s and s in text]
        deadline_score = min(len(deadline_hits) * self._deadline_per_hit, self._deadline_cap)

        # ---- 已结束标题域惩罚（仅 head=标题+首段）----
        ended_hit = next((s for s in self._ended_title_signals if s and s in head), None)
        ended_penalty = self._ended_penalty if ended_hit else 0.0

        total = float(max(0.0, min(100.0, recency + prep_score + deadline_score + ended_penalty)))
        if total >= self._urgency_p0:
            level = "P0"
        elif total >= self._urgency_p1:
            level = "P1"
        else:
            level = "P2"

        breakdown = {
            "recency": recency, "recency_note": recency_note,
            "prep_hits": prep_hits[:6], "prep_score": prep_score,
            "deadline_hits": deadline_hits[:6], "deadline_score": deadline_score,
            "ended_hit": ended_hit, "ended_penalty": ended_penalty,
            "total": total, "level": level,
        }
        return total, level, breakdown

    def _build_reasoning(self, intent, signals, recency_note, advert_hit, score, level, is_lead,
                         resource_level="normal", matched_groups=None) -> str:
        parts = [f"意图={intent}", f"时效={recency_note}"]
        if signals:
            parts.append("行动信号:" + "/".join(signals[:6]))
        if advert_hit:
            parts.append(f"疑似推广({advert_hit})")
        if is_lead:
            rl_label = "优" if resource_level == "excellent" else "普通"
            if matched_groups:
                parts.append(f"资源等级={rl_label}(命中:{'/'.join(matched_groups)})")
            else:
                parts.append(f"资源等级={rl_label}")
        verdict = "判定为线索" if is_lead else "非线索"
        return f"[规则] {verdict}（{level}/{score:.0f}分）：" + "，".join(parts)


# ==================== 使用示例 / 自测 ====================

if __name__ == "__main__":
    from types import SimpleNamespace

    samples = [
        SimpleNamespace(
            title="关于开展2026年度“最美劳动者”评选活动的通知",
            content="为弘扬劳模精神，现面向全市征集并组织网络投票评选。请各单位于8月30日前"
                    "报名，扫码提交报名表，详情见附件，组委会联系电话……" * 3,
            account="市总工会", account_id="official_123456", publish_time="2026年8月14日 09:00",
            source_channel="wechat_pc", keyword="评选活动",
        ),
        SimpleNamespace(
            title="今日天气晴朗适合出行",
            content="本地今天多云转晴，气温适宜。" * 20,
            account="本地资讯", account_id="news_000001", publish_time="2026-08-14 08:00",
            source_channel="wechat_pc", keyword="天气",
        ),
    ]
    scorer = RuleScorer()
    for s in samples:
        r = scorer.score(s)
        print(f"\n标题：{s.title}")
        print(f"  → {r.to_dict()}")

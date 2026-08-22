"""
AI 智能分析层（第二部分：LLM 价值评估）——可开关，默认关闭（预留跳过）。

定位：文章通过三层去重成功入库后，对正文做价值评分 / 意图分类 / 线索识别，
决定是否升级为 qualified_leads。本模块只提供**开关框架与接口契约**，真正的
LLM 调用后续填充（见 _analyze_impl 的 TODO）。

开关来源（worker 端跑在容器里，读环境变量最自然）：
  - AI_ENABLED   : "1/true/yes/on" 视为开启，其余（含缺省）为关闭。
  - AI_BACKEND   : 评分后端 rule（默认，离线规则、免费）/ llm（预留占位）/ stub（中性）。
  - AI_PROVIDER  : 预留，如 openai / qwen / local（默认 openai，仅 llm 后端用）。
  - AI_MODEL     : 预留模型名（默认 gpt-4o-mini，仅 llm 后端用）。
  - OPENAI_API_KEY: 预留密钥（真正接 LLM 时使用）。

设计原则：
  - 关闭时 analyze() 立即返回 analyzed=False 的“跳过”结果，零外部依赖、零成本，
    调用方据此保持既有行为不变；
  - 开启 + rule 后端：走离线规则评分器（免费、无外呼），产出真实评分/意图/线索判定；
  - 开启 + llm 后端：走占位实现（返回中性默认值并告警一次），等真正接入 LLM 时只改
    _score_by_llm 一处；
  - 无论哪种后端，任何异常都降级为“未分析”，绝不阻断入库主流程。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)


def _truthy(val: Optional[str]) -> bool:
    """把环境变量字符串解析为布尔（缺省/无法识别一律 False）。"""
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class AIResult:
    """AI 分析结果。analyzed=False 表示本次未真正分析（AI 关闭或跳过）。

    字段与 articles_core 的 AI 列一一对应，便于直接落库：
      intent_category / has_lead_value(=is_lead) / lead_type / priority_score /
      priority_level / scoring_breakdown / llm_reasoning(=reasoning)。
    """

    analyzed: bool = False                       # 是否实际跑了 AI 分析
    is_lead: bool = False                        # 是否判定为高价值线索 → has_lead_value
    intent_category: str = "其他"                # 评选/投票/征集/活动/资讯/其他
    lead_type: Optional[str] = None              # 命中线索时的类型（= intent_category），否则 None
    priority_score: float = 0.0                  # 价值评分 0~100
    priority_level: str = "P2"                   # P0/P1/P2
    resource_level: str = "normal"               # 资源等级 excellent(优)/normal(普通)
    reasoning: str = ""                          # 判定理由（供人工复核）→ llm_reasoning
    scoring_breakdown: dict = field(default_factory=dict)  # 各信号得分明细 → scoring_breakdown(jsonb)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "analyzed": self.analyzed,
            "is_lead": self.is_lead,
            "intent_category": self.intent_category,
            "lead_type": self.lead_type,
            "priority_score": self.priority_score,
            "priority_level": self.priority_level,
            "resource_level": self.resource_level,
            "reasoning": self.reasoning,
            "scoring_breakdown": dict(self.scoring_breakdown),
            "tags": list(self.tags),
        }


class AIAnalyzer:
    """AI 分析层。可开关：关闭时所有文章走“跳过”；开启时按 backend 分发。"""

    def __init__(self, enabled: bool = False, backend: str = "rule",
                 provider: str = "openai", model: str = "gpt-4o-mini",
                 api_key: Optional[str] = None):
        self.enabled = bool(enabled)
        self.backend = (backend or "rule").strip().lower()
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self._warned = False  # 占位实现只告警一次，避免刷屏
        self._scorer = None   # rule 后端惰性初始化
        if self.enabled:
            log.info(f"🧠 AI 分析层：已开启（backend={self.backend}）")
        else:
            log.info("🧠 AI 分析层：已关闭（文章仅去重入库，不做价值评估）")

    @classmethod
    def from_env(cls) -> "AIAnalyzer":
        """从环境变量构造（worker 端默认入口）。AI_ENABLED 缺省即关闭；
        AI_BACKEND 缺省为 rule（离线免费，即便忘配也不会误花钱或外呼）。"""
        return cls(
            enabled=_truthy(os.getenv("AI_ENABLED")),
            backend=os.getenv("AI_BACKEND", "rule"),
            provider=os.getenv("AI_PROVIDER", "openai"),
            model=os.getenv("AI_MODEL", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY") or None,
        )

    def analyze(self, article) -> AIResult:
        """分析一篇文章。关闭时立即返回“跳过”结果；开启时按 backend 分发。

        永不抛异常：AI 属加分项，任何失败都降级为“未分析”，绝不阻断入库主流程。
        """
        if not self.enabled:
            return AIResult(analyzed=False, reasoning="ai_disabled")

        try:
            if self.backend == "rule":
                return self._score_by_rule(article)
            if self.backend == "llm":
                return self._score_by_llm(article)
            # 未知/stub 后端：中性结果，保证“开也能跑”
            return AIResult(analyzed=True, reasoning=f"ai_stub_backend:{self.backend}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"AI 分析异常，降级跳过：{exc}")
            return AIResult(analyzed=False, reasoning=f"ai_error:{exc}")

    # ---- rule 后端：离线规则评分器（免费、无外呼） ----
    def _score_by_rule(self, article) -> AIResult:
        if self._scorer is None:
            from wxsearch.ai_filters.rule_scorer import RuleScorer
            self._scorer = RuleScorer()
        return self._scorer.score(article)

    # ---- llm 后端：真正的 LLM 分析实现（通用 OpenAI 兼容后端） ----
    def _score_by_llm(self, article) -> AIResult:
        """调用大模型做价值判定 + 意图分类，产出与 RuleScorer 同构的 AIResult。

        真正的调用委托给 llm_analyzer（内部用 llm_client 走 OpenAI 兼容接口）。
        任何失败都不在此吞掉——交由上层 analyze() 的 try/except 统一降级为「未分析」，
        从而保持主流程行为不变（既不误判也不阻断入库）。
        """
        from wxsearch.ai_filters.llm_analyzer import analyze as llm_analyze, to_ai_result

        title = str(getattr(article, "title", "") or "")
        content = str(getattr(article, "content_clean", "") or getattr(article, "content", "") or "")
        publish_time = getattr(article, "publish_time", None)
        data = llm_analyze(title, content, publish_time)
        return to_ai_result(data)

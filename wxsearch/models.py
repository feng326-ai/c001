"""
统一数据模型定义
所有采集渠道共用这些数据结构
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


@dataclass
class Article:
    """文章对象 (所有渠道共用)"""
    
    # 必需字段
    title: str                              # 标题
    content: str                            # 正文 (HTML/纯文本)
    url: str                                # 原始 URL
    
    # 来源信息
    source_channel: str                     # wechat_pc / sogou_wap / baidu_news
    keyword: str                            # 采集关键词
    
    # 公众号信息 (可选)
    account: Optional[str] = None           # 公众号名
    account_id: Optional[str] = None        # __biz
    mid: Optional[str] = None               # 文章 ID
    idx: Optional[str] = None               # 序号
    sn: Optional[str] = None                # 签名
    
    # 时间信息
    publish_time: Optional[str] = None      # 发布时间字符串
    collected_at: Optional[str] = None      # 采集(抓取)时刻 ISO 串——由采集端在抓到时置，区别于入库时刻
    created_at: datetime = field(default_factory=datetime.now)
    
    # 自动生成摘要
    summary: Optional[str] = field(default=None)
    
    def __post_init__(self):
        if self.summary is None and self.content:
            # 提取前 500 字作为摘要
            import re
            text = re.sub(r'<[^>]+>', ' ', self.content)[:500]
            self.summary = text.strip()
        
        if not self.account:
            self.account = ""
        
        # 采集时刻：采集端未显式给时，用对象构造时刻（≈抓取时刻）兜底，落库真实采集时间
        if not self.collected_at:
            self.collected_at = datetime.now().isoformat()


@dataclass
class QualifiedLead:
    """合格线索对象 (通过 AI 筛选的高价值线索)"""
    
    # 必填字段
    title: str
    url: str
    account: str
    intent_category: str                   # purchase/tender/cooperation/news/other
    
    # 可选字段
    id: Optional[int] = None
    article_id: Optional[int] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    publish_time: Optional[str] = None
    lead_type: Optional[str] = None
    priority_score: float = 0.0
    priority_level: Optional[str] = None   # P0/P1/P2
    scoring_breakdown: Optional[dict] = None
    llm_reasoning: Optional[str] = None
    has_lead_value: bool = True
    status: str = "pending_followup"
    assigned_to: Optional[str] = None
    follow_up_deadline: Optional[str] = None
    source_channel: Optional[str] = None
    keyword: Optional[str] = None
    processed_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> dict:
        """转换为字典 (用于 API 推送/日志记录)"""
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "url": self.url,
            "account": self.account,
            "publish_time": self.publish_time,
            "intent_category": self.intent_category,
            "priority_score": self.priority_score,
            "priority_level": self.priority_level,
            "llm_reasoning": self.llm_reasoning,
            "source_channel": self.source_channel,
            "keyword": self.keyword,
            "processed_at": self.processed_at.isoformat(),
        }


@dataclass
class CollectTaskResult:
    """采集任务结果上报对象"""
    
    keyword: str
    channel: str
    vm_instance: str
    articles_count: int
    success: bool
    start_time: datetime
    end_time: Optional[datetime] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "keyword": self.keyword,
            "channel": self.channel,
            "vm_instance": self.vm_instance,
            "articles_count": self.articles_count,
            "success": self.success,
            "duration_seconds": (self.end_time - self.start_time).total_seconds() if self.end_time else None,
            "error_message": self.error_message
        }


@dataclass
class FeedbackRequest:
    """人工反馈请求"""
    
    lead_id: int
    was_relevant: bool
    correction: Optional[dict] = None     # {"category": "...", "mark_by": "..."}
    tags: Optional[List[str]] = None

"""
Smart Dedup Store - 智能去重存储层
三层去重策略：URL 指纹 → 内容哈希 → SimHash 相似度检测
支持多渠道并发写入
"""

import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Tuple, Optional, List
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import json

from wxsearch.content_hasher import ContentHasher
from wxsearch.ai_filters.event_extractor import fill_for_article

log = logging.getLogger(__name__)


# 采集层提取到的发布时间是**中文展示串**（如「2026年8月14日 00:00」「3天前」「昨天」），
# 而 articles_core.publish_time 是 timestamptz、source_date 是 date：
# 直接塞原串会让 PG 报 invalid input syntax 并使整条插入失败（表现为「新增 0 条」）。
# 故在入库前统一归一化为 datetime；解析不出来时返回 None（列可空），绝不因时间格式丢文章。
_REL_RE = re.compile(r"^(\d+)\s*(分钟|小时|天)前$")
_CN_DATE_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}):(\d{2}))?$")
_CN_MD_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s*(\d{1,2}):(\d{2}))?$")


def parse_publish_time(raw, now: Optional[datetime] = None) -> Optional[datetime]:
    """把采集到的中文发布时间串归一化为 datetime，无法识别时返回 None。

    覆盖 wechat_driver._TIME_RE 允许的全部形态：相对时间（N分钟/小时/天前）、
    昨天/前天/刚刚、YYYY年M月D日[ H:MM]、M月D日（无年份按当年）、YYYY-M-D[ H:MM]。
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw

    text = str(raw).strip()
    if not text:
        return None

    now = now or datetime.now()

    if text == "刚刚":
        return now
    if text == "昨天":
        return now - timedelta(days=1)
    if text == "前天":
        return now - timedelta(days=2)

    m = _REL_RE.match(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"分钟": timedelta(minutes=n), "小时": timedelta(hours=n), "天": timedelta(days=n)}[unit]
        return now - delta

    for pattern in (_CN_DATE_RE, _ISO_RE):
        m = pattern.match(text)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour = int(m.group(4)) if m.group(4) else 0
            minute = int(m.group(5)) if m.group(5) else 0
            try:
                return datetime(year, month, day, hour, minute)
            except ValueError:
                return None

    m = _CN_MD_RE.match(text)
    if m:
        try:
            return datetime(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None

    return None


class SmartDedupStore:
    """智能去重存储层 (所有渠道共用)"""
    
    # ==================== 渠道优先级配置 ====================
    
    SOURCE_PRIORITY = {
        "wechat_pc": 1,           # PC UIA 最优 (有 sn 可点击真链 + 正文最全)
        "weixin_mobile": 2,       # 手机搜一搜
        "sogou_weixin": 3,        # 搜狗微信(Playwright)
        "sogou_wap": 3,           # WAP 旧命名
        "baidu_news": 4,          # 新闻聚合兜底
    }

    # 正文短于此长度时不做内容哈希/SimHash 去重（对齐 SQLite 路径的 dedup.min_content_len）。
    # 空/极短正文的指纹彼此相同，若参与第②③层会把大量无关文章误判为重复并丢弃；
    # 此时仅依靠第①层 URL 指纹去重（公众号文章的真实身份），宁可重不可错杀。
    MIN_CONTENT_LEN = 30
    
    def __init__(self, db_config: dict):
        """
        初始化去重存储层
        
        Args:
            db_config: PostgreSQL 连接配置
        """
        
        self.db_config = db_config
        self.conn = None
        self.cur = None
        
        try:
            import psycopg2
            self.conn = psycopg2.connect(**db_config)
            self.cur = self.conn.cursor()
            
            print(f"✅ 智能去重服务已初始化：{db_config['database']}")
            
        except Exception as e:
            raise ConnectionError(f"数据库连接失败：{e}")
    
    def add_article(self, article) -> Tuple[bool, str]:
        """
        插入单篇文章 (自动去重)
        
        Args:
            article: Article 对象
        
        Returns:
            (success, reason)
            success=True: 新增成功
            reason: "new" / "url_duplicate" / "exact_duplicate" / "similar_duplicate"
        """
        
        # 步骤 1: 规范化 URL 指纹
        canonical_url, url_fp = self._normalize_url(article.url)
        
        # 步骤 2: 提取并计算内容指纹
        clean_text = ContentHasher.extract_text_from_source(
            article.content, 
            article.source_channel
        )
        
        content_hash = ContentHasher.generate_content_hash(clean_text)
        simhash = ContentHasher.generate_similarity_hash(clean_text)
        
        # 更新 Article 对象的指纹字段
        article.content_hash = content_hash
        article.simhash = simhash
        
        # ========== 三重去重检测 ==========
        
        # ① URL 指纹完全匹配
        existing_by_url = self._check_url_fingerprint_exists(url_fp)
        if existing_by_url:
            # 跨渠道/多词命中同一篇：并入渠道集合，并按优先级升级主记录（以搜一搜为主）。
            self._absorb_into_primary(existing_by_url[0], article, canonical_url)
            self._map_keyword_to_article(article.keyword, existing_by_url[0])
            return (False, f"url_duplicate(ID:{existing_by_url[0]}, channel:{existing_by_url[3]})")
        
        # 正文过短（常见于正文未取到）时跳过②③层，避免同为空串指纹而被成批误杀。
        content_too_short = len(clean_text) < self.MIN_CONTENT_LEN
        if content_too_short:
            log.warning(
                f"⚠️ 正文仅 {len(clean_text)} 字（<{self.MIN_CONTENT_LEN}），跳过内容去重仅凭 URL 指纹：{article.title[:30]}"
            )
        
        # ② 内容哈希完全匹配 (转载/镜像)
        existing_by_hash = None if content_too_short else self._check_content_hash_exists(content_hash)
        if existing_by_hash:
            cluster_id = existing_by_hash[0]
            
            # 记录镜像关系
            self._record_mirror(cluster_id, article.url, article.source_channel)
            
            # 同内容不同渠道/URL：把当前关键词归因到集群主文章，支撑「多词命中同一资源」统计
            try:
                self.cur.execute(
                    "SELECT primary_article_id FROM article_clusters WHERE id = %s", (cluster_id,)
                )
                _row = self.cur.fetchone()
                if _row:
                    # 并入渠道集合 + 按优先级升级主记录（与 URL 重复一致）
                    self._absorb_into_primary(_row[0], article, canonical_url)
                    self._map_keyword_to_article(article.keyword, _row[0])
            except Exception as _e:  # noqa: BLE001
                log.warning(f"内容重复归因关键词失败 (cluster_id={cluster_id})：{_e}")
            
            return (False, f"exact_duplicate(cluster_id:{cluster_id}, channel:{article.source_channel})")
        
        # ③ SimHash 近似匹配。
        # 阈值必须是 1.0（即汉明距离 0，指纹完全相同）：实测 194 篇真实语料两两比对显示，
        # 距离 3~6 这一段几乎不携带信号——距离恰好为 3 的配对里多数是主题完全无关的文章
        # （如《长征主题读后感征集》被判成《"点剧"模式投票》），而距离 4/6 反倒存在标题几乎
        # 相同的真转载。放宽到 <=3 的结果是「该拦的漏、不该拦的杀」，实测误杀率约 12%。
        # 收紧到 0 后：距离 0 的真转载照旧拦下，无关文章不再被丢；改写幅度大的转载仍会漏，
        # 但放宽阈值同样漏，故无损失。第①层 URL 指纹与第②层正文哈希不受影响，主力去重能力不变。
        similar_articles = [] if content_too_short else self._find_similar_content(simhash, threshold=1.0)
        
        if similar_articles:
            # 判断是否值得保留
            decision = self._should_keep_new_or_old(article, similar_articles[0])
            
            if not decision["keep_new"]:
                # 近似转载也算这个关键词命中了原资源
                self._map_keyword_to_article(article.keyword, similar_articles[0][0])
                # 带上正文字数：事后才能分清「真的是近似转载」还是「正文没取到导致指纹雷同」。
                return (
                    False,
                    f"similar_duplicate(id:{similar_articles[0][0]}, "
                    f"score:{decision['similarity']:.2%}, 正文{len(clean_text)}字)"
                )
            
            # 如果新文章更优，可以替换旧版本
            # TODO: 这里可以添加 UPDATE 逻辑
        
        # ========== 全部通过，执行插入 ==========
        
        # 中文时间串 → datetime（解析失败则留空，不阻断入库）
        published_at = parse_publish_time(article.publish_time)
        if article.publish_time and published_at is None:
            log.warning(f"⚠️ 发布时间无法解析，置空入库：{article.publish_time!r}")
        
        try:
            self.cur.execute("""
                INSERT INTO articles_core 
                (content_hash, url_fingerprint, simhash, title, summary, account, 
                 account_id, mid, idx, sn, publish_time, source_date, 
                 canonical_url, original_url, source_channel, keyword,
                 content, content_clean, keywords, source_channels,
                 collected_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()), NOW())
                RETURNING id
            """, (
                content_hash, url_fp, str(simhash),
                article.title, article.summary or "", 
                article.account or "",
                article.account_id, article.mid, article.idx, article.sn,
                published_at,
                published_at.date() if published_at else None,
                canonical_url, article.url,
                article.source_channel, article.keyword,
                # 正文必须落库：AI 评分/意图识别要读它，且指纹算法一旦调整需能按原文重算。
                # 此前只存指纹不存正文，导致「为什么这两篇被判近似」完全不可复查。
                article.content or "",
                clean_text,
                [article.keyword],
                article.source_channel,   # source_channels 初始=主渠道（后续跨渠道命中时并入）
                getattr(article, "collected_at", None),
            ))
            
            new_id = self.cur.fetchone()[0]
            self.conn.commit()
            
            # 如果是首次出现此内容的文章，创建集群（正文过短时不建，避免空串指纹占据集群）
            if not content_too_short:
                self._create_cluster(content_hash, new_id, article.source_channel)
            
            log.info(
                f"✅ 新增文章 ID:{new_id} ({article.source_channel}, 正文 {len(clean_text)} 字): "
                f"{article.title[:30]}"
            )
            
            # 新资源首次入库：记录关键词命中
            self._map_keyword_to_article(article.keyword, new_id)
            
            return (True, f"new(ID:{new_id})")
            
        except Exception as e:
            self.conn.rollback()
            log.error(f"❌ 插入失败：{e}")
            return (False, f"insert_error({str(e)})")
    
    def bulk_insert(self, articles: List) -> dict:
        """
        批量插入文章
        
        Args:
            articles: Article 对象列表
        
        Returns:
            {"total": N, "new": X, "exact_duplicate": Y, "similar_duplicate": Z, "errors": W}
        """
        
        stats = {
            "total": len(articles),
            "new": 0,
            "url_duplicate": 0,
            "exact_duplicate": 0,
            "similar_duplicate": 0,
            "errors": 0
        }
        
        for article in articles:
            try:
                success, reason = self.add_article(article)
                
                if success:
                    stats["new"] += 1
                elif "url_duplicate" in reason:
                    stats["url_duplicate"] += 1
                elif "exact_duplicate" in reason:
                    stats["exact_duplicate"] += 1
                elif "similar_duplicate" in reason:
                    stats["similar_duplicate"] += 1
                else:
                    stats["errors"] += 1
                    
            except Exception as e:
                log.error(f"批量插入异常：{e}")
                stats["errors"] += 1
        
        return stats
    
    def save_scoring(self, article_id: int, ai) -> bool:
        """把 AI 评分结果回写 articles_core 的 7 个字段。

        Args:
            article_id: articles_core.id（add_article 成功时 RETURNING 出来的）
            ai: AIResult（含 intent_category/is_lead/lead_type/priority_score/
                priority_level/scoring_breakdown/reasoning）

        评分属加分项：任何失败只 log + 回滚 + 返回 False，绝不阻断入库主流程。
        """
        try:
            self.cur.execute("""
                UPDATE articles_core
                SET intent_category = %s,
                    has_lead_value  = %s,
                    lead_type       = %s,
                    priority_score  = %s,
                    priority_level  = %s,
                    resource_level  = %s,
                    scoring_breakdown = %s,
                    llm_reasoning   = %s,
                    updated_at      = NOW()
                WHERE id = %s
            """, (
                ai.intent_category,
                bool(ai.is_lead),
                ai.lead_type,
                ai.priority_score,
                ai.priority_level,
                getattr(ai, "resource_level", "normal"),
                json.dumps(ai.scoring_breakdown, ensure_ascii=False),
                ai.reasoning,
                article_id,
            ))
            self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"回写 AI 评分失败 (id={article_id})：{e}")
            return False

    def promote_lead(self, article_id: int) -> bool:
        """把一条已判定为线索的文章「提升」进 qualified_leads 线索管理表。

        直接从 articles_core 拷贝该行（含刚回写的 AI 评分），交由看板做查看/标注/
        跟进。幂等：同一 article_id 已存在则更新评分字段，不存在才插入；url 为空转
        NULL 以避开 qualified_leads 的 UNIQUE(url) 约束。任何失败只 log + 回滚 +
        返回 False，绝不阻断入库主流程。
        """
        try:
            self.cur.execute("SELECT id FROM qualified_leads WHERE article_id = %s", (article_id,))
            if self.cur.fetchone():
                self.cur.execute("""
                    UPDATE qualified_leads q
                    SET intent_category   = a.intent_category,
                        has_lead_value    = a.has_lead_value,
                        lead_type         = a.lead_type,
                        priority_score    = a.priority_score,
                        priority_level    = a.priority_level,
                        resource_level    = a.resource_level,
                        scoring_breakdown = a.scoring_breakdown,
                        llm_reasoning     = a.llm_reasoning,
                        title             = a.title,
                        summary           = a.summary,
                        keyword           = a.keyword,
                        updated_at        = NOW()
                    FROM articles_core a
                    WHERE q.article_id = a.id AND a.id = %s
                """, (article_id,))
            else:
                self.cur.execute("""
                    INSERT INTO qualified_leads
                        (article_id, title, summary, content, url, account, publish_time,
                         source_channel, keyword, intent_category, has_lead_value, lead_type,
                         priority_score, priority_level, resource_level, scoring_breakdown, llm_reasoning,
                         collected_at, status, created_at, updated_at)
                    SELECT id, title, summary, content,
                           NULLIF(COALESCE(canonical_url, original_url), ''),
                           account, publish_time, source_channel, keyword,
                           intent_category, has_lead_value, lead_type,
                           priority_score, priority_level, resource_level, scoring_breakdown, llm_reasoning,
                           collected_at, 'pending_followup', NOW(), NOW()
                    FROM articles_core WHERE id = %s
                    ON CONFLICT (url) DO NOTHING
                """, (article_id,))
            self.conn.commit()
            # 后置调用：活动名称/信息 AI 初稿（只填空、不覆盖人工）
            try:
                fill_for_article(self.cur, article_id)
            except Exception as e:  # noqa: BLE001
                log.warning(f"fill_for_article(article_id={article_id}) 失败：{e}")
            return True
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"提升线索失败 (article_id={article_id})：{e}")
            return False

    # ==================== 后台 LLM 清洗流水线（异步、可开关、不阻断） ====================

    def fetch_pending_llm_leads(self, limit: int = 5) -> list:
        """按优先级捞一批待清洗线索（P0>P1>P2，同级内分高者优先）。

        只捞 llm_status='pending' 且 updated_by_human=FALSE 的行（人工动过的不管）。
        正文读原文列 content（完整全文，非截断的 content_clean）。
        返回 [(lead_id, article_id, title, content, publish_time), ...]。
        """
        try:
            self.cur.execute(
                """
                SELECT id, article_id, title, content, publish_time
                FROM qualified_leads
                WHERE llm_status = 'pending' AND updated_by_human = FALSE
                ORDER BY priority_level ASC, priority_score DESC NULLS LAST
                LIMIT %s
                """,
                (int(limit),),
            )
            return list(self.cur.fetchall())
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"拉取待清洗线索失败：{e}")
            return []

    def save_llm_enrichment(self, lead_id: int, article_id, ai, name: str, details: str,
                            is_online_voting=None, online_voting_url: str = "",
                            is_recurring=None, activity_category: str = "",
                            activity_region: str = "", recurrence: str = "",
                            activity_status: str = "", organizer_name: str = "",
                            organizer_region: str = "", voting_platform: str = "",
                            organizer_contact=None, voting_status: str = "",
                            recurrence_period: str = "", edition_no=None) -> bool:
        """把大模型清洗结果回写线索表，并标记 llm_status='done'。

        覆盖规则：
          - 人工编辑保护：WHERE updated_by_human=FALSE，人工动过的行不会被写；
          - 规则草稿可覆盖：event_name/event_details 直接用大模型值（更准）；
          - 空结果不抹旧值：LLM 返回空串时用 COALESCE(NULLIF(...)) 保留原值。
        评分字段同时镜像回 articles_core，避免重新提升时回退。失败只 log+回滚。
        """
        try:
            breakdown = json.dumps(getattr(ai, "scoring_breakdown", {}) or {}, ensure_ascii=False)
            resource_level = getattr(ai, "resource_level", "normal")
            # 主办方结构化联系方式：非空字典才序列化为 JSON，否则传 None（不抖旧值）。
            organizer_contact_json = (
                json.dumps(organizer_contact, ensure_ascii=False)
                if organizer_contact else None
            )
            self.cur.execute(
                """
                UPDATE qualified_leads SET
                    event_name        = COALESCE(NULLIF(%s, ''), event_name),
                    event_details     = COALESCE(NULLIF(%s, ''), event_details),
                    intent_category   = %s,
                    has_lead_value    = %s,
                    lead_type         = %s,
                    priority_score    = %s,
                    priority_level    = %s,
                    resource_level    = %s,
                    scoring_breakdown = %s,
                    llm_reasoning     = %s,
                    is_online_voting  = %s,
                    online_voting_url = COALESCE(NULLIF(%s, ''), online_voting_url),
                    is_recurring      = %s,
                    activity_category = COALESCE(NULLIF(%s, ''), activity_category),
                    activity_region   = COALESCE(NULLIF(%s, ''), activity_region),
                    recurrence        = COALESCE(NULLIF(%s, ''), recurrence),
                    activity_status   = COALESCE(NULLIF(%s, ''), activity_status),
                    organizer_name    = COALESCE(NULLIF(%s, ''), organizer_name),
                    organizer_region  = COALESCE(NULLIF(%s, ''), organizer_region),
                    voting_platform   = COALESCE(NULLIF(%s, ''), voting_platform),
                    voting_status     = COALESCE(NULLIF(%s, ''), voting_status),
                    recurrence_period = COALESCE(NULLIF(%s, ''), recurrence_period),
                    edition_no        = COALESCE(%s, edition_no),
                    organizer_contact = COALESCE(%s::jsonb, organizer_contact),
                    llm_status        = 'done',
                    llm_last_run_at   = NOW(),
                    llm_attempts      = llm_attempts + 1,
                    updated_at        = NOW()
                WHERE id = %s AND updated_by_human = FALSE
                """,
                (
                    name or "", details or "",
                    ai.intent_category, bool(ai.is_lead), ai.lead_type,
                    ai.priority_score, ai.priority_level, resource_level,
                    breakdown, ai.reasoning,
                    is_online_voting, online_voting_url or "",
                    is_recurring, activity_category or "",
                    activity_region or "", recurrence or "", activity_status or "",
                    organizer_name or "", organizer_region or "", voting_platform or "",
                    voting_status or "", recurrence_period or "", edition_no,
                    organizer_contact_json,
                    lead_id,
                ),
            )
            if article_id is not None:
                self.cur.execute(
                    """
                    UPDATE articles_core SET
                        event_name        = COALESCE(NULLIF(%s, ''), event_name),
                        event_details     = COALESCE(NULLIF(%s, ''), event_details),
                        intent_category   = %s,
                        has_lead_value    = %s,
                        lead_type         = %s,
                        priority_score    = %s,
                        priority_level    = %s,
                        resource_level    = %s,
                        scoring_breakdown = %s,
                        llm_reasoning     = %s,
                        is_recurring      = %s,
                        activity_category = COALESCE(NULLIF(%s, ''), activity_category),
                        activity_region   = COALESCE(NULLIF(%s, ''), activity_region),
                        recurrence        = COALESCE(NULLIF(%s, ''), recurrence),
                        activity_status   = COALESCE(NULLIF(%s, ''), activity_status),
                        organizer_name    = COALESCE(NULLIF(%s, ''), organizer_name),
                        organizer_region  = COALESCE(NULLIF(%s, ''), organizer_region),
                        voting_platform   = COALESCE(NULLIF(%s, ''), voting_platform),
                        voting_status     = COALESCE(NULLIF(%s, ''), voting_status),
                        recurrence_period = COALESCE(NULLIF(%s, ''), recurrence_period),
                        edition_no        = COALESCE(%s, edition_no),
                        organizer_contact = COALESCE(%s::jsonb, organizer_contact),
                        updated_at        = NOW()
                    WHERE id = %s
                    """,
                    (
                        name or "", details or "",
                        ai.intent_category, bool(ai.is_lead), ai.lead_type,
                        ai.priority_score, ai.priority_level, resource_level,
                        breakdown, ai.reasoning,
                        is_recurring, activity_category or "",
                        activity_region or "", recurrence or "", activity_status or "",
                        organizer_name or "", organizer_region or "", voting_platform or "",
                        voting_status or "", recurrence_period or "", edition_no,
                        organizer_contact_json,
                        article_id,
                    ),
                )
            self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"回写 LLM 清洗结果失败 (lead_id={lead_id})：{e}")
            return False

    def mark_llm_failed(self, lead_id: int, max_attempts: int = 3) -> bool:
        """标记一次清洗失败：累加尝试次数，未达上限留 pending 等下轮重试，达上限则置 fail。"""
        try:
            self.cur.execute(
                """
                UPDATE qualified_leads SET
                    llm_status      = CASE WHEN llm_attempts + 1 >= %s THEN 'fail' ELSE 'pending' END,
                    llm_attempts    = llm_attempts + 1,
                    llm_last_run_at = NOW()
                WHERE id = %s
                """,
                (int(max_attempts), lead_id),
            )
            self.conn.commit()
            return True
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"标记 LLM 清洗失败状态出错 (lead_id={lead_id})：{e}")
            return False

    def close(self):
        """关闭数据库连接"""
        if self.cur:
            self.cur.close()
        if self.conn:
            self.conn.close()
    
    # ==================== 内部方法 ====================
    
    def _normalize_url(self, url: str) -> Tuple[str, str]:
        """
        规范化 URL
        
        Returns:
            (canonical_url, url_fingerprint)
        """
        
        if not url or "mp.weixin.qq.com" not in url:
            return url, ""
        
        parts = urlsplit(url)
        params = dict(parse_qsl(parts.query))

        # 去重指纹只用 __biz-mid-idx（不含 sn）：这三个已唯一标识一篇公众号文章，
        # 而 sn 只是每次分享的签名（PC 搜一搜有 sn、搜狗无 sn）——纳入 sn 会导致
        # 同一篇文章跨渠道指纹不同、第①层 URL 去重跨渠道失效。
        biz, mid, idx = params.get("__biz", ""), params.get("mid", ""), params.get("idx", "")
        # biz/mid 缺失（如搜狗账号已迁移页）时置空指纹，改走内容去重，避免空指纹误撞。
        fingerprint = "-".join([biz, mid, idx]) if (biz and mid) else ""

        # 规范 URL(统一参数顺序)：canonical 仍保留 sn（PC 带 sn 可点击）。
        keep_keys = {"__biz", "mid", "idx", "sn"}
        clean_params = {k: v for k, v in params.items() if k in keep_keys}
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                               urlencode(sorted(clean_params.items())),
                               parts.fragment))

        return clean_url, fingerprint
    
    def _check_url_fingerprint_exists(self, fp: str) -> Optional[tuple]:
        """检查 URL 指纹是否存在"""
        
        if not fp:
            return None
        
        self.cur.execute(
            "SELECT id, title, account, source_channel FROM articles_core WHERE url_fingerprint = %s",
            (fp,)
        )
        return self.cur.fetchone()
    
    def _check_content_hash_exists(self, hash_val: str) -> Optional[tuple]:
        """检查内容哈希是否存在"""
        
        self.cur.execute(
            "SELECT id FROM article_clusters WHERE content_hash = %s",
            (hash_val,)
        )
        return self.cur.fetchone()
    
    def _find_similar_content(self, simhash: int, threshold: float = 1.0) -> List[tuple]:
        """查找相似内容 (SimHash 汉明距离 <=max_distance)。

        注：PostgreSQL 的 `^` 是乘方而非异或，且无 bigint 原生 popcount，
        故改为拉取候选 simhash(TEXT) 在 Python 侧计算汉明距离，跨版本安全。

        默认阈值取 1.0（距离 0）而非早先的 0.9（距离 6）：现行 SimHash 对中文长文的
        区分度不足，放宽距离会大量误杀无关文章，详见 add_article 第③层处的实测说明。
        """
        
        max_distance = int(64 * (1 - threshold))
        
        self.cur.execute("""
            SELECT id, title, account, source_channel, simhash
            FROM articles_core 
            WHERE simhash IS NOT NULL AND simhash != ''
            ORDER BY id DESC
            LIMIT 2000
        """)
        
        result = []
        for row in self.cur.fetchall():
            try:
                other = int(row[4])
            except (TypeError, ValueError):
                continue
            if ContentHasher.hamming_distance(simhash, other) <= max_distance:
                result.append(row)
                if len(result) >= 5:
                    break
        return result
    
    def _should_keep_new_or_old(self, new_article, old_article: tuple) -> dict:
        """
        决策：是否保留新版本
        
        Returns:
            {"keep_new": bool, "new_priority": int, "old_priority": int, "similarity": float}
        """
        
        new_priority = self.SOURCE_PRIORITY.get(new_article.source_channel, 99)
        old_priority = self.SOURCE_PRIORITY.get(old_article[3], 99)
        
        keep_new = new_priority < old_priority
        
        # 计算相似度 (基于 SimHash 汉明距离；old_article[4] 为 TEXT 存储)
        try:
            old_simhash = int(old_article[4])
        except (TypeError, ValueError):
            old_simhash = 0
        similarity = 1 - (ContentHasher.hamming_distance(old_simhash, new_article.simhash) / 64)
        
        return {
            "keep_new": keep_new,
            "new_priority": new_priority,
            "old_priority": old_priority,
            "similarity": similarity
        }
    
    def _create_cluster(self, content_hash: str, article_id: int, channel: str):
        """创建内容集群"""
        
        try:
            self.cur.execute("""
                INSERT INTO article_clusters (content_hash, primary_article_id, channels)
                VALUES (%s, %s, ARRAY[%s])
                ON CONFLICT (content_hash) DO UPDATE SET mirror_count = article_clusters.mirror_count + 1
            """, (content_hash, article_id, channel))
            
            self.conn.commit()
            
        except Exception as e:
            log.warning(f"创建集群失败：{e}")
    
    def _map_keyword_to_article(self, keyword, article_id):
        """维护 keyword ↔ article 映射表（keyword_article_map）。

        支撑「多个关键词命中同一资源」的关键词效果统计：无论文章是新增还是被判为
        重复，只要某个关键词命中了它，就记一条映射；同一 (keyword, article_id) 再次
        命中则累加 match_count 并刷新 last_seen_at。属统计加分项，任何失败只 log +
        回滚，绝不阻断入库主流程。
        """
        if not keyword or not article_id:
            return
        try:
            self.cur.execute("""
                INSERT INTO keyword_article_map (keyword, article_id)
                VALUES (%s, %s)
                ON CONFLICT (keyword, article_id) DO UPDATE SET
                    last_seen_at = NOW(),
                    match_count = keyword_article_map.match_count + 1
            """, (keyword, article_id))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"维护关键词映射失败 (keyword={keyword!r}, article_id={article_id})：{e}")

    def _absorb_into_primary(self, primary_id: int, article, canonical_url: str):
        """跨渠道命中同一篇文章时：
        1) 把来者渠道并入主记录的 source_channels（逗号去重）；
        2) 以搜一搜为主：若来者渠道优先级更高（如 wechat_pc 命中原本 sogou 主记录），
           升级主记录为可点击真链 + 更全正文，并同步已提升的 qualified_leads 行。
        均为加分项，任何失败只回滚 + log，不阻断去重主流程。
        """
        incoming = article.source_channel
        # (1) 并入渠道集合
        try:
            self.cur.execute("""
                UPDATE articles_core SET source_channels = (
                    SELECT string_agg(DISTINCT trim(c), ',')
                    FROM unnest(string_to_array(
                        COALESCE(NULLIF(source_channels, ''), source_channel) || ',' || %s, ',')) AS c
                    WHERE trim(c) <> ''
                ) WHERE id = %s
            """, (incoming, primary_id))
            self.conn.commit()
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"并入渠道失败 (primary_id={primary_id}, ch={incoming}): {e}")

        # (2) 以搜一搜为主：来者优先级更高则升级主记录
        try:
            self.cur.execute("SELECT source_channel FROM articles_core WHERE id = %s", (primary_id,))
            row = self.cur.fetchone()
            cur_channel = row[0] if row else ""
            if self.SOURCE_PRIORITY.get(incoming, 99) < self.SOURCE_PRIORITY.get(cur_channel, 99):
                self.cur.execute("""
                    UPDATE articles_core SET
                        source_channel = %s,
                        canonical_url = %s,
                        original_url = %s,
                        account = COALESCE(NULLIF(%s, ''), account),
                        account_id = COALESCE(%s, account_id),
                        mid = COALESCE(%s, mid), idx = COALESCE(%s, idx), sn = COALESCE(%s, sn),
                        content = CASE WHEN length(%s) > length(COALESCE(content, '')) THEN %s ELSE content END
                    WHERE id = %s
                """, (incoming, canonical_url, article.url,
                      article.account or "", article.account_id, article.mid, article.idx, article.sn,
                      article.content or "", article.content or "", primary_id))
                self.conn.commit()
                # 同步已提升的线索行（若存在）：主渠道 + 可点击链接
                try:
                    self.cur.execute(
                        "UPDATE qualified_leads SET source_channel = %s, url = %s WHERE article_id = %s",
                        (incoming, canonical_url or article.url, primary_id))
                    self.conn.commit()
                except Exception as e2:  # noqa: BLE001
                    self.conn.rollback()
                    log.warning(f"同步线索主渠道失败 (article_id={primary_id}): {e2}")
                log.info(f"↑ 主记录升级为 {incoming}(可点击真链) ID:{primary_id}")
        except Exception as e:  # noqa: BLE001
            self.conn.rollback()
            log.warning(f"升级主记录失败 (primary_id={primary_id}): {e}")

    def _record_mirror(self, cluster_id: int, mirror_url: str, channel: str):
        """记录镜像链接"""
        
        try:
            self.cur.execute("""
                INSERT INTO article_mirrors (cluster_id, mirror_url, mirror_source_channel)
                VALUES (%s, %s, %s)
                ON CONFLICT (cluster_id, mirror_url) DO NOTHING
            """, (cluster_id, mirror_url, channel))
            
            self.conn.commit()
            
        except Exception as e:
            log.warning(f"记录镜像失败：{e}")


# ==================== 使用示例 ====================

if __name__ == "__main__":
    from wxsearch.models import Article
    
    # 模拟多篇文章 (包括重复的)
    articles = [
        Article(
            title="人工智能发展趋势报告",
            content="本文详细介绍了人工智能在医疗、教育、工业等领域的最新应用...",
            url="https://mp.weixin.qq.com/s?__biz=xxx&mid=yyy&idx=zzz&sn=aaa",
            source_channel="wechat_pc",
            keyword="人工智能",
            account="AI 前沿观察",
            account_id="official_account_123"
        ),
        Article(
            title="人工智能发展趋势报告 (转载)",
            content="本文详细介绍了人工智能在医疗、教育、工业等领域的最新应用...",  # 相同内容
            url="https://news.baidu.com/n?word=人工智能",
            source_channel="baidu_news",
            keyword="人工智能"
        ),
        Article(
            title="AI 技术新进展 (微调标题)",
            content="本文详细介绍了人工智能在医疗、教育、工业等领域的最新应用... (结尾略有不同)",  # 近似内容
            url="https://mp.weixin.qq.com/s?__biz=bbb&mid=ccc&idx=ddd&sn=eee",
            source_channel="wechat_pc",
            keyword="人工智能",
            account="科技日报"
        )
    ]
    
    # 初始化去重服务
    db_config = {
        "host": "localhost",
        "port": 5432,
        "database": "wx_search",
        "user": "admin",
        "password": "your_password"
    }
    
    store = SmartDedupStore(db_config)
    
    # 批量插入测试
    print("\n=== 批量插入测试 ===")
    stats = store.bulk_insert(articles)
    
    print(f"总文章数：{stats['total']}")
    print(f"新增加入：{stats['new']}")
    print(f"URL 重复：{stats['url_duplicate']}")
    print(f"内容完全重复：{stats['exact_duplicate']}")
    print(f"内容相似重复：{stats['similar_duplicate']}")
    print(f"错误：{stats['errors']}")
    
    store.close()

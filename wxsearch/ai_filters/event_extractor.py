"""活动信息抽取器（活动名称 / 活动信息 的 AI 初稿生成）——纯规则、离线、零外呼。

定位：线索入库后，从标题 + 正文里用规则抽取出「活动名称」与「活动信息」（活动介绍/
主办承办/时间地点/报名方式/联系方式等），作为 AI 初稿写进线索表，减少人工录入。

设计原则（与 rule_scorer 一致）：
  - 可开关：config["event_info"]["enabled"]，默认开；关掉则完全不自动填充，全交人工；
  - 只填空、不覆盖：只在字段为空时写入，绝不覆盖资源处理人员的人工编辑；
  - 失败不阻断：任何异常只 log，绝不影响入库/提升主流程；
  - 备注(notes) 永远不由 AI 生成，属纯人工态。
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from wxsearch.ai_filters.rule_scorer import load_rule_config

log = logging.getLogger(__name__)

# 正文按这些标点/换行切成短句，便于按标签定位。
_SEG_SPLIT = re.compile(r"[\n\r。；;]+")

# 结构化字段 → 触发关键词（命中即取该短句作为该字段内容）。
# 词表按「长词在前」排列，便于生成更干净的标签值。
_FIELD_KEYWORDS = [
    ("主办", ["主办单位", "主办方", "主办", "承办单位", "承办方", "承办", "协办", "组委会"]),
    ("时间", ["活动时间", "举办时间", "比赛时间", "评选时间", "征集时间", "活动日期", "举办日期"]),
    ("地点", ["活动地点", "举办地点", "比赛地点", "举办地", "地点", "地址"]),
    ("对象", ["参与对象", "征集对象", "参评对象", "参赛对象", "申报对象", "评选对象", "参选对象"]),
    ("报名", ["报名方式", "报名时间", "报名截止", "截止日期", "截止时间", "报名须知", "申报方式", "参与方式", "投稿方式", "参评方式"]),
    ("奖励", ["奖项设置", "奖励设置", "奖金", "奖励", "奖品"]),
    ("联系方式", ["联系电话", "咨询电话", "联系人", "联系方式", "咨询热线", "服务热线", "咨询方式"]),
]

# 联系方式补充抽取（正文里出现即补录，避免漏掉未带标签的电话/邮箱/链接）。
_PHONE_RE = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8})(?!\d)")
_EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s，。；;）)】」\"”]+")

# 活动名称（兜底）：标题为空时，从正文取书名号/引号包裹的名称。
_NAME_BRACKET_RE = re.compile(r"[《【「\"“]([^》】」\"”]{4,60})[》】」\"”]")
# 开头的【标签】、通知类噪音前缀、结尾的「的通知/的公告」。
_NAME_TAG_RE = re.compile(r"^【[^】]{1,14}】\s*")
_NAME_NOTICE_RE = re.compile(r"^(重要通知|紧急通知|通知公告|欢迎报名|欢迎参加|欢迎参与|通知|公告|关于)[！!：:，,\s]*")
_NAME_SUFFIX_RE = re.compile(r"(的通知|的公告|的通告|的函|的方案|的启事)\s*$")
# 活动名称提练：分隔符、年份、活动类型核心词、尾部动作词。
_SEP_RE = re.compile(r"[|｜丨]")
_YEAR_RE = re.compile(r"20\d{2}")
# 活动类型核心词（名称通常以此结尾）——长词在前，截到最后一个命中为止。
_ACTIVITY_RE = re.compile(
    r"评选活动|征集活动|投票活动|竞赛活动|评比活动|"
    r"评选|大赛|比赛|竞赛|征集|票选|投票|评比|颁奖典礼|大会|论坛|展览|赛事|大典|活动"
)
# 尾部动作词（无活动核心词时用于剥尾）。
_TRAILING_TAIL_RE = re.compile(
    r"(正式|隆重|盛大|火热|即将|再次)?"
    r"(启动|开启|开始啦|开始|启幕|开幕|圆满落幕|落幕|举行|举办|来了|上线|收官|揭晓|开赛|拉开帷幕)+[！!？?。.\s]*$"
)

# 「短标签：正文」前缀识别（用于剥掉重复标签，如「主办方：富宝资讯」→「富宝资讯」）。
_LABEL_PREFIX_RE = re.compile(r"^[^：:]{1,10}[：:]\s*(.+)$")
_ALL_KEYWORDS = [kw for _, kws in _FIELD_KEYWORDS for kw in kws]


def _strip_leading_slogan(name: str) -> str:
    """去掉开头的装饰口号段（如「光影揽山海，镜护众生灵｜」）：当分隔符前短段无年份、且后段含年份/活动词时剥离。"""
    m = _SEP_RE.search(name[:20])
    if not m:
        return name
    head, tail = name[: m.start()].strip(), name[m.end():].strip()
    if not tail or _YEAR_RE.search(head):
        return name
    tail_strong = _YEAR_RE.search(tail) or re.search(r"评选|大赛|比赛|征集|大会|活动|投票|票选", tail)
    head_strong = re.search(r"评选|大赛|比赛|征集|大会", head)
    if tail_strong and not head_strong:
        return tail
    if len(head) <= 14 and not head_strong:
        return tail
    return name


def _cut_after_activity(name: str) -> str:
    """截到最后一个活动类型核心词为止，丢弃其后的动作/无关尾巴（如「…评选活动怎么开展？…」）。"""
    last = None
    for mm in _ACTIVITY_RE.finditer(name):
        last = mm
    return name[: last.end()] if last else name


def _derive_event_name(title: str, content: str) -> str:
    """从标题提练简洁核心活动名称：去通知前缀 → 去开头口号 → 截到活动核心词 → 去尾部动作词。"""
    raw = str(title or "").strip()
    if not raw:
        m = _NAME_BRACKET_RE.search(str(content or "")[:200])
        return m.group(1).strip() if m else ""
    name = raw
    for _ in range(3):  # 多轮剥离【标签】+通知前缀
        new = _NAME_NOTICE_RE.sub("", _NAME_TAG_RE.sub("", name)).strip()
        if new == name:
            break
        name = new
    name = _strip_leading_slogan(name)
    name = _cut_after_activity(name)
    name = _TRAILING_TAIL_RE.sub("", name)
    name = _NAME_SUFFIX_RE.sub("", name)
    name = name.strip(" 　|｜丨-—·、，,。.！!？?")
    return name or raw


def _clean_value(seg: str) -> str:
    """去掉「短标签：」前缀，返回冒号后的正文；无冒号则原样返回。"""
    m = _LABEL_PREFIX_RE.match(seg)
    return m.group(1).strip() if m else seg.strip()


def _is_meaningful(value: str) -> bool:
    """判断抽取值是否含实质内容——剔除只剩标签词的空壳（如「报名方式」「报名时间」）。"""
    if not value:
        return False
    if _PHONE_RE.search(value) or _EMAIL_RE.search(value) or _URL_RE.search(value):
        return True
    residual = value
    for kw in _ALL_KEYWORDS:
        residual = residual.replace(kw, "")
    residual = re.sub(r"[\s：:，,、。.\-—()（）]+", "", residual)
    return len(residual) >= 2

_CONFIG_CACHE: Optional[dict] = None


def _cfg() -> dict:
    """读取并缓存 event_info 配置块（读不到用安全默认）。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        try:
            _CONFIG_CACHE = (load_rule_config() or {}).get("event_info", {}) or {}
        except Exception:  # noqa: BLE001
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def is_enabled() -> bool:
    """是否开启 AI 辅助填充（默认开）。"""
    return bool(_cfg().get("enabled", True))


def extract_event_info(title: str, content: str) -> Tuple[str, str]:
    """从标题 + 正文抽取 (event_name, event_details)。纯函数、不抛异常。

    - event_name：优先书名号/引号内名称，否则用清洗后的标题；
    - event_details：主办/时间/地点/报名/联系方式等结构化短句拼成的多行文本。
    抽不到内容则返回空串（交人工在悬浮窗补全）。
    """
    title = str(title or "").strip()
    content = str(content or "").strip()
    cfg = _cfg()
    max_name = int(cfg.get("max_name_len", 60))
    max_details = int(cfg.get("max_details_len", 800))

    # ---- LLM 路径（可选，默认关）：配置 event_info.backend == "llm" 时交大模型完善。
    #     失败自动回退到下方规则抽取，绝不阻断。
    if str(cfg.get("backend", "rule")).strip().lower() == "llm":
        try:
            from wxsearch.ai_filters.llm_analyzer import analyze as llm_analyze, build_event_fields
            data = llm_analyze(title, content)
            name, details = build_event_fields(data)
            if name or details:
                return name[:max_name], details[:max_details]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"LLM 活动信息抽取失败，回退规则：{exc}")

    # ---- 活动名称（从标题提练核心名称）----
    event_name = _derive_event_name(title, content)[:max_name]

    # ---- 活动信息（结构化短句）----
    text = f"{title}。{content}"
    segments = [s.strip() for s in _SEG_SPLIT.split(text) if s.strip()]
    lines, used = [], set()
    for label, kws in _FIELD_KEYWORDS:
        for seg in segments:
            if len(seg) > 120:  # 过长的段落多半是正文叙述，跳过
                continue
            if seg in used or not any(kw in seg for kw in kws):
                continue
            value = _clean_value(seg)
            if not _is_meaningful(value):  # 跳过只剩标签词的空壳
                continue
            lines.append(f"{label}：{value}")
            used.add(seg)
            break

    # 联系方式/链接兜底：正文里的电话/邮箱/URL 直接补录
    phones = list(dict.fromkeys(_PHONE_RE.findall(content)))[:3]
    emails = list(dict.fromkeys(_EMAIL_RE.findall(content)))[:2]
    urls = list(dict.fromkeys(_URL_RE.findall(content)))[:2]
    extra = []
    if phones:
        extra.append("电话：" + "，".join(phones))
    if emails:
        extra.append("邮箱：" + "，".join(emails))
    # 避免与已抽取的“联系方式”行重复堆叠
    if extra and not any(l.startswith("联系方式") for l in lines):
        lines.extend(extra)
    if urls and not any("http" in l for l in lines):
        lines.append("链接：" + "，".join(urls))

    event_details = "\n".join(lines)[:max_details]
    return event_name, event_details


def fill_for_article(cur, article_id: int) -> bool:
    """给一条 article_id 生成活动名称/信息并「只填空」写回 articles_core + qualified_leads。

    使用调用方传入的游标 cur（不自行 commit，由调用方统一提交/回滚）。
    仅在目标字段为空时写入，绝不覆盖人工编辑。返回是否写入了内容。
    """
    if not is_enabled():
        return False
    cur.execute(
        "SELECT title, content_clean, content, event_name, event_details "
        "FROM articles_core WHERE id = %s",
        (article_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    title, content_clean, content, cur_name, cur_details = row
    # 两个字段都已有值则无需再算
    if (cur_name or "").strip() and (cur_details or "").strip():
        return False

    name, details = extract_event_info(title, content_clean or content or "")

    cur.execute(
        """
        UPDATE articles_core
        SET event_name    = COALESCE(NULLIF(event_name, ''), %s),
            event_details = COALESCE(NULLIF(event_details, ''), %s)
        WHERE id = %s
        """,
        (name or None, details or None, article_id),
    )
    cur.execute(
        """
        UPDATE qualified_leads
        SET event_name    = COALESCE(NULLIF(event_name, ''), %s),
            event_details = COALESCE(NULLIF(event_details, ''), %s)
        WHERE article_id = %s
        """,
        (name or None, details or None, article_id),
    )
    return bool(name or details)

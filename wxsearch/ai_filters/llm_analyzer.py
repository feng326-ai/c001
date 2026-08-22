"""大模型分析器——把「一条线索」交给大模型，一次产出价值判定 + 活动信息。

对应用户诉求：大模型要①确定这条线索是否真有价值；②完善活动信息：
  1. 活动的真实名称；
  2. 活动开始/结束时间以及当前所处阶段；
  3. 线索中的联系人与联系方式。

一次调用（省钱省时）返回一个结构化 JSON，再分别映射为：
  - AIResult（评分/意图/线索判定，供 ai_analyzer 的 llm 后端用）；
  - (event_name, event_details)（活动名称/详情，供 event_extractor 的 llm 路径用）。

设计原则：
  - 失败即抛异常（LLMError 或解析异常），由调用方降级/回退，绝不阻断主流程；
  - 提示词强约束「只输出 JSON」，并对模型输出做健壮解析与字段兜底。

命令行自测（WebAI2API/DeepSeek 等接口就绪后可直接验证一条）：
    docker exec wxsearch_worker python -m wxsearch.ai_filters.llm_analyzer \
        --title "关于开展2026年度最美劳动者评选活动的通知" --content "……"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date
from typing import Optional, Tuple

from wxsearch.ai_filters.ai_analyzer import AIResult
from wxsearch.ai_filters.llm_client import get_client

log = logging.getLogger(__name__)

# 与规则评分器一致的意图口径（便于两种后端结果可比）。
_LEAD_INTENTS = {"评选", "投票", "征集", "活动"}
_VALID_INTENTS = _LEAD_INTENTS | {"资讯", "其他"}

# 评选系统平台域名字典：URL 域名 → 平台名（voting_platform 的兜底归一来源）。
# key 为域名关键片段（子串匹配），命中即归一为对应平台名；gov/edu 归「自建/官网」。
_VOTING_PLATFORM_DOMAINS = {
    "wjx.cn": "问卷星", "wjx.top": "问卷星", "wjx.com": "问卷星",
    "vote520.cn": "vote520",
    "huotp.cn": "活动统计", "huodongxing": "活动行",
    "yeeol.com": "壹枱",
    "mikecrm.com": "麦客",
    "jinshuju.net": "金数据", "jsj.top": "金数据",
    "mp.weixin.qq.com": "微信投票", "weixin.qq.com": "微信投票",
}


def _platform_from_url(url: str) -> str:
    """从投票 URL 的域名推断评选系统平台名（域名字典兜底）。

    命中第三方平台字典 → 平台名；命中 gov/edu 官方域名 → 『自建/官网』；否则空串。
    """
    u = str(url or "").strip().lower()
    if not u:
        return ""
    for frag, name in _VOTING_PLATFORM_DOMAINS.items():
        if frag in u:
            return name
    # 政府/教育/官方域名视为主办方自建站点，而非第三方投票 SaaS
    if ".gov.cn" in u or ".edu.cn" in u:
        return "自建/官网"
    return ""

# ==================== 提示词配置（可从 rule_config.json 外部化，缺省用内置默认）====================

_DEFAULT_SYSTEM_PROMPT = (
    "你是活动线索分析助手。用户会给你一篇公众号文章的标题和正文，"
    "请判断它是否是一条『评选/投票/征集/活动』类的、真实可参与的高价值线索，"
    "并抽取活动的关键信息。"
    "注意：正文中常夹带与活动无关的噪音——引导关注、广告插播、往期推荐/相关阅读、"
    "版权声明/免责声明、长按识别二维码、点赞在看等，请一律忽略，只依据真正的活动正文来判断与提取；"
    "联系人与联系方式经常出现在文章末尾，务必读完全文再作答。"
    "严格只输出一个 JSON 对象，不要输出任何解释、前后缀或代码块标记。"
)

_DEFAULT_SCHEMA_HINT = """请按以下 JSON 结构输出（字段必须齐全，无法确定的用空字符串或空数组，切勿编造）：
{
  "is_valuable": true/false,           // 是否真有商机价值（真实、可参与的活动通知，而非普通报道/广告）
  "value_score": 0-100,                // 价值评分（越高越值得跟进）
  "intent_category": "评选 | 投票 | 征集 | 活动 | 资讯 | 其他",
  "reasoning": "一句话说明为什么这样判定",
  "event_name": "活动的真实名称（去掉通知/口号等噪音，只保留核心名称）",
  "time_start": "活动开始日期 YYYY-MM-DD，未知留空",
  "time_end": "活动结束/截止日期 YYYY-MM-DD，未知留空",
  "current_stage": "未开始 | 报名中/投票中 | 评审中/已结束|未知",
  "is_online_voting": true/false,      // 是否有线上投票/网络评选环节（如有需同时填充 online_voting_url）
  "online_voting_url": "网络投票/报名链接，无则留空",
  "recurrence": "多届 | 第一届 | 单届（按是否往届/首届/一次性判定，未知留空）",
  "activity_region": "全国 | 省 | 市 | 县 | 镇（按主办层级/活动覆盖范围判定，未知留空）",
  "activity_status": "征集中 | 报名中 | 进行中 | 已结束（按当前所处阶段判定，未知留空）",
  "resource_quality": "优 | 普 | 低（优=规模大/权威高；普=一般；低=信息不全或明显广告软文）",
  "activity_category": "活动类别，如：评选|投票|征集|榜单|赛事|展会|其他",
  "organizer": "主办方主体全称（只取【主办】单位，忽略承办/协办/支持单位；多个主办取最主要的一个；未知留空）",
  "organizer_region": "主办方所在的具体省/市（如：湖南省、广州市；从主办方名称或正文判断，与活动覆盖层级不同；未知留空）",
  "voting_platform": "线上投票/评选所用的第三方平台名（如：问卷星、微信投票、活动统计等；若为主办方自建官网则填『自建/官网』；无线上投票或未知留空）",
  "voting_status": "has | none | suspect（has=明确有线上投票；suspect=评选/榜单/大赛类未提但很可能后续有投票；none=确无）",
  "recurrence_period": "年度 | 双年 | 季度 | 月度 | 不定期（周期粒度，不确定留空）",
  "edition_no": "第几届的数字（如「第二届」→2；未写留空，不编造）",
  "contact_person": "联系人姓名，未知留空",
  "contact_info": ["电话/邮箱/微信/报名链接等，逐条列出，未知留空数组"]
}"""


def _load_system_prompt() -> str:
    """从 rule_config.json 的 llm.prompt_schemas.system_prompt 读取系统提示词，缺省用内置默认。"""
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        cfg = (load_rule_config() or {}).get("llm", {}) or {}
        return cfg.get("prompt_schemas", {}).get("system_prompt", _DEFAULT_SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001
        return _DEFAULT_SYSTEM_PROMPT


def _load_schema_hint() -> str:
    """从 rule_config.json 的 llm.prompt_schemas.schema_hint 读取输出契约，缺省用内置默认。"""
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        cfg = (load_rule_config() or {}).get("llm", {}) or {}
        return cfg.get("prompt_schemas", {}).get("schema_hint", _DEFAULT_SCHEMA_HINT)
    except Exception:  # noqa: BLE001
        return _DEFAULT_SCHEMA_HINT


def _max_input_chars() -> int:
    """正文送入大模型的字数上限（默认 12000，可在 rule_config.json 的 llm.max_input_chars 调）。

    现代模型上下文动辄数万字，活动通知普遍很短，故默认整篇全发；仅极少数超长文
    （如多活动汇总）会触达上限，届时用「头部 + 尾部」兜底，兼顾开头活动介绍与结尾联系方式。
    """
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        cfg = (load_rule_config() or {}).get("llm", {}) or {}
        return int(cfg.get("max_input_chars", 12000))
    except Exception:  # noqa: BLE001
        return 12000


def _fit_content(content: str, limit: int) -> str:
    """把正文裁到 limit 以内：未超限整篇返回；超限则取头部 + 尾部并标注省略，绝不只留头部。"""
    body = str(content or "").strip()
    if len(body) <= limit:
        return body
    head = int(limit * 0.7)          # 头部占多，保留活动介绍/规则
    tail = limit - head              # 尾部保联系方式/报名截止
    return f"{body[:head]}\n……（此处省略中间 {len(body) - limit} 字）……\n{body[-tail:]}"


def _build_user_prompt(title: str, content: str, publish_time: Optional[str]) -> str:
    """拼接用户提示。默认整篇全发（读原文全文），超长才头尾兜底，避免丢失真实意图。

    注入【系统当前基准日】供模型换算“即日起/下周五/本月底”等相对时间，避免瞎猜年份。
    """
    body = _fit_content(content, _max_input_chars())
    pub = f"\n【发布时间】{publish_time}" if publish_time else ""
    today = date.today().isoformat()
    return (
        f"{_load_schema_hint()}\n\n"
        f"【系统当前基准日】{today}{pub}\n"
        f"【标题】{str(title or '').strip()}\n"
        f"【正文】{body}"
    )


def analyze(title: str, content: str, publish_time: Optional[str] = None) -> dict:
    """调用大模型分析一条线索，返回规范化+后置校验后的结果 dict。失败向上抛异常。"""
    client = get_client()
    user = _build_user_prompt(title, content, publish_time)
    system = _load_system_prompt()
    raw = client.chat_json(system, user)
    return _post_process(_normalize(raw))


def _post_process(data: dict) -> dict:
    """模型输出后的轻量规则纠偏（温和版，不否决价值）：
    1. 一致性：非线索却标“报名中/投票中” → 纠为已结束；
    2. 温和时间降级：截止日已过且阶段已是“评审中/已结束”时确保 status=已结束（不因仅报名截止就否决价值）；
    3. 空值兜底。
    """
    if not data.get("is_valuable") and data.get("current_stage") == "报名中/投票中":
        data["current_stage"] = "评审中/已结束"
        data["activity_status"] = "已结束"
    te = data.get("time_end") or ""
    try:
        if te and date.fromisoformat(te) < date.today() and data.get("current_stage") == "评审中/已结束":
            data["activity_status"] = "已结束"
    except ValueError:
        pass
    if not isinstance(data.get("contact_info"), list):
        data["contact_info"] = []
    return data


def _normalize(raw: dict) -> dict:
    """把模型返回的原始 dict 规范化为固定 schema（做类型/取值兜底）。"""
    intent = str(raw.get("intent_category", "") or "").strip()
    if intent not in _VALID_INTENTS:
        intent = "其他"

    try:
        score = float(raw.get("value_score", 0) or 0)
    except (ValueError, TypeError):
        score = 0.0
    score = max(0.0, min(100.0, score))

    # 新增字段处理
    is_online_voting = bool(raw.get("is_online_voting"))
    online_voting_url = str(raw.get("online_voting_url", "") or "").strip()
    activity_category = str(raw.get("activity_category", "") or "").strip()

    # 多届三态（白名单兑底）；为兼容旧 is_recurring 同时派生 bool
    recurrence = str(raw.get("recurrence", "") or "").strip()
    if recurrence not in ("多届", "第一届", "单届"):
        recurrence = ""
    is_recurring = (recurrence == "多届") or bool(raw.get("is_recurring"))

    activity_region = str(raw.get("activity_region", "") or "").strip()
    if activity_region not in ("全国", "省", "市", "县", "镇"):
        activity_region = ""
    activity_status = str(raw.get("activity_status", "") or "").strip()
    if activity_status not in ("征集中", "报名中", "进行中", "已结束"):
        activity_status = ""
    resource_quality = str(raw.get("resource_quality", "") or "").strip()
    if resource_quality not in ("优", "普", "低"):
        resource_quality = ""

    # 主办方地区（具体省/市，自由文本，只去首尾空白）
    organizer_region = str(raw.get("organizer_region", "") or "").strip()
    # 评选系统平台：优先用 LLM 抽的 voting_platform；为空时用投票 URL 域名字典兜底归一。
    voting_platform = str(raw.get("voting_platform", "") or "").strip()
    if not voting_platform:
        voting_platform = _platform_from_url(online_voting_url)

    # 线上投票三态 voting_status(has/none/suspect)：优先用 LLM 给的；缺省由硬证据派生。
    # has=明确有线上投票；suspect=评选/榜单类未提但痑有（交业务员核实）；none=确无。
    voting_status = str(raw.get("voting_status", "") or "").strip().lower()
    if voting_status not in ("has", "none", "suspect"):
        voting_status = ""
    if not voting_status:
        voting_status = "has" if (is_online_voting or online_voting_url) else "none"
    elif voting_status == "suspect" and (is_online_voting or online_voting_url):
        voting_status = "has"  # 有确凿证据就不是痑似

    # 周期粒度 recurrence_period(弱提示，白名单兜底)；届次序号 edition_no(可空整数)
    recurrence_period = str(raw.get("recurrence_period", "") or "").strip()
    if recurrence_period not in ("年度", "双年", "季度", "月度", "不定期"):
        recurrence_period = ""
    try:
        _ed_raw = str(raw.get("edition_no", "") or "").strip()
        _ed_m = re.search(r"\d+", _ed_raw)
        edition_no = int(_ed_m.group()) if _ed_m else None
    except (ValueError, TypeError):
        edition_no = None

    contact_info = raw.get("contact_info", []) or []
    if isinstance(contact_info, str):
        contact_info = [contact_info]
    contact_info = [str(x).strip() for x in contact_info if str(x).strip()]

    return {
        "is_valuable": bool(raw.get("is_valuable", False)),
        "value_score": score,
        "intent_category": intent,
        "reasoning": str(raw.get("reasoning", "") or "").strip(),
        "event_name": str(raw.get("event_name", "") or "").strip(),
        "time_start": str(raw.get("time_start", "") or "").strip(),
        "time_end": str(raw.get("time_end", "") or "").strip(),
        "current_stage": str(raw.get("current_stage", "") or "").strip(),
        "is_online_voting": is_online_voting,
        "online_voting_url": online_voting_url,
        "is_recurring": is_recurring,
        "recurrence": recurrence,
        "activity_region": activity_region,
        "activity_status": activity_status,
        "resource_quality": resource_quality,
        "activity_category": activity_category,
        "organizer": str(raw.get("organizer", "") or "").strip(),
        "organizer_region": organizer_region,
        "voting_platform": voting_platform,
        "voting_status": voting_status,
        "recurrence_period": recurrence_period,
        "edition_no": edition_no,
        "contact_person": str(raw.get("contact_person", "") or "").strip(),
        "contact_info": contact_info,
    }


def to_ai_result(data: dict) -> AIResult:
    """把分析结果映射为 AIResult（供 ai_analyzer 的 llm 后端落库）。"""
    score = float(data.get("value_score", 0.0))
    intent = data.get("intent_category", "其他")
    is_lead = bool(data.get("is_valuable")) and intent in _LEAD_INTENTS

    if score >= 70:
        level = "P0"
    elif score >= 45:
        level = "P1"
    else:
        level = "P2"
    # 资源质量：优先用 LLM 直接判定的 resource_quality(优/普/低)；未给时回退启发式。
    _quality_map = {"优": "excellent", "普": "normal", "低": "poor"}
    resource_level = _quality_map.get(data.get("resource_quality", ""))
    if not resource_level:
        resource_level = "excellent" if (is_lead and score >= 75) else "normal"

    return AIResult(
        analyzed=True,
        is_lead=is_lead,
        intent_category=intent,
        lead_type=intent if is_lead else None,
        priority_score=score,
        priority_level=level,
        resource_level=resource_level,
        reasoning="[LLM] " + (data.get("reasoning") or ("判定为线索" if is_lead else "非线索")),
        scoring_breakdown={"method": "llm", "value_score": score, "raw": data},
        tags=[intent] if intent != "其他" else [],
    )


def build_event_fields(data: dict) -> Tuple[str, str]:
    """把分析结果映射为 (event_name, event_details)——供 event_extractor 的 llm 路径用。

    event_details 组织为：时间 / 阶段 / 联系人 / 联系方式 的多行文本。
    """
    name = data.get("event_name", "") or ""

    lines = []
    start, end = data.get("time_start", ""), data.get("time_end", "")
    if start or end:
        span = f"{start or '?'} ~ {end or '?'}" if (start and end) else (start or end)
        lines.append(f"时间：{span}")
    if data.get("current_stage") and data["current_stage"] != "未知":
        lines.append(f"阶段：{data['current_stage']}")
    if data.get("contact_person"):
        lines.append(f"联系人：{data['contact_person']}")
    if data.get("contact_info"):
        lines.append("联系方式：" + "，".join(data["contact_info"]))

    return name, "\n".join(lines)


# 联系方式分类正则（粗分，仅用于结构化归档，不追求严格校验）。
_RE_EMAIL = re.compile(r"[\w.\-]+@[\w.\-]+\.\w+")
_RE_PHONE = re.compile(r"(?:\+?86[-\s]?)?(?:1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8})")
_RE_URL = re.compile(r"https?://[^\s，,；;]+")


def build_organizer_contact(data: dict) -> dict:
    """把联系人/联系方式整理为结构化 dict（落入主办方 organizer_contact JSONB 列）。

    结构：{person, phone[], email[], url[], wechat[], other[]}；各项去重、去空。
    contact_info 是一串自由文本，用正则粗分为电话/邮箱/链接，含“微信”的归 wechat，
    其余归 other；保守不丢弃信息（分不出的原样进 other）。全空时返回 {}。
    """
    person = str(data.get("contact_person", "") or "").strip()
    items = data.get("contact_info", []) or []
    if isinstance(items, str):
        items = [items]

    buckets: dict = {"phone": [], "email": [], "url": [], "wechat": [], "other": []}

    def _add(key: str, val: str):
        val = val.strip().strip("，,；; ")
        if val and val not in buckets[key]:
            buckets[key].append(val)

    for it in items:
        s = str(it or "").strip()
        if not s:
            continue
        matched = False
        for m in _RE_EMAIL.findall(s):
            _add("email", m); matched = True
        for m in _RE_URL.findall(s):
            _add("url", m); matched = True
        for m in _RE_PHONE.findall(s):
            _add("phone", m); matched = True
        if not matched:
            if "微信" in s or "wechat" in s.lower() or s.startswith("vx"):
                _add("wechat", s)
            else:
                _add("other", s)

    result = {"person": person} if person else {}
    for k, v in buckets.items():
        if v:
            result[k] = v
    return result


# ==================== 命令行自测 ====================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="大模型线索分析自测")
    parser.add_argument("--title", required=True, help="文章标题")
    parser.add_argument("--content", default="", help="文章正文")
    parser.add_argument("--publish-time", default=None, help="发布时间（可选）")
    args = parser.parse_args()

    client = get_client()
    print(f"→ 端点：{client.endpoint}  模型：{client.model}  鉴权：{'有' if client.api_key else '无'}")
    result = analyze(args.title, args.content, args.publish_time)
    print("\n【分析结果】")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n【映射为 AIResult】")
    print(json.dumps(to_ai_result(result).to_dict(), ensure_ascii=False, indent=2))
    name, details = build_event_fields(result)
    print("\n【活动名称】", name)
    print("【活动详情】\n" + details)

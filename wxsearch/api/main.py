"""
FastAPI 管理后台服务
提供关键词管理、任务领取、进度监控等 API
"""

import hmac
import json
import logging
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from pathlib import Path

# 初始化日志
log = logging.getLogger(__name__)

from ..task_scheduler import DistributedTaskScheduler
from ..models import FeedbackRequest
from ..db_connector import DatabaseConnector
from .auth import (current_user_optional, authenticate, make_session_token,
                   get_current_user, require_admin, require_super, hash_password, verify_password,
                   COOKIE_NAME, TENANT_COOKIE_NAME,
                   make_tenant_scope_token, session_cookie_options)


app = FastAPI(
    title="WX Search AI Collector",
    description="多渠道智能采集系统 - 分布式任务调度与 AI 清洗",
    version="1.0.0"
)

# 响应 gzip 压缩：看板 HTML/JSON 是纯文本，压缩后体积降一个数量级，
# 对外网/中转带宽场景（小带宽云服务器 frp）提速明显；小于 512B 不压缩。
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=512)

from .tenant_session import (
    router as tenant_session_router,
    tenant_review_enabled,
    tenant_session_binding_enabled,
    validate_tenant_feature_flags,
)

async def validate_tenant_session_rollout_flags():
    """Reject unsafe or partially enabled tenant rollout configurations."""
    validate_tenant_feature_flags()


app.router.add_event_handler("startup", validate_tenant_session_rollout_flags)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """全局登录保护：/admin* 未登录跳登录页；/api/* 未登录返 401；放行 登录/静态/健康。
    同时给后台页面加禁缓存头，避免浏览器用旧页面/旧 JS 导致会话失效时“翻页无数据”。"""
    path = request.url.path
    if path.startswith("/api/v2/session") and not tenant_session_binding_enabled():
        return JSONResponse(
            {"detail": "not_found"},
            status_code=404,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "Vary": "Cookie",
            },
        )
    if (
        path.startswith("/api/v2/tenant-candidates")
        or path.startswith("/api/v2/tenant-reviews")
    ) and not tenant_review_enabled():
        return JSONResponse(
            {"detail": "not_found"},
            status_code=404,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "Vary": "Cookie",
            },
        )
    allow = path in ("/login", "/logout", "/api/v1/login", "/", "/health", "/favicon.ico",
                     "/api/v1/collect_logs/report",
                     "/api/v1/settings/collection") or path.startswith("/static")
    # 采集参数仅放行只读 GET（采集机拉取）；PUT 仍需登录会话
    if path == "/api/v1/settings/collection" and request.method != "GET":
        allow = False
    if not allow and (path.startswith("/admin") or path.startswith("/api")):
        if not current_user_optional(request):
            if path.startswith("/api"):
                return JSONResponse({"detail": "未登录或会话失效"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
    # 菜单权限拦截：已登录用户访问被隐藏菜单页 → 回线索公海（前端隐藏 + 后端兜底，防直连 URL）。
    if path.startswith("/admin"):
        _u = current_user_optional(request)
        if _u:
            _norm = "/admin" if path == "/admin/" else path
            if _norm in _MENU_KEYS and not _menu_visible(_u.get("role", ""), _norm):
                return RedirectResponse("/admin", status_code=302)
    resp = await call_next(request)
    if (
        path.startswith("/admin")
        or path == "/login"
        or path.startswith("/api/v2/session")
        or path.startswith("/api/v2/tenant-candidates")
        or path.startswith("/api/v2/tenant-reviews")
    ):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        if path.startswith("/api/v2/session"):
            resp.headers["Vary"] = "Cookie"
    return resp


@app.get("/favicon.ico")
async def favicon():
    """站点图标（目标图标）；浏览器自动请求根目录 /favicon.ico，覆盖登录页与所有后台页。"""
    p = Path(__file__).parent.parent / "static" / "favicon.png"
    if p.exists():
        return FileResponse(str(p), media_type="image/png")
    return JSONResponse({"detail": "favicon not found"}, status_code=404)

# ==================== 默认提示词（系统设置页面用）====================

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
  "is_online_voting": true/false,      // 是否有线上投票/网络评选环节
  "online_voting_url": "网络投票/报名链接，无则留空",
  "recurrence": "多届 | 第一届 | 单届（按是否往届/首届/一次性判定，未知留空）",
  "activity_region": "全国 | 省 | 市 | 县 | 镇（按主办层级/活动覆盖范围判定，未知留空）",
  "activity_status": "征集中 | 报名中 | 进行中 | 已结束（按当前所处阶段判定，未知留空）",
  "resource_quality": "优 | 普 | 低（优=规模大/权威高；普=一般；低=信息不全或明显广告软文）",
  "activity_category": "活动类别，如：评选|投票|征集|榜单|赛事|展会|其他",
  "contact_person": "联系人姓名，未知留空",
  "contact_info": ["电话/邮箱/微信/报名链接等，逐条列出，未知留空数组"]
}"""

# 采集参数默认值（页面未配置时回退用此；与采集器 config.json 的 selectors/collect 字段对齐）
_DEFAULT_COLLECT_SETTINGS = {
    "wechat": {
        "filter_sort": "最新",
        "filter_type": "文章",
        "filter_time": "最近一天",
        "filter_scope": "",
        "max_items_per_keyword": 200,
        "max_scrolls": 30,
    },
    "sogou": {
        "enabled": False,
        "filter_time": "最近一天",
        "filter_type": "文章",
        "max_items_per_keyword": 100,
        # 搜狗管理页（/admin/sogou）可改的运行参数，VM 循环每轮拉取即生效：
        "batch": 5,                # 每轮领词数
        "interval_seconds": 60,    # 轮间隔秒
        "concurrency": 1,          # 并发 worker 数（各自独立浏览器）
        "proxies": [],             # 代理池，一行一个；worker 轮询绑定
    },
}

# 采集机日志上报令牌：VM 循环无登录会话，用固定令牌走 /api/v1/collect_logs/report。
_COLLECT_LOG_TOKEN = os.getenv("COLLECT_LOG_TOKEN", "").strip()

# ==================== 菜单权限（角色 → 菜单可见性，超管在成员管理页配置）====================
# 权威菜单清单（key=路由, label=显示名）：前端 sidebar 与权限管理 UI 均以此为准。
_MENU_ITEMS = [
    ("/admin", "线索公海"),
    ("/admin/ai_library", "AI活动库"),
    ("/admin/library", "我的活动库"),
    ("/admin/organizers", "主办方库"),
    ("/admin/keywords", "数据优化"),
    ("/admin/collection", "采集设置"),
    ("/admin/devices", "设备监控"),
    ("/admin/sogou", "搜狗采集"),
    ("/admin/settings", "系统设置"),
    ("/admin/feedback", "反馈管理"),
    ("/admin/users", "成员管理"),
]
_MENU_KEYS = {k for k, _ in _MENU_ITEMS}

# 默认可见性（仅 admin/member 可配；super 恒全部可见，不入配置）。
# 沿用历史：普通管理员除主办方库外都可见；成员仅三大库。主办方库默认仅超管。
_DEFAULT_MENU_PERMS = {
    "admin": {k: (k != "/admin/organizers") for k, _ in _MENU_ITEMS},
    "member": {k: (k in ("/admin", "/admin/ai_library", "/admin/library")) for k, _ in _MENU_ITEMS},
}


def _menu_perms_path() -> str:
    """菜单权限配置文件路径（独立于 rule_config，避免与采集/LLM 配置混杂）。"""
    return str(Path(__file__).parent.parent / "config" / "menu_permissions.json")


def load_menu_perms() -> dict:
    """读取角色菜单权限，以内置默认为底合并（缺文件/缺键都不报错，回退默认）。"""
    perms = {r: dict(m) for r, m in _DEFAULT_MENU_PERMS.items()}
    try:
        with open(_menu_perms_path(), "r", encoding="utf-8") as f:
            saved = json.load(f) or {}
        for role in ("admin", "member"):
            for k, v in (saved.get(role, {}) or {}).items():
                if k in _MENU_KEYS:
                    perms[role][k] = bool(v)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # 线索公海是兜底落点，必须始终可见。
    perms["admin"]["/admin"] = True
    perms["member"]["/admin"] = True
    return perms


def save_menu_perms(permissions: dict) -> None:
    """保存角色菜单权限（只接受 admin/member，键限白名单，值转 bool，/admin 强制可见）。"""
    out = {r: dict(m) for r, m in _DEFAULT_MENU_PERMS.items()}
    for role in ("admin", "member"):
        for k, v in (permissions.get(role, {}) or {}).items():
            if k in _MENU_KEYS:
                out[role][k] = bool(v)
    out["admin"]["/admin"] = True
    out["member"]["/admin"] = True
    with open(_menu_perms_path(), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def _menu_visible(role: str, path: str) -> bool:
    """某角色是否可见/可访问某菜单路径。super 恒 True；非菜单路径不拦（True）。"""
    if role == "super":
        return True
    if path not in _MENU_KEYS:
        return True
    return bool(load_menu_perms().get(role, {}).get(path, False))


# ==================== Pydantic Models ====================

class KeywordCreate(BaseModel):
    """创建关键词请求"""
    keyword: str
    category: Optional[str] = None
    weight: int = 1
    channels: Optional[List[str]] = None   # 分组/渠道：souyisou=核心词、sogou=拓展词；缺省两者都给

class KeywordUpdate(BaseModel):
    """更新关键词状态"""
    enabled: Optional[bool] = None
    status: Optional[str] = None
    update_cycle_minutes: Optional[int] = None
    channels: Optional[List[str]] = None       # 改分组（同步 keyword_channel_state）
    cycles: Optional[dict] = None              # {channel: minutes} 词×渠道专属周期

class TaskClaimRequest(BaseModel):
    """VM 端领取任务请求"""
    channel: str
    vm_instance_id: str
    max_keywords: int = 10

class TaskResultReport(BaseModel):
    """上报采集结果请求"""
    keyword: str
    channel: str
    vm_instance: str
    articles_count: int
    success: bool
    error_message: Optional[str] = None

class ManualMarkingRequest(BaseModel):
    """人工标注请求"""
    lead_id: int
    was_relevant: bool
    corrected_category: Optional[str] = None
    tags: Optional[List[str]] = None

class SystemSettingsUpdate(BaseModel):
    """系统配置更新请求（只允许更新 llm 块 + prompt_schemas，api_key 单独存 secrets.json）"""
    llm_model: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_timeout: Optional[float] = None
    llm_temperature: Optional[float] = None
    llm_max_tokens: Optional[int] = None
    api_key: Optional[str] = None
    system_prompt: Optional[str] = None
    schema_hint: Optional[str] = None
    clean_enabled: Optional[bool] = None


class ModelProbeRequest(BaseModel):
    """超级管理员在保存前探测一个受控 OpenAI 兼容端点。"""
    base_url: Optional[str] = None
    api_key: Optional[str] = None

class CollectionSettingsUpdate(BaseModel):
    """采集参数更新请求（写回 rule_config.json 的 collect_settings 块，供采集器拉取）"""
    wechat: Optional[dict] = None   # 搜一搜筛选：filter_sort/filter_type/filter_time/filter_scope/max_items_per_keyword/max_scrolls
    sogou: Optional[dict] = None    # 搜狗：enabled/filter_time/max_items_per_keyword/batch/interval_seconds/concurrency/proxies

class CollectLogItem(BaseModel):
    """采集机日志单条"""
    device_id: str = ""
    level: str = "INFO"
    message: str

class CollectLogBatch(BaseModel):
    """采集机日志批量上报"""
    logs: List[CollectLogItem]

class MenuPermsUpdate(BaseModel):
    """菜单权限更新请求（仅超管）：{permissions: {admin: {key:bool}, member: {key:bool}}}"""
    permissions: dict

class FilterConfigUpdate(BaseModel):
    """过滤模型总控台配置更新（仅超管）：只允许白名单内的配置段，写回 rule_config.json。"""
    thresholds: Optional[dict] = None          # 阶段3 评分阈值(p0_min/p1_min/lead_min_score)
    priority: Optional[dict] = None            # 阶段3 时效/截止信号
    resource_level: Optional[dict] = None      # 阶段3 资源规模信号组
    cleaning: Optional[dict] = None            # 阶段1 采集端粗过滤阈值
    ended_title_signals: Optional[dict] = None # 阶段3 标题域已结束信号
    event_modifiers: Optional[dict] = None     # 事件键修饰词表
    negative_keywords: Optional[list] = None   # 全文负向黑名单

# ==================== API Routes ====================

@app.get("/")
async def root():
    """根路径直接进看板（未登录会由中间件再跳登录页）；
    健康检查请用 /health，避免用户输域名看到一串 JSON。"""
    return RedirectResponse("/admin", status_code=302)

@app.get("/health")
async def health_check():
    """检查 Web 进程实际依赖的 PostgreSQL 与 Redis。"""
    checks = {}
    db = None

    try:
        db = DatabaseConnector()
        rows = db.execute_query("SELECT 1")
        checks["postgresql"] = {
            "ok": bool(rows and rows[0][0] == 1)
        }
    except Exception as e:
        checks["postgresql"] = {
            "ok": False,
            "error": type(e).__name__,
        }
    finally:
        if db is not None:
            db.close()

    redis_client = None
    try:
        import redis

        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise RuntimeError("REDIS_URL is not configured")
        redis_client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        checks["redis"] = {"ok": bool(redis_client.ping())}
    except Exception as e:
        checks["redis"] = {
            "ok": False,
            "error": type(e).__name__,
        }
    finally:
        if redis_client is not None:
            redis_client.close()

    healthy = all(item["ok"] for item in checks.values())
    payload = {
        "status": "healthy" if healthy else "unhealthy",
        "mode": "distributed_postgresql",
        "checks": checks,
        "timestamp": datetime.now().isoformat(),
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)

# ==================== 系统设置 API ====================

@app.get("/api/v1/settings/system")
async def get_system_settings(current_user: dict = Depends(require_super)):
    """
    获取系统配置（LLM 参数 + 提示词）
    
    GET /api/v1/settings/system
    
    返回：
    - llm_config: { base_url, model, timeout, temperature, max_tokens, api_key_set(bool) }
    - prompts: { system_prompt, schema_hint }
    """
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        from wxsearch.ai_filters.llm_client import get_client, get_clean_enabled, load_secret_api_key
        
        cfg = load_rule_config() or {}
        llm_cfg = cfg.get("llm", {}) or {}
        client = get_client()
        
        # 检测密钥是否已配置（环境变量 或 secrets.json）
        api_key_set = bool(
            os.getenv("OPENAI_API_KEY") or os.getenv("AI_API_KEY") or load_secret_api_key()
        )
        # 后台清洗开关：rule_config.llm.clean_enabled 显式设了就用它，否则回退 docker env
        config_clean = llm_cfg.get("clean_enabled")
        env_clean = os.getenv("LLM_CLEAN_ENABLED", "false").lower() == "true"
        is_llm_clean_active = config_clean if config_clean is not None else env_clean
        
        return {
            "llm_config": {
                "base_url": llm_cfg.get("base_url", client.base_url),
                "model": llm_cfg.get("model", client.model),
                "timeout": llm_cfg.get("timeout", client.timeout),
                "temperature": llm_cfg.get("temperature", client.temperature),
                "max_tokens": llm_cfg.get("max_tokens", client.max_tokens),
                "api_key_set": api_key_set,
            },
            "prompts": {
                "system_prompt": llm_cfg.get("prompt_schemas", {}).get("system_prompt", _DEFAULT_SYSTEM_PROMPT),
                "schema_hint": llm_cfg.get("prompt_schemas", {}).get("schema_hint", _DEFAULT_SCHEMA_HINT),
            },
            "is_llm_clean_active": is_llm_clean_active,
        }
    except Exception as e:
        log.error(f"获取系统配置失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取配置失败：{str(e)}")


@app.put("/api/v1/settings/system")
async def update_system_settings(update: SystemSettingsUpdate, current_user: dict = Depends(require_super)):
    """
    更新系统配置（LLM 参数 + 提示词）
    
    PUT /api/v1/settings/system
    Body: {
      "llm_model": "gpt-4o-mini",
      "llm_base_url": "http://host.docker.internal:3000/v1",
      "llm_timeout": 60.0,
      "llm_temperature": 0.2,
      "llm_max_tokens": 1024,
      "system_prompt": "...",
      "schema_hint": "..."
    }
    
    返回：{"updated": true, "message": "配置已保存，LLM 客户端将自动重新加载"}
    """
    try:
        from wxsearch.ai_filters.rule_scorer import _DEFAULT_CONFIG_PATH
        from wxsearch.ai_filters.llm_client import reset_client, save_secret_api_key
        
        path = os.getenv("RULE_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
        
        # API 密钥单独写入 secrets.json（绝不进 rule_config.json / 不入库）
        if update.api_key is not None and update.api_key.strip():
            save_secret_api_key(update.api_key.strip())
        
        # 读取现有配置
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cfg = {}
        
        # 构建 llm 块
        llm_block = cfg.get("llm", {}) or {}
        
        # 后台 LLM 清洗开关
        if update.clean_enabled is not None:
            llm_block["clean_enabled"] = bool(update.clean_enabled)
        
        if update.llm_model is not None:
            llm_block["model"] = update.llm_model
        if update.llm_base_url is not None:
            llm_block["base_url"] = update.llm_base_url
        if update.llm_timeout is not None:
            llm_block["timeout"] = float(update.llm_timeout)
        if update.llm_temperature is not None:
            llm_block["temperature"] = float(update.llm_temperature)
        if update.llm_max_tokens is not None:
            llm_block["max_tokens"] = int(update.llm_max_tokens)
        
        # 提示词部分（允许更新或清空）
        prompt_schemas = llm_block.get("prompt_schemas", {})
        if update.system_prompt is not None:
            prompt_schemas["system_prompt"] = update.system_prompt
        if update.schema_hint is not None:
            prompt_schemas["schema_hint"] = update.schema_hint
        llm_block["prompt_schemas"] = prompt_schemas
        
        cfg["llm"] = llm_block
        
        # 写回文件
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        
        # 强制重置客户端单例（下次调用时自动重新初始化）
        reset_client()
        
        log.info(f"✅ 系统配置已更新：llm={json.dumps(llm_block, ensure_ascii=False)}")
        
        return {
            "updated": True,
            "message": "配置已保存生效"
        }
    except Exception as e:
        log.error(f"保存系统配置失败：{e}")
        raise HTTPException(status_code=500, detail=f"保存配置失败：{str(e)}")


def _validate_model_probe_base_url(
    candidate: str, configured_base_url: str
) -> str:
    """Allow the configured endpoint or an explicitly allowlisted HTTPS host."""
    normalized = str(candidate or "").strip().rstrip("/")
    configured = str(configured_base_url or "").strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="模型服务地址格式不安全")
    if normalized == configured:
        return normalized

    allowed_hosts = {
        item.strip().lower()
        for item in os.getenv("LLM_PROBE_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    if parsed.scheme != "https" or parsed.hostname.lower() not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail="自定义模型地址必须使用 HTTPS 且主机已加入服务端允许列表",
        )
    return normalized


@app.post("/api/v1/settings/models")
async def list_available_models(
    probe: ModelProbeRequest,
    current_user: dict = Depends(require_super),
):
    """
    拉取当前大模型服务的可用模型列表（OpenAI 兼容 GET /models）。

    POST /api/v1/settings/models
    可选 JSON：base_url / api_key（密钥不进入 URL、浏览器历史或代理日志）。
    自定义地址必须显式提供临时密钥，绝不继承服务器已保存密钥。
    返回：{"models": ["..."], "count": N}
    """
    try:
        from wxsearch.ai_filters.llm_client import LLMClient, get_client

        configured_client = get_client()
        requested_base_url = str(probe.base_url or "").strip()
        temporary_key = str(probe.api_key or "").strip()
        if requested_base_url:
            target = _validate_model_probe_base_url(
                requested_base_url, configured_client.base_url
            )
            is_custom = target != configured_client.base_url.rstrip("/")
            if is_custom and not temporary_key:
                raise HTTPException(
                    status_code=400,
                    detail="探测自定义模型地址必须显式提供临时密钥",
                )
        else:
            target = configured_client.base_url

        if temporary_key:
            client = LLMClient(base_url=target, api_key=temporary_key)
        else:
            client = configured_client
        models = client.list_models()
        return {"models": models, "count": len(models)}
    except Exception as e:
        log.error(f"拉取模型列表失败：{e}")
        raise HTTPException(status_code=502, detail=f"拉取模型失败：{str(e)}")


@app.get("/api/v1/settings/collection")
async def get_collection_settings():
    """获取采集参数（搜一搜/搜狗筛选）。缺省回退内置默认。采集器也可调此接口拉取最新参数。

    GET /api/v1/settings/collection
    返回：{"wechat": {...}, "sogou": {...}}
    """
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        cfg = load_rule_config() or {}
        saved = cfg.get("collect_settings", {}) or {}
        # 以默认为底，合并已保存值（缺字段不报错）
        result = {}
        for group, defaults in _DEFAULT_COLLECT_SETTINGS.items():
            merged = dict(defaults)
            merged.update(saved.get(group, {}) or {})
            result[group] = merged
        return result
    except Exception as e:
        log.error(f"获取采集参数失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取采集参数失败：{str(e)}")


@app.put("/api/v1/settings/collection")
async def update_collection_settings(update: CollectionSettingsUpdate, current_user: dict = Depends(require_super)):
    """更新采集参数，写回 rule_config.json 的 collect_settings 块。

    PUT /api/v1/settings/collection
    Body: {"wechat": {"filter_sort":"最新","filter_time":"最近一天",...}, "sogou": {...}}
    只覆盖 body 中出现的分组与字段，其余保留。
    """
    try:
        from wxsearch.ai_filters.rule_scorer import _DEFAULT_CONFIG_PATH
        path = os.getenv("RULE_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cfg = {}

        block = cfg.get("collect_settings", {}) or {}
        for group in ("wechat", "sogou"):
            incoming = getattr(update, group)
            if incoming is not None:
                merged = dict(block.get(group, {}) or {})
                merged.update(incoming)
                block[group] = merged
        cfg["collect_settings"] = block

        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        log.info(f"✅ 采集参数已更新：{json.dumps(block, ensure_ascii=False)}")
        return {"updated": True, "message": "采集参数已保存（采集器下次任务时生效）", "collect_settings": block}
    except Exception as e:
        log.error(f"保存采集参数失败：{e}")
        raise HTTPException(status_code=500, detail=f"保存采集参数失败：{str(e)}")

# ==================== 采集机日志 API ====================

_COLLECT_LOG_KEEP = 20000   # 最多保留条数，超出裁剪最旧

@app.post("/api/v1/collect_logs/report")
async def report_collect_logs(
    batch: CollectLogBatch,
    x_collect_token: Optional[str] = Header(
        default=None, alias="X-Collect-Token"
    ),
    x_token: Optional[str] = Query(default=None),
):
    """采集机（VM）批量上报运行日志。无登录会话，用固定令牌鉴权。

    POST /api/v1/collect_logs/report，令牌放在 X-Collect-Token 请求头。
    x_token query 默认拒绝；仅当 ALLOW_LEGACY_COLLECT_LOG_QUERY=1 时临时兼容。
    Body: {"logs": [{"device_id": "sogou-vm-01", "level": "ERROR", "message": "..."}]}
    """
    if not _COLLECT_LOG_TOKEN:
        raise HTTPException(status_code=503, detail="采集日志令牌未配置")
    supplied_token = x_collect_token or ""
    if not supplied_token and x_token:
        legacy_query_enabled = os.getenv(
            "ALLOW_LEGACY_COLLECT_LOG_QUERY", ""
        ).strip().lower() in {"1", "true", "yes"}
        if legacy_query_enabled:
            log.warning(
                "接受了一次旧版 query 采集日志令牌；请升级节点并关闭 "
                "ALLOW_LEGACY_COLLECT_LOG_QUERY"
            )
            supplied_token = x_token
    if not hmac.compare_digest(supplied_token, _COLLECT_LOG_TOKEN):
        raise HTTPException(status_code=401, detail="令牌无效")
    if not batch.logs:
        return {"inserted": 0}
    db = DatabaseConnector()
    try:
        rows = [(l.device_id or "", (l.level or "INFO").upper()[:10], l.message[:1000])
                for l in batch.logs[-500:]]   # 单批上限 500 条，防刷爆
        # execute_write 只接单条参数元组 → 拼多行 VALUES 一次插入
        flat = []
        for r in rows:
            flat.extend(r)
        ph = ",".join(["(%s, %s, %s)"] * len(rows))
        db.execute_write(
            f"INSERT INTO collect_logs (device_id, level, message) VALUES {ph}",
            tuple(flat))
        # 顺手裁剪：只留最近 N 条
        db.execute_write(
            "DELETE FROM collect_logs WHERE id NOT IN (SELECT id FROM collect_logs ORDER BY id DESC LIMIT %s)",
            (_COLLECT_LOG_KEEP,))
        return {"inserted": len(rows)}
    except Exception as e:
        log.error(f"采集日志上报失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/v1/collect_logs")
async def list_collect_logs(
    device_id: Optional[str] = None,
    level: Optional[str] = None,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, le=1000),
    current_user: dict = Depends(require_admin),
):
    """读取采集机日志（管理页轮询）。after_id 之后追加拉取；level=ERROR 只看错误。"""
    q = "SELECT id, device_id, level, message, created_at FROM collect_logs WHERE id > %s"
    params: list = [after_id]
    if device_id:
        q += " AND device_id = %s"
        params.append(device_id)
    if level:
        q += " AND level = %s"
        params.append(level.upper())
    q += " ORDER BY id DESC LIMIT %s"
    params.append(limit)
    db = DatabaseConnector()
    try:
        rows = db.execute_query(q, tuple(params))
        # 拉的是最新 limit 条（倒序），前端按时间正序展示 → 反一下
        rows = list(reversed(rows))
        err_row = db.execute_query(
            "SELECT COUNT(*) FROM collect_logs WHERE level IN ('ERROR', 'CRITICAL') AND created_at > NOW() - INTERVAL '24 hours'")
        return {
            "logs": [{"id": r[0], "device_id": r[1], "level": r[2], "message": r[3],
                      "created_at": r[4].isoformat() if r[4] else ""} for r in rows],
            "error_count_24h": err_row[0][0] if err_row else 0,
        }
    finally:
        db.close()

# ==================== 调度运行日志 API（全渠道关键词）====================

@app.get("/api/v1/schedule_logs")
async def list_schedule_logs(
    channel: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=300, le=1000),
    current_user: dict = Depends(require_admin),
):
    """全渠道关键词的调度+运行日志（读 collect_tasks）。

    GET /api/v1/schedule_logs?channel=souyisou&status=failed
    每条=一次关键词采集运行：渠道/设备/关键词/状态/新增数/错误。
    """
    q = ("SELECT ct.id, ct.channel, ct.device_id, k.keyword, ct.status, "
         "ct.articles_count, ct.error_message, ct.start_time "
         "FROM collect_tasks ct LEFT JOIN keywords k ON k.id = ct.keyword_id WHERE 1=1")
    params: list = []
    if channel:
        q += " AND ct.channel = %s"
        params.append(channel)
    if status:
        q += " AND ct.status = %s"
        params.append(status)
    q += " ORDER BY ct.start_time DESC LIMIT %s"
    params.append(limit)
    db = DatabaseConnector()
    try:
        rows = db.execute_query(q, tuple(params))
        rows = list(reversed(rows))  # 按时间正序展示
        # 可选渠道列表（给前端筛选下拉）
        chans = db.execute_query(
            "SELECT DISTINCT channel FROM collect_tasks WHERE channel IS NOT NULL ORDER BY channel")
        logs = []
        for r in rows:
            status_v = r[4] or ""
            level = "ERROR" if status_v == "failed" else "INFO"
            cnt = r[5] if r[5] is not None else 0
            if status_v == "completed":
                msg = f"采集关键词「{r[3] or '-'}」完成，新增 {cnt} 条"
            elif status_v == "failed":
                msg = f"采集关键词「{r[3] or '-'}」失败：{(r[6] or '')[:120]}"
            else:
                msg = f"关键词「{r[3] or '-'}」 {status_v}"
            logs.append({
                "id": r[0], "channel": r[1] or "-", "device_id": r[2] or "-",
                "level": level, "message": msg,
                "created_at": r[7].isoformat() if r[7] else "",
            })
        return {"logs": logs, "channels": [c[0] for c in chans]}
    finally:
        db.close()


# ==================== 关键词管理 API ====================
_DEFAULT_KW_CHANNELS = ["souyisou", "sogou"]


def _sync_kw_channels(cur, keyword: str, channels: list, cycles: dict = None):
    """同步关键词分组→keyword_channel_state：删除不再属于的渠道行、为新渠道建行(立即可采)。

    channels 为该词归属的渠道集合；cycles 可选 {channel: minutes} 设词×渠道专属周期。
    与外层共享事务（不单独 commit），由调用方统一提交。
    """
    cur.execute("SELECT id FROM keywords WHERE keyword = %s", (keyword,))
    r = cur.fetchone()
    if not r:
        return
    kid = r[0]
    channels = [c for c in (channels or []) if c]
    if channels:
        cur.execute(
            "DELETE FROM keyword_channel_state WHERE keyword_id = %s AND channel <> ALL(%s)",
            (kid, channels),
        )
    else:
        cur.execute("DELETE FROM keyword_channel_state WHERE keyword_id = %s", (kid,))
    for ch in channels:
        cyc = (cycles or {}).get(ch)
        cur.execute(
            """
            INSERT INTO keyword_channel_state
                (keyword_id, channel, status, next_collect_time, update_cycle_minutes)
            VALUES (%s, %s, 'pending', NOW(), %s)
            ON CONFLICT (keyword_id, channel) DO UPDATE SET
                update_cycle_minutes = COALESCE(EXCLUDED.update_cycle_minutes,
                                                keyword_channel_state.update_cycle_minutes)
            """,
            (kid, ch, cyc),
        )


@app.post("/api/v1/keywords/")
async def create_keywords(keywords: List[KeywordCreate], current_user: dict = Depends(require_admin)):
    """
    批量创建关键词
    
    POST /api/v1/keywords/
    Body: [{"keyword": "人工智能", "category": "行业趋势"}, ...]
    """
    if not keywords:
        raise HTTPException(status_code=400, detail="keywords 不能为空")
    
    # 本地简化模式：直接写入数据库
    from ..db_connector import DatabaseConnector
    
    db = DatabaseConnector()
    cur = db.cursor()
    
    count = 0
    kw_list = [k.keyword for k in keywords]
    default_category = keywords[0].category if len(keywords) == 1 else None
    
    for kw_obj in keywords:
        try:
            chans = kw_obj.channels if kw_obj.channels is not None else _DEFAULT_KW_CHANNELS
            chans = [c for c in chans if c] or _DEFAULT_KW_CHANNELS
            # 插入或更新（含分组 channels）
            cur.execute("""
                INSERT INTO keywords (keyword, category, status, enabled, channels, created_at)
                VALUES (%s, %s, 'pending', TRUE, %s, NOW())
                ON CONFLICT (keyword) DO UPDATE
                SET category = EXCLUDED.category, channels = EXCLUDED.channels
            """, (kw_obj.keyword, kw_obj.category or default_category, chans))
            # 同步渠道调度行（分组分发的关键）
            _sync_kw_channels(cur, kw_obj.keyword, chans)
            count += 1
            db.commit()
        except Exception as e:
            log.error(f"插入关键词 {kw_obj.keyword} 失败：{e}")
            db.rollback()
    
    db.close()
    
    return {
        "status": "success",
        "count": count,
        "total": len(kw_list),
        "message": f"成功添加 {count} 个关键词（本地模式）"
    }

@app.get("/api/v1/keywords/")
async def list_keywords(
    status: Optional[str] = None,
    enabled: Optional[bool] = None,
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0)
):
    """
    获取关键词列表
    
    GET /api/v1/keywords/?status=pending&enabled=true&limit=50
    """
    from ..db_connector import DatabaseConnector
    
    db = DatabaseConnector()
    cur = db.cursor()
    
    query = """
        SELECT k.*, COALESCE((
            SELECT json_object_agg(s.channel, json_build_object(
                'status', s.status,
                'next_collect_time', s.next_collect_time,
                'last_count', s.last_count,
                'update_cycle_minutes', s.update_cycle_minutes))
            FROM keyword_channel_state s WHERE s.keyword_id = k.id
        ), '{}'::json) AS channel_state
        FROM keywords k WHERE 1=1
    """
    params = []
    
    if status:
        query += " AND status = %s"
        params.append(status)
    
    if enabled is not None:
        query += " AND enabled = %s"
        params.append(enabled)
    
    query += f" ORDER BY weight DESC, created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    
    cur.execute(query, params)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    
    db.close()
    
    return {
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "data": [dict(zip(cols, row)) for row in rows]
    }

@app.put("/api/v1/keywords/{keyword}")
async def update_keyword(keyword: str, update: KeywordUpdate, current_user: dict = Depends(require_admin)):
    """
    更新单个关键词
    
    PUT /api/v1/keywords/人工智能
    Body: {"enabled": false, "status": "failed"}
    """
    from ..db_connector import DatabaseConnector
    
    db = DatabaseConnector()
    cur = db.cursor()
    
    fields = []
    params = []
    
    if update.enabled is not None:
        fields.append("enabled = %s")
        params.append(update.enabled)
    
    if update.status:
        fields.append("status = %s")
        params.append(update.status)
    
    if update.update_cycle_minutes:
        fields.append("update_cycle_minutes = %s")
        params.append(update.update_cycle_minutes)
    if update.channels is not None:
        fields.append("channels = %s")
        params.append([c for c in update.channels if c] or _DEFAULT_KW_CHANNELS)
    
    need_sync = (update.channels is not None) or (update.cycles is not None)
    if not fields and not need_sync:
        raise HTTPException(status_code=400, detail="至少提供一个更新字段")
    
    try:
        if fields:
            params.append(keyword)
            cur.execute(f"UPDATE keywords SET {', '.join(fields)} WHERE keyword = %s", params)
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"关键词 '{keyword}' 不存在")
        # 改分组或设词×渠道周期时，同步 keyword_channel_state
        if need_sync:
            if update.channels is not None:
                chans = [c for c in update.channels if c] or _DEFAULT_KW_CHANNELS
            else:
                # 仅改周期：不动分组，沿用当前 channels
                cur.execute("SELECT channels FROM keywords WHERE keyword = %s", (keyword,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail=f"关键词 '{keyword}' 不存在")
                chans = row[0] or _DEFAULT_KW_CHANNELS
            _sync_kw_channels(cur, keyword, chans, update.cycles)
        
        db.commit()
        return {"status": "success"}
        
    finally:
        db.close()

@app.delete("/api/v1/keywords/{keyword}")
async def delete_keyword(keyword: str, current_user: dict = Depends(require_admin)):
    """删除关键词 (软删除，设置 enabled=false)"""
    from ..db_connector import DatabaseConnector
    
    db = DatabaseConnector()
    cur = db.cursor()
    
    try:
        cur.execute("""
            UPDATE keywords SET enabled = FALSE, status = 'archived'
            WHERE keyword = %s
        """, (keyword,))
        
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"关键词 '{keyword}' 不存在")
        
        db.commit()
        return {"status": "deleted", "keyword": keyword}
    finally:
        db.close()

# ==================== 任务调度 API ====================

@app.post("/api/v1/tasks/claim")
async def claim_task(request: TaskClaimRequest):
    """
    VM 端调用：领取关键词任务
    
    POST /api/v1/tasks/claim
    Body: {"channel": "wechat_pc", "vm_instance_id": "vm-001", "max_keywords": 10}
    
    Returns: [keyword1, keyword2, ...]
    """
    # 本地简化模式：提示需要完整环境
    return {
        "status": "info",
        "message": "此功能需要在生产级环境下使用 (PostgreSQL + Redis)",
        "details": "请启动 Docker Compose 以启用分布式任务调度",
        "keywords": [],
        "count": 0
    }

@app.post("/api/v1/tasks/result")
async def report_task_result(request: TaskResultReport):
    """
    VM 端调用：上报采集结果
    
    POST /api/v1/tasks/result
    Body: {"keyword": "评选征集", "articles_count": 36, "success": true, "error_message": null}
    """
    # 本地简化模式：提示需要完整环境
    return {
        "status": "info",
        "message": "此功能需要在生产级环境下使用 (PostgreSQL + Redis)",
        "details": "请启动 Docker Compose 以启用分布式任务调度",
        "success": False
    }

@app.get("/api/v1/tasks/claimed")
async def get_claimed_tasks():
    """
    获取当前所有 VM 正在领取的关键词
    GET /api/v1/tasks/claimed
    """
    # 本地简化模式
    return {
        "total": 0,
        "keywords": [],
        "mode": "local_simplified",
        "message": "此功能需要在生产级环境下使用 (PostgreSQL + Redis)"
    }

# ==================== 统计分析 API ====================

@app.get("/api/v1/stats/dashboard")
async def get_dashboard_stats(request: Request):
    """
    仪表盘统计数据（公海/AI活动库/我的活动库 各视图共用）
    
    GET /api/v1/stats/dashboard
    """
    from ..db_connector import DatabaseConnector
    
    db = DatabaseConnector()
    stats = {}
    
    # 当前用户 ID（用于个人状态统计）
    me_id = None
    try:
        me = current_user_optional(request)
        if me:
            me_id = me["id"]
    except Exception:
        pass
    
    # ---- 关键词/采集侧（原有）----
    stats["total_keywords"] = db.execute_query("SELECT COUNT(*) FROM keywords")[0][0]
    stats["status_distribution"] = dict(
        db.execute_query("SELECT status, COUNT(*) FROM keywords GROUP BY status")
    )
    today_articles = db.execute_query(
        "SELECT SUM(articles_count) FROM collect_tasks WHERE DATE(start_time) = CURRENT_DATE"
    )[0][0]
    stats["today_articles_collected"] = today_articles or 0
    stats["active_vm_instances"] = db.execute_query(
        "SELECT COUNT(DISTINCT vm_instance) FROM collect_tasks "
        "WHERE start_time > NOW() - INTERVAL '1 hour'"
    )[0][0]
    
    # ---- 线索侧（看板卡片）----
    stats["total_leads"] = db.execute_query(
        "SELECT COUNT(*) FROM qualified_leads "
        "WHERE llm_status='done' AND has_lead_value=TRUE"
    )[0][0]
    # 今日入库
    stats["today_count"] = db.execute_query(
        "SELECT COUNT(*) FROM qualified_leads "
        "WHERE llm_status='done' AND has_lead_value=TRUE "
        "AND created_at >= CURRENT_DATE"
    )[0][0]
    # 昨日入库（[CURRENT_DATE-1, CURRENT_DATE)）
    stats["yesterday_count"] = db.execute_query(
        "SELECT COUNT(*) FROM qualified_leads "
        "WHERE llm_status='done' AND has_lead_value=TRUE "
        "AND created_at >= CURRENT_DATE - INTERVAL '1 day' AND created_at < CURRENT_DATE"
    )[0][0]
    # AI 活动库数量（有投票 + 未开始，且当前用户未入库）
    if me_id:
        stats["ai_library_count"] = db.execute_query(
            "SELECT COUNT(*) FROM qualified_leads q "
            "LEFT JOIN lead_user_state s ON s.lead_id = q.id AND s.user_id = %s "
            "WHERE q.is_online_voting = TRUE "
            "AND q.llm_status='done' AND q.has_lead_value=TRUE "
            "AND COALESCE(q.activity_status, '') NOT IN ('进行中', '已结束') "
            "AND COALESCE(s.in_library, FALSE) = FALSE",
            (me_id,)
        )[0][0]
        # 当前用户已处理数
        stats["processed_count"] = db.execute_query(
            "SELECT COUNT(*) FROM lead_user_state WHERE user_id = %s AND processed = TRUE",
            (me_id,)
        )[0][0]
        # 我的活动库数量
        stats["library_count"] = db.execute_query(
            "SELECT COUNT(*) FROM lead_user_state WHERE user_id = %s AND in_library = TRUE",
            (me_id,)
        )[0][0]
        # 我的活动库中已处理的
        stats["library_processed"] = db.execute_query(
            "SELECT COUNT(*) FROM lead_user_state WHERE user_id = %s AND in_library = TRUE AND processed = TRUE",
            (me_id,)
        )[0][0]
        # 我的活动库中未处理的（已处理 + 未处理 = 活动库数量）
        stats["library_unprocessed"] = db.execute_query(
            "SELECT COUNT(*) FROM lead_user_state "
            "WHERE user_id = %s AND in_library = TRUE AND COALESCE(processed, FALSE) = FALSE",
            (me_id,)
        )[0][0]
        # AI活动库中已处理的（与 ai_library_count 同口径：含 in_library=FALSE，保证已处理+未处理=数量）
        stats["ai_library_processed"] = db.execute_query(
            "SELECT COUNT(*) FROM qualified_leads q "
            "JOIN lead_user_state s ON s.lead_id = q.id AND s.user_id = %s "
            "WHERE q.is_online_voting = TRUE "
            "AND q.llm_status='done' AND q.has_lead_value=TRUE "
            "AND COALESCE(q.activity_status, '') NOT IN ('进行中', '已结束') "
            "AND COALESCE(s.in_library, FALSE) = FALSE "
            "AND s.processed = TRUE",
            (me_id,)
        )[0][0]
        # AI活动库中未处理的（同口径）
        stats["ai_library_unprocessed"] = db.execute_query(
            "SELECT COUNT(*) FROM qualified_leads q "
            "LEFT JOIN lead_user_state s ON s.lead_id = q.id AND s.user_id = %s "
            "WHERE q.is_online_voting = TRUE "
            "AND q.llm_status='done' AND q.has_lead_value=TRUE "
            "AND COALESCE(q.activity_status, '') NOT IN ('进行中', '已结束') "
            "AND COALESCE(s.in_library, FALSE) = FALSE "
            "AND COALESCE(s.processed, FALSE) = FALSE",
            (me_id,)
        )[0][0]
        # AI活动库今日入库（同口径 + 今日创建）
        stats["ai_library_today"] = db.execute_query(
            "SELECT COUNT(*) FROM qualified_leads q "
            "LEFT JOIN lead_user_state s ON s.lead_id = q.id AND s.user_id = %s "
            "WHERE q.is_online_voting = TRUE "
            "AND q.llm_status='done' AND q.has_lead_value=TRUE "
            "AND COALESCE(q.activity_status, '') NOT IN ('进行中', '已结束') "
            "AND COALESCE(s.in_library, FALSE) = FALSE "
            "AND q.created_at >= CURRENT_DATE",
            (me_id,)
        )[0][0]
        # AI活动库昨日入库（同口径 + 昨日创建 [CURRENT_DATE-1, CURRENT_DATE)）
        stats["ai_library_yesterday"] = db.execute_query(
            "SELECT COUNT(*) FROM qualified_leads q "
            "LEFT JOIN lead_user_state s ON s.lead_id = q.id AND s.user_id = %s "
            "WHERE q.is_online_voting = TRUE "
            "AND q.llm_status='done' AND q.has_lead_value=TRUE "
            "AND COALESCE(q.activity_status, '') NOT IN ('进行中', '已结束') "
            "AND COALESCE(s.in_library, FALSE) = FALSE "
            "AND q.created_at >= CURRENT_DATE - INTERVAL '1 day' AND q.created_at < CURRENT_DATE",
            (me_id,)
        )[0][0]
    else:
        stats["ai_library_count"] = 0
        stats["processed_count"] = 0
        stats["library_count"] = 0
        stats["library_processed"] = 0
        stats["library_unprocessed"] = 0
        stats["ai_library_processed"] = 0
        stats["ai_library_unprocessed"] = 0
        stats["ai_library_today"] = 0
        stats["ai_library_yesterday"] = 0
    
    return stats

@app.get("/api/v1/stats/channel_performance")
async def get_channel_performance(days: int = Query(default=7)):
    """
    各渠道性能统计
    
    GET /api/v1/stats/channel_performance?days=7
    """
    from ..db_connector import DatabaseConnector
    
    db = DatabaseConnector()
    cur = db.cursor()
    
    cur.execute("""
        SELECT 
            channel,
            COUNT(*) as task_count,
            SUM(articles_count) as total_articles,
            AVG(articles_count::numeric) as avg_per_task,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
            ROUND(AVG(EXTRACT(EPOCH FROM (end_time - start_time)))/60, 2) as avg_duration_min
        FROM collect_tasks
        WHERE start_time > NOW() - INTERVAL '%s days'
        GROUP BY channel
        ORDER BY total_articles DESC
    """, (days,))
    
    results = cur.fetchall()
    db.close()
    
    return {
        "days": days,
        "channels": [dict(r) for r in results]
    }

# ==================== 关键词效果统计 API ====================

@app.get("/api/v1/stats/keywords/basic")
async def get_keyword_stats_basic():
    """关键词效果统计（概览）。

    GET /api/v1/stats/keywords/basic

    读 kw_stat_basic 视图：每个关键词的触达文章数/线索数/优质率/意图事件数/
    近7天30天/平均分，用于横向对比「哪些关键词覆盖广、采集质量高」。
    """
    from ..db_connector import DatabaseConnector

    cols = ["keyword", "articles_count", "leads_count", "avg_score",
            "excellent_rate", "intent_events", "recent_30d", "recent_7d",
            "last_lead_date"]
    db = DatabaseConnector()
    rows = db.execute_query(f"""
        SELECT {", ".join(cols)}
        FROM kw_stat_basic
        ORDER BY leads_count DESC, articles_count DESC
    """)
    return {"data": [dict(zip(cols, r)) for r in rows]}


@app.get("/api/v1/stats/keywords/intent")
async def get_keyword_stats_intent(keyword: Optional[str] = None):
    """关键词意图分布（可按关键词过滤）。

    GET /api/v1/stats/keywords/intent?keyword=人工智能

    读 kw_stat_intent 视图：某关键词在各意图类别（评选/投票/征集/活动…）下的
    条数/平均分/优质数，用于下钻看某个词到底采到什么样的活动。
    """
    from ..db_connector import DatabaseConnector

    cols = ["keyword", "category", "count", "avg_score",
            "excellent_rate", "excellent_count"]
    db = DatabaseConnector()
    if keyword:
        rows = db.execute_query(f"""
            SELECT {", ".join(cols)}
            FROM kw_stat_intent
            WHERE keyword = %s
            ORDER BY count DESC NULLS LAST
        """, (keyword,))
    else:
        rows = db.execute_query(f"""
            SELECT {", ".join(cols)}
            FROM kw_stat_intent
            ORDER BY keyword, count DESC NULLS LAST
        """)
    return {"data": [dict(zip(cols, r)) for r in rows]}

# ==================== 线索反馈 API ====================

@app.post("/api/v1/leads/{lead_id}/feedback")
async def submit_feedback(lead_id: int, feedback: dict):
    """
    提交人工标注反馈（lead_id = qualified_leads.id）。

    POST /api/v1/leads/{lead_id}/feedback
    Body: {"was_relevant": true, "corrected_category": "评选", "tags": ["高价值"]}

    写入 lead_feedback 标注历史，并回写 qualified_leads.status（relevant/not_relevant），
    供看板刷新后展示。走 execute_write（DatabaseConnector 不提供 commit）。
    """
    from ..db_connector import DatabaseConnector

    was_relevant = feedback.get("was_relevant")
    if was_relevant is None:
        raise HTTPException(status_code=400, detail="was_relevant 参数必填")
    corrected_category = feedback.get("corrected_category")
    tags = feedback.get("tags") or None  # text[]：psycopg2 直接适配 Python list

    db = DatabaseConnector()
    try:
        db.execute_write("""
            INSERT INTO lead_feedback
                (lead_id, was_relevant, corrected_category, tags, mark_method)
            VALUES (%s, %s, %s, %s, 'manual')
        """, (lead_id, was_relevant, corrected_category, tags))

        new_status = "relevant" if was_relevant else "not_relevant"
        db.execute_write(
            "UPDATE qualified_leads SET status = %s, updated_at = NOW() WHERE id = %s",
            (new_status, lead_id),
        )
        return {"status": "feedback_received", "lead_id": lead_id, "new_status": new_status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==================== 站内 Bug 反馈 API（测试期）====================

# 反馈分类白名单：前端下拉与此一致。
_BUG_CATEGORIES = {"重复活动", "漏判", "误判为广告", "清洗错误", "需求", "其他"}
_BUG_STATUS = {"open", "resolved", "wontfix"}


@app.post("/api/v1/feedback")
async def create_bug_feedback(payload: dict, current_user: dict = Depends(get_current_user)):
    """测试员就地提交 bug 反馈（含“重复活动”）。所有登录用户可提交。

    POST /api/v1/feedback
    Body: {"category":"重复活动","description":"...","lead_ids":[1506,1513],"page_url":"/admin"}
    lead_ids 由前端自动带上（当前勾选的线索）；category 必填且在白名单内。
    """
    category = str(payload.get("category", "") or "").strip()
    if category not in _BUG_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"非法反馈分类：{category}")
    description = str(payload.get("description", "") or "").strip()
    page_url = str(payload.get("page_url", "") or "")[:500]
    raw_ids = payload.get("lead_ids") or []
    # 容错：只取能转 int 的，最多 200 个，避免异常输入
    lead_ids = []
    for x in raw_ids[:200]:
        try:
            lead_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not description and not lead_ids:
        raise HTTPException(status_code=400, detail="请填写描述或先勾选相关线索")

    db = DatabaseConnector()
    # 注：必须走 execute_write（会 commit）；execute_query 不提交，INSERT 会在连接归池时被回滚。
    db.execute_write(
        """
        INSERT INTO bug_feedback (category, description, lead_ids, page_url, created_by, created_by_name)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (category, description, lead_ids, page_url, current_user["id"], current_user["username"]),
    )
    return {"status": "ok"}


@app.get("/api/v1/feedback")
async def list_bug_feedback(status: Optional[str] = None, current_user: dict = Depends(require_admin)):
    """反馈列表（仅管理员）。status 可传 open/resolved/wontfix 筛选，缺省全部。"""
    db = DatabaseConnector()
    cols = ["id", "category", "description", "lead_ids", "page_url", "created_by_name",
            "status", "admin_note", "resolved_at", "created_at"]
    where, params = "", []
    if status in _BUG_STATUS:
        where = "WHERE status = %s"
        params.append(status)
    rows = db.execute_query(
        f"""SELECT id, category, description, lead_ids, page_url, created_by_name,
                  status, admin_note, resolved_at, created_at
           FROM bug_feedback {where}
           ORDER BY (status='open') DESC, created_at DESC
           LIMIT 500""",
        tuple(params) if params else None,
    )
    data = [dict(zip(cols, r)) for r in rows]
    counts = db.execute_query("SELECT status, COUNT(*) FROM bug_feedback GROUP BY status")
    summary = {s: 0 for s in _BUG_STATUS}
    for s, n in counts:
        summary[s] = n
    return {"data": data, "summary": summary}


@app.patch("/api/v1/feedback/{fid}")
async def resolve_bug_feedback(fid: int, payload: dict, current_user: dict = Depends(require_admin)):
    """标记反馈处理状态（仅管理员）。Body: {"status":"resolved|open|wontfix","admin_note":"..."}。"""
    status = str(payload.get("status", "") or "").strip()
    if status not in _BUG_STATUS:
        raise HTTPException(status_code=400, detail=f"非法状态：{status}")
    admin_note = str(payload.get("admin_note", "") or "")
    db = DatabaseConnector()
    if status == "open":
        aff = db.execute_write(
            "UPDATE bug_feedback SET status='open', resolved_by=NULL, resolved_at=NULL, admin_note=%s WHERE id=%s",
            (admin_note, fid))
    else:
        aff = db.execute_write(
            "UPDATE bug_feedback SET status=%s, resolved_by=%s, resolved_at=NOW(), admin_note=%s WHERE id=%s",
            (status, current_user["id"], admin_note, fid))
    if not aff:
        raise HTTPException(status_code=404, detail=f"未找到反馈 id={fid}")
    return {"status": "ok", "id": fid, "new_status": status}

# ==================== 设备监控 API ====================

# 在线判定阈值（秒）：last_heartbeat 超此视为离线。
_DEVICE_ONLINE_TIMEOUT = int(os.getenv("DEVICE_ONLINE_TIMEOUT", "180"))

@app.get("/api/v1/devices")
async def list_devices():
    """设备总览：每台设备的在线状态/当前在采词/今日采集量/任务数/成功率/连续失败/最近上报。

    GET /api/v1/devices
    在线判定：last_heartbeat 在 DEVICE_ONLINE_TIMEOUT 秒内。
    """
    from ..db_connector import DatabaseConnector
    db = DatabaseConnector()
    cols = ["device_id", "device_type", "channel", "current_keyword", "last_heartbeat",
            "is_online", "today_tasks", "today_articles", "today_success", "today_fail", "last_report"]
    rows = db.execute_query("""
        SELECT d.device_id, d.device_type, d.channel, d.current_keyword, d.last_heartbeat,
               (d.last_heartbeat IS NOT NULL AND d.last_heartbeat > NOW() - make_interval(secs => %s)) AS is_online,
               COUNT(ct.id) AS today_tasks,
               COALESCE(SUM(ct.articles_count), 0) AS today_articles,
               COUNT(ct.id) FILTER (WHERE ct.status='completed') AS today_success,
               COUNT(ct.id) FILTER (WHERE ct.status='failed') AS today_fail,
               MAX(ct.start_time) AS last_report
        FROM devices d
        LEFT JOIN collect_tasks ct
               ON ct.device_id = d.device_id AND ct.start_time >= CURRENT_DATE
        GROUP BY d.device_id, d.device_type, d.channel, d.current_keyword, d.last_heartbeat
        ORDER BY is_online DESC, d.device_id
    """, (_DEVICE_ONLINE_TIMEOUT,))
    data = [dict(zip(cols, r)) for r in rows]
    # 连续失败流取自 Redis
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        sched = DistributedTaskScheduler.from_env()
        for d in data:
            v = sched.redis.get(f"wxsearch:device:fail_streak:{d['device_id']}")
            d["fail_streak"] = int(v) if v else 0
    except Exception:
        for d in data:
            d["fail_streak"] = 0
    online = sum(1 for d in data if d["is_online"])
    return {
        "data": data,
        "summary": {
            "total": len(data), "online": online, "offline": len(data) - online,
            "today_articles": sum(int(d["today_articles"] or 0) for d in data),
            "abnormal": sum(1 for d in data if (not d["is_online"]) or int(d.get("fail_streak", 0)) >= 3),
        },
    }


@app.get("/api/v1/devices/{device_id}/keywords")
async def device_keywords(device_id: str, limit: int = Query(default=100, le=500)):
    """某设备采过哪些关键词、各多少条、最近时间（读 collect_tasks）。

    GET /api/v1/devices/{device_id}/keywords
    """
    from ..db_connector import DatabaseConnector
    db = DatabaseConnector()
    cols = ["keyword", "tasks", "articles", "success", "fail", "last_time"]
    rows = db.execute_query("""
        SELECT k.keyword,
               COUNT(ct.id) AS tasks,
               COALESCE(SUM(ct.articles_count),0) AS articles,
               COUNT(ct.id) FILTER (WHERE ct.status='completed') AS success,
               COUNT(ct.id) FILTER (WHERE ct.status='failed') AS fail,
               MAX(ct.start_time) AS last_time
        FROM collect_tasks ct
        JOIN keywords k ON k.id = ct.keyword_id
        WHERE ct.device_id = %s
        GROUP BY k.keyword
        ORDER BY last_time DESC NULLS LAST
        LIMIT %s
    """, (device_id, limit))
    return {"device_id": device_id, "data": [dict(zip(cols, r)) for r in rows]}

# ==================== 管理界面 ====================

# 模板目录（/admin 直接读 HTML 文件返回，无需 Jinja2，免装依赖）
templates_dir = Path(__file__).parent.parent / "templates"

# 静态资源（渠道图标等）：挂载到 /static，供看板 <img src="/static/icons/..."> 引用。
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/admin", response_class=HTMLResponse)
async def leads_dashboard(request: Request):
    """
    线索管理后台界面
    """
    # 直接返回 HTML 文件内容 (简化方式)
    template_path = templates_dir / "leads_dashboard.html"
    if template_path.exists():
        html_content = template_path.read_text(encoding='utf-8')
        return HTMLResponse(content=html_content, status_code=200)
    else:
        return HTMLResponse(content="Template not found", status_code=404)

@app.get("/admin/ai_library", response_class=HTMLResponse)
async def ai_activity_library(request: Request):
    """AI 活动库（复用线索看板页；前端按 /admin/ai_library 路径预设‘已清洗且明确有线上投票、
    且活动状态非进行中/已结束（LLM 判定的活动状态）’筛选）。"""
    template_path = templates_dir / "leads_dashboard.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding='utf-8'), status_code=200)
    return HTMLResponse(content="Template not found", status_code=404)


@app.get("/admin/library", response_class=HTMLResponse)
async def my_activity_library(request: Request):
    """我的活动库（复用线索看板页；前端按 /admin/library 路径预设 in_library 筛选）。"""
    template_path = templates_dir / "leads_dashboard.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding='utf-8'), status_code=200)
    return HTMLResponse(content="Template not found", status_code=404)


@app.get("/admin/organizers", response_class=HTMLResponse)
async def organizers_page(request: Request):
    """主办方库页（独立业务模块，所有角色可见）。"""
    template_path = templates_dir / "organizers_dashboard.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding='utf-8'), status_code=200)
    return HTMLResponse(content="Template not found", status_code=404)


@app.get("/admin/keywords", response_class=HTMLResponse)
async def keywords_dashboard(request: Request):
    """关键词效果统计报表页（直接读 HTML 返回）。"""
    _u = current_user_optional(request)
    if not _u:
        return RedirectResponse("/login", status_code=302)
    if _u["role"] == "member":
        return RedirectResponse("/admin", status_code=302)
    template_path = templates_dir / "keywords_dashboard.html"
    if template_path.exists():
        html_content = template_path.read_text(encoding='utf-8')
        return HTMLResponse(content=html_content, status_code=200)
    else:
        return HTMLResponse(content="Template not found", status_code=404)

@app.get("/admin/settings", response_class=HTMLResponse)
async def system_settings_page(request: Request):
    """系统设置页（LLM 模型配置 + 清洗提示词，直接读 HTML 返回）。"""
    _u = current_user_optional(request)
    if not _u:
        return RedirectResponse("/login", status_code=302)
    if _u["role"] == "member":
        return RedirectResponse("/admin", status_code=302)
    template_path = templates_dir / "system_settings.html"
    if template_path.exists():
        html_content = template_path.read_text(encoding='utf-8')
        return HTMLResponse(content=html_content, status_code=200)
    else:
        return HTMLResponse(content="Template not found", status_code=404)

@app.get("/admin/collection", response_class=HTMLResponse)
async def collection_settings_page(request: Request):
    """采集设置页（采集关键词 + 搜一搜/搜狗采集参数，直接读 HTML 返回）。"""
    _u = current_user_optional(request)
    if not _u:
        return RedirectResponse("/login", status_code=302)
    if _u["role"] == "member":
        return RedirectResponse("/admin", status_code=302)
    template_path = templates_dir / "collection_settings.html"
    if template_path.exists():
        html_content = template_path.read_text(encoding='utf-8')
        return HTMLResponse(content=html_content, status_code=200)
    else:
        return HTMLResponse(content="Template not found", status_code=404)

@app.get("/admin/devices", response_class=HTMLResponse)
async def devices_dashboard_page(request: Request):
    """设备监控页（各采集设备在线/采集量/健康，直接读 HTML 返回）。"""
    _u = current_user_optional(request)
    if not _u:
        return RedirectResponse("/login", status_code=302)
    if _u["role"] == "member":
        return RedirectResponse("/admin", status_code=302)
    template_path = templates_dir / "devices_dashboard.html"
    if template_path.exists():
        html_content = template_path.read_text(encoding='utf-8')
        return HTMLResponse(content=html_content, status_code=200)
    else:
        return HTMLResponse(content="Template not found", status_code=404)

@app.get("/admin/sogou", response_class=HTMLResponse)
async def sogou_dashboard_page(request: Request):
    """搜狗采集管理页（开关/并发/代理/实时日志，仅管理员可见）。"""
    _u = current_user_optional(request)
    if not _u:
        return RedirectResponse("/login", status_code=302)
    if _u["role"] == "member":
        return RedirectResponse("/admin", status_code=302)
    template_path = templates_dir / "sogou_dashboard.html"
    if template_path.exists():
        html_content = template_path.read_text(encoding='utf-8')
        return HTMLResponse(content=html_content, status_code=200)
    else:
        return HTMLResponse(content="Template not found", status_code=404)

@app.get("/admin/feedback", response_class=HTMLResponse)
async def feedback_dashboard_page(request: Request):
    """反馈管理页（测试员提交的 bug，仅管理员可见）。"""
    _u = current_user_optional(request)
    if not _u:
        return RedirectResponse("/login", status_code=302)
    if _u["role"] == "member":
        return RedirectResponse("/admin", status_code=302)
    template_path = templates_dir / "feedback_dashboard.html"
    if template_path.exists():
        return HTMLResponse(content=template_path.read_text(encoding='utf-8'), status_code=200)
    return HTMLResponse(content="Template not found", status_code=404)

# ==================== 登录与用户管理 ====================

class LoginReq(BaseModel):
    username: str
    password: str


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user_optional(request):
        return RedirectResponse("/admin", status_code=302)
    p = templates_dir / "login.html"
    return HTMLResponse(p.read_text(encoding="utf-8")) if p.exists() else HTMLResponse("login page missing", status_code=404)


@app.post("/api/v1/login")
async def login_submit(payload: LoginReq):
    user = authenticate(payload.username, payload.password)
    if not user:
        return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)
    resp = JSONResponse({"status": "ok"})
    auth_token = make_session_token(user["id"])
    cookie_options = session_cookie_options()
    resp.set_cookie(COOKIE_NAME, auth_token, **cookie_options)
    if tenant_session_binding_enabled():
        validate_tenant_feature_flags()
        scope_token = make_tenant_scope_token(auth_token, user["id"])
        resp.set_cookie(TENANT_COOKIE_NAME, scope_token, **cookie_options)
    else:
        delete_options = dict(cookie_options)
        delete_options.pop("max_age", None)
        resp.delete_cookie(TENANT_COOKIE_NAME, **delete_options)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    delete_options = session_cookie_options()
    delete_options.pop("max_age", None)
    resp.delete_cookie(COOKIE_NAME, **delete_options)
    resp.delete_cookie(TENANT_COOKIE_NAME, **delete_options)
    return resp


@app.get("/api/v1/me")
async def api_me(current_user: dict = Depends(get_current_user)):
    return current_user


@app.get("/api/v1/menu_permissions")
async def get_menu_permissions(current_user: dict = Depends(get_current_user)):
    """菜单权限：返回菜单清单 + admin/member 当前可见性。

    sidebar.js 用它过滤菜单；权限管理 UI 用它渲染矩阵。任意登录用户可读（仅菜单键，非敏感）。
    """
    return {
        "items": [{"key": k, "label": lb} for k, lb in _MENU_ITEMS],
        "permissions": load_menu_perms(),
    }


@app.put("/api/v1/menu_permissions")
async def update_menu_permissions(update: MenuPermsUpdate, current_user: dict = Depends(require_super)):
    """保存菜单权限（仅超管）。super 不可配、恒全部可见；/admin 强制可见。"""
    try:
        save_menu_perms(update.permissions or {})
        return {"updated": True, "permissions": load_menu_perms()}
    except Exception as e:  # noqa: BLE001
        log.error(f"保存菜单权限失败：{e}")
        raise HTTPException(status_code=500, detail=f"保存失败：{str(e)}")


# 过滤模型总控台：可读写回 rule_config.json 的配置段白名单（段名→默认值工厂）。
_FILTER_CFG_SECTIONS = {
    "thresholds": dict, "priority": dict, "resource_level": dict,
    "cleaning": dict, "ended_title_signals": dict, "event_modifiers": dict,
    "negative_keywords": list,
}


@app.get("/api/v1/filter_config")
async def get_filter_config(current_user: dict = Depends(get_current_user)):
    """返回过滤模型总控台可配的配置段（仅白名单段，供配置页渲染）。"""
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        cfg = load_rule_config() or {}
    except Exception as e:  # noqa: BLE001
        log.error(f"读取过滤配置失败：{e}")
        cfg = {}
    return {k: cfg.get(k) for k in _FILTER_CFG_SECTIONS}


@app.put("/api/v1/filter_config")
async def update_filter_config(update: FilterConfigUpdate, current_user: dict = Depends(require_super)):
    """保存过滤模型配置（仅超管）。只写白名单段、类型校验；写回 rule_config.json 后重启 worker 生效。"""
    try:
        from wxsearch.ai_filters.rule_scorer import _DEFAULT_CONFIG_PATH
        path = os.getenv("RULE_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            cfg = {}
        payload = update.dict(exclude_none=True)
        for key, val in payload.items():
            expected = _FILTER_CFG_SECTIONS.get(key)
            if expected and isinstance(val, expected):
                cfg[key] = val
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return {"updated": True, "sections": list(payload.keys())}
    except Exception as e:  # noqa: BLE001
        log.error(f"保存过滤配置失败：{e}")
        raise HTTPException(status_code=500, detail=f"保存失败：{str(e)}")


class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/v1/change_password")
async def change_password(payload: ChangePwdReq, current_user: dict = Depends(get_current_user)):
    """修改当前登录用户密码：验原密码 → 写新密码(至少 6 位)。"""
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    db = DatabaseConnector()
    row = db.execute_query("SELECT password_hash FROM users WHERE id = %s", (current_user["id"],))
    if not row or not verify_password(payload.old_password, row[0][0]):
        raise HTTPException(status_code=400, detail="原密码错误")
    db.execute_write("UPDATE users SET password_hash = %s WHERE id = %s",
                     (hash_password(payload.new_password), current_user["id"]))
    return {"status": "ok"}


@app.get("/admin/users", response_class=HTMLResponse)
async def users_page(request: Request):
    user = current_user_optional(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user["role"] not in ("admin", "super"):
        return HTMLResponse("<h3 style='font-family:sans-serif'>需要管理员权限</h3>", status_code=403)
    p = templates_dir / "users_dashboard.html"
    return HTMLResponse(p.read_text(encoding="utf-8")) if p.exists() else HTMLResponse("page missing", status_code=404)


@app.get("/api/v1/users")
async def list_users(current_user: dict = Depends(require_admin)):
    db = DatabaseConnector()
    base = ("SELECT u.id, u.username, u.role, u.enabled, COALESCE(t.name,'') "
            "FROM users u LEFT JOIN teams t ON t.id = u.team_id")
    if current_user["role"] == "super":
        rows = db.execute_query(base + " ORDER BY u.id")
    else:
        rows = db.execute_query(base + " WHERE u.team_id = %s ORDER BY u.id", (current_user["team_id"],))
    cols = ["id", "username", "role", "enabled", "team_name"]
    teams = []
    if current_user["role"] == "super":
        teams = [{"id": r[0], "name": r[1]} for r in db.execute_query("SELECT id, name FROM teams ORDER BY id")]
    return {"data": [dict(zip(cols, r)) for r in rows], "me": current_user, "teams": teams}


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "member"
    team_id: Optional[int] = None


@app.post("/api/v1/users")
async def create_user(payload: UserCreate, current_user: dict = Depends(require_admin)):
    if not payload.username or not payload.password:
        raise HTTPException(status_code=400, detail="用户名和密码必填")
    role = payload.role if payload.role in ("admin", "member") else "member"
    team_id = payload.team_id if current_user["role"] == "super" else current_user["team_id"]
    db = DatabaseConnector()
    if db.execute_query("SELECT 1 FROM users WHERE username = %s", (payload.username,)):
        raise HTTPException(status_code=400, detail="用户名已存在")
    db.execute_write(
        "INSERT INTO users(username, password_hash, team_id, role) VALUES(%s,%s,%s,%s)",
        (payload.username, hash_password(payload.password), team_id, role),
    )
    return {"status": "ok"}


@app.patch("/api/v1/users/{uid}")
async def update_user(uid: int, payload: dict, current_user: dict = Depends(require_admin)):
    db = DatabaseConnector()
    row = db.execute_query("SELECT team_id FROM users WHERE id = %s", (uid,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if current_user["role"] != "super" and row[0][0] != current_user["team_id"]:
        raise HTTPException(status_code=403, detail="只能管理本团队成员")
    sets, params = [], []
    if "enabled" in payload:
        sets.append("enabled = %s"); params.append(bool(payload["enabled"]))
    if payload.get("role") in ("admin", "member"):
        sets.append("role = %s"); params.append(payload["role"])
    if payload.get("password"):
        sets.append("password_hash = %s"); params.append(hash_password(str(payload["password"])))
    if not sets:
        raise HTTPException(status_code=400, detail="无可更新字段")
    params.append(uid)
    db.execute_write(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", tuple(params))
    return {"status": "ok"}


class TeamCreate(BaseModel):
    name: str


@app.post("/api/v1/teams")
async def create_team(payload: TeamCreate, current_user: dict = Depends(require_admin)):
    if current_user["role"] != "super":
        raise HTTPException(status_code=403, detail="仅超级管理员可建团队")
    db = DatabaseConnector()
    db.execute_write("INSERT INTO teams(name) VALUES(%s) ON CONFLICT (name) DO NOTHING", (payload.name,))
    return {"status": "ok"}


# ==================== 人在回路：反馈复盘（阶段A地基，仅超管） ====================

@app.get("/api/v1/feedback/samples")
async def feedback_samples(limit: int = 50, current_user: dict = Depends(require_super)):
    """跨用户聚合个人反馈，产出正/负样本供复盘（只读，不改规则）。
    正信号=收藏或赞；负信号=回收站(隐藏)或反对；按去重用户数计。"""
    db = DatabaseConnector()
    rows = db.execute_query(
        """
        WITH agg AS (
            SELECT lead_id,
                COUNT(DISTINCT user_id) FILTER (WHERE in_library OR llm_feedback = 1) AS pos,
                COUNT(DISTINCT user_id) FILTER (WHERE hidden OR llm_feedback = -1) AS neg
            FROM lead_user_state GROUP BY lead_id
        )
        SELECT q.id, q.title, q.event_name, q.keyword, q.intent_category,
               q.resource_level, q.source_channel, a.pos, a.neg
        FROM agg a JOIN qualified_leads q ON q.id = a.lead_id
        WHERE a.pos > 0 OR a.neg > 0
        """
    )
    cols = ["id", "title", "event_name", "keyword", "intent_category", "resource_level", "source_channel", "pos", "neg"]
    data = [dict(zip(cols, r)) for r in rows]
    positive = sorted([d for d in data if d["pos"] > 0 and d["pos"] >= d["neg"]], key=lambda x: -x["pos"])[:limit]
    negative = sorted([d for d in data if d["neg"] > d["pos"]], key=lambda x: -x["neg"])[:limit]
    return {"positive": positive, "negative": negative,
            "summary": {"pos_total": len(positive), "neg_total": len(negative)}}


# --- 负向黑名单（阶段B）：读/写 rule_config.negative_keywords + 从负样本挖候选词 ---
_RULE_CFG_PATH = Path(__file__).parent.parent / "config" / "rule_config.json"


def _load_rule_cfg():
    return json.loads(_RULE_CFG_PATH.read_text(encoding="utf-8"))


def _save_rule_cfg(cfg):
    _RULE_CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _feedback_titles():
    """返回 (负样本标题列表, 正样本标题集合)。"""
    db = DatabaseConnector()
    rows = db.execute_query(
        """
        WITH agg AS (
            SELECT lead_id,
                COUNT(DISTINCT user_id) FILTER (WHERE in_library OR llm_feedback = 1) AS pos,
                COUNT(DISTINCT user_id) FILTER (WHERE hidden OR llm_feedback = -1) AS neg
            FROM lead_user_state GROUP BY lead_id
        )
        SELECT q.title, a.pos, a.neg FROM agg a JOIN qualified_leads q ON q.id = a.lead_id
        WHERE a.pos > 0 OR a.neg > 0
        """
    )
    neg = [r[0] or "" for r in rows if r[2] > r[1]]
    pos = set(r[0] or "" for r in rows if r[1] > 0 and r[1] >= r[2])
    return neg, pos


def _mine_candidates(neg_titles, pos_titles, blacklist, top=20):
    """从负样本标题挖 2-4gram：按文档频(出现在几条负标题)排序，排除正样本出现过的与已在黑名单的。"""
    import re
    from collections import Counter

    def norm(t):
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", (t or "").lower())

    pos_norm = [norm(t) for t in pos_titles]
    df = Counter()
    for t in neg_titles:
        s = norm(t)
        seen = set()
        for n in (2, 3, 4):
            for i in range(len(s) - n + 1):
                seen.add(s[i:i + n])
        for g in seen:
            df[g] += 1
    bl = set(blacklist)
    cands = []
    for g, d in df.most_common(300):
        if d < 2 or g in bl:
            continue
        if any(g in pn for pn in pos_norm):   # 正样本里出现过 → 不安全, 跳过
            continue
        cands.append({"word": g, "neg_docs": d})
        if len(cands) >= top:
            break
    return cands


@app.get("/api/v1/feedback/blacklist")
async def get_blacklist(current_user: dict = Depends(require_super)):
    cfg = _load_rule_cfg()
    bl = [w for w in (cfg.get("negative_keywords") or []) if w]
    neg, pos = _feedback_titles()
    return {"blacklist": bl, "candidates": _mine_candidates(neg, pos, bl)}


class BlacklistWord(BaseModel):
    word: str


@app.post("/api/v1/feedback/blacklist")
async def add_blacklist(payload: BlacklistWord, current_user: dict = Depends(require_super)):
    w = (payload.word or "").strip()
    if not w:
        raise HTTPException(status_code=400, detail="词不能为空")
    cfg = _load_rule_cfg()
    bl = [x for x in (cfg.get("negative_keywords") or []) if x]
    if w not in bl:
        bl.append(w)
        cfg["negative_keywords"] = bl
        _save_rule_cfg(cfg)
    return {"status": "ok", "blacklist": bl}


@app.delete("/api/v1/feedback/blacklist")
async def del_blacklist(word: str, current_user: dict = Depends(require_super)):
    cfg = _load_rule_cfg()
    bl = [x for x in (cfg.get("negative_keywords") or []) if x and x != word]
    cfg["negative_keywords"] = bl
    _save_rule_cfg(cfg)
    return {"status": "ok", "blacklist": bl}


# 导入线索管理路由
from . import leads
app.include_router(leads.router, prefix="/api/v1", tags=["leads"])

# 导入主办方库路由（独立业务模块）
from . import organizers
app.include_router(organizers.router, prefix="/api/v1", tags=["organizers"])

# 租户会话只在主应用路由全部声明后装配；开关关闭时中间件统一返回 404。
app.include_router(tenant_session_router)

# 租户审核路由保持注册但默认隐藏；启动门禁仍拒绝提前打开生产开关。
from .review_routes import router as tenant_review_router
app.include_router(tenant_review_router)

# 采集侧 -> 正式环境内部同步入口（时间戳 HMAC 鉴权，不使用登录 Cookie）。
from . import sync
app.include_router(sync.router, prefix="/internal/v1", tags=["internal-sync"])


# ==================== 启动命令 ====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)

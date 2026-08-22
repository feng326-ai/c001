"""通用大模型客户端（OpenAI 兼容）——一处实现，任意兼容服务通用。

定位：把「调用大模型」这件事收敛到一个地方。只要对方提供 OpenAI 兼容的
`/chat/completions` 接口（WebAI2API / DeepSeek / 通义千问 Qwen / OpenAI 官方…），
本客户端都能直接对接，业务层（评分、活动信息抽取）无需关心是哪一家。

配置优先级（环境变量 > rule_config.json 的 llm 块 > 内置默认）：
  - base_url : AI_BASE_URL / OPENAI_BASE_URL，默认 http://host.docker.internal:3000/v1
               （WebAI2API 默认端口；worker 在容器内用 host.docker.internal 访问宿主机）
  - api_key  : OPENAI_API_KEY / AI_API_KEY（密钥只从环境变量读，绝不写进配置或代码）
  - model    : AI_MODEL，默认 gpt-4o-mini
  - timeout / temperature / max_tokens：见 rule_config.json 的 llm 块

设计原则（与 AI 层一致）：
  - 失败即抛 LLMError，由上层业务决定「降级跳过 / 回退规则」，绝不阻断主流程；
  - 只做非流式请求（stream=False），实现简单、结果一次拿全；
  - 无新增第三方依赖（requests 已在 requirements.txt）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

# ==================== 全局状态 ====================

_CLIENT: Optional[LLMClient] = None  # 进程内单例，构造后复用
_CONFIG_PATH: Optional[str] = None   # rule_config.json 的绝对路径
_CONFIG_MTIME: float = 0             # 上次加载时的 mtime


class LLMError(RuntimeError):
    """大模型调用失败（网络/鉴权/超时/返回体异常）。上层据此降级或回退。"""


# 从返回文本里剥出 JSON 的辅助（模型常把 JSON 包在 ```json ... ``` 或夹带解释）。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)
_BRACE_RE = re.compile(r"\{.*\}", re.DOTALL)


def _cfg_llm() -> dict:
    """读取 rule_config.json 的 llm 块（读不到返回空 dict，用内置默认兜底）。
    
    每次调用时检测配置文件是否改动，如果变化则重置单例客户端。
    """
    # 检测并记录配置 mtime
    global _CONFIG_PATH, _CONFIG_MTIME
    path = os.getenv("RULE_CONFIG_PATH") or _CONFIG_PATH
    if not path:
        try:
            from wxsearch.ai_filters.rule_scorer import _DEFAULT_CONFIG_PATH
            path = _DEFAULT_CONFIG_PATH
            _CONFIG_PATH = path
        except Exception:  # noqa: BLE001
            path = ""
    
    if path and os.path.isfile(path):
        current_mtime = os.stat(path).st_mtime
        if current_mtime != _CONFIG_MTIME:
            _CONFIG_MTIME = current_mtime
            log.info(f"🔄 检测到规则配置文件变更 (mtime={current_mtime})，下次 get_client() 将重新初始化")
    
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        return (load_rule_config() or {}).get("llm", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def get_clean_enabled(default: bool = False) -> bool:
    """判断后台 LLM 清洗是否已启用。

    显式环境变量优先于规则文件，便于同一份 ``rule_config.json`` 在测试和正式
    环境采取不同运行角色。正式环境设置 ``LLM_CLEAN_ENABLED=false`` 时必须硬关闭，
    不能再被共享规则文件里的 ``clean_enabled=true`` 覆盖。

    Args:
        default: 环境变量和规则文件均未配置时的回退值
    Returns:
        True=开启；False=关闭
    """
    env_value = os.getenv("LLM_CLEAN_ENABLED")
    if env_value is not None:
        return str(env_value).strip().lower() in {"1", "true", "yes", "on"}

    cfg = _cfg_llm()
    val = cfg.get("clean_enabled", None)
    return default if val is None else bool(val)


def _secrets_path() -> str:
    """secrets.json 的绝对路径（与 rule_config.json 同目录，已被 .gitignore 忽略）。"""
    try:
        from wxsearch.ai_filters.rule_scorer import _DEFAULT_CONFIG_PATH
        return os.path.join(os.path.dirname(_DEFAULT_CONFIG_PATH), "secrets.json")
    except Exception:  # noqa: BLE001
        return ""


def load_secret_api_key() -> str:
    """从 secrets.json 读取 api_key（页面填写时存在这里）。读不到返回空串。"""
    path = _secrets_path()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return str((json.load(f) or {}).get("api_key", "") or "")
        except Exception:  # noqa: BLE001
            return ""
    return ""


def save_secret_api_key(api_key: str) -> None:
    """把 api_key 写入 secrets.json（绝不写进 rule_config.json，不入库）。"""
    path = _secrets_path()
    if not path:
        raise RuntimeError("无法定位 secrets.json 路径")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:  # noqa: BLE001
            data = {}
    data["api_key"] = str(api_key or "")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class LLMClient:
    """OpenAI 兼容的对话客户端。构造后可复用（内部持有 requests.Session）。"""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None,
                 model: Optional[str] = None, timeout: float = 60.0,
                 temperature: float = 0.2, max_tokens: int = 1024):
        cfg = _cfg_llm()
        self.base_url = (base_url
                         or os.getenv("AI_BASE_URL")
                         or os.getenv("OPENAI_BASE_URL")
                         or cfg.get("base_url")
                         or "http://host.docker.internal:3000/v1").rstrip("/")
        self.api_key = (api_key
                        or os.getenv("OPENAI_API_KEY")
                        or os.getenv("AI_API_KEY")
                        or load_secret_api_key()
                        or "")
        self.model = model or os.getenv("AI_MODEL") or cfg.get("model") or "gpt-4o-mini"
        self.timeout = float(cfg.get("timeout", timeout))
        self.temperature = float(cfg.get("temperature", temperature))
        self.max_tokens = int(cfg.get("max_tokens", max_tokens))
        self.use_json_format = True   # 优先尝试 response_format=json_object；端点不支持则自动关闭回退
        self._session = requests.Session()

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def chat(self, messages: List[dict], **override) -> str:
        """发一轮对话，返回助手回复的纯文本内容。失败抛 LLMError。"""
        payload = {
            "model": override.get("model", self.model),
            "messages": messages,
            "temperature": override.get("temperature", self.temperature),
            "max_tokens": override.get("max_tokens", self.max_tokens),
            "stream": False,
        }
        rf = override.get("response_format")
        if rf:
            payload["response_format"] = rf
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = self._session.post(
                self.endpoint, headers=headers,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise LLMError(f"请求失败（网络/超时）：{exc}") from exc

        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}：{resp.text[:300]}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"返回体解析失败：{exc}；原文：{resp.text[:300]}") from exc

        if not str(content or "").strip():
            raise LLMError("模型返回空内容")
        return content

    def chat_json(self, system: str, user: str, **override) -> dict:
        """要求模型只输出 JSON，返回解析后的 dict。容忍 ```json``` 包裹与前后解释文字。

        优先带 response_format=json_object（强约束 JSON）；若端点不支持（首次报错），
        自动回退为纯文本调用 + 正则剥壳，并记住后续不再尝试。
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if self.use_json_format and "response_format" not in override:
            try:
                text = self.chat(messages, response_format={"type": "json_object"}, **override)
            except LLMError as exc:
                # 仅当错误确属 response_format 不支持时才回退（避免把“空返回/超时”等瞬时错误
                # 误当成不支持而在同一次调用里重发——那样会加倍 LLM 负载、加剧降级）。
                msg = str(exc)
                unsupported = ("response_format" in msg) or ("json_object" in msg) or (
                    ("HTTP 400" in msg or "HTTP 422" in msg) and "format" in msg.lower())
                if not unsupported:
                    raise   # 空返回/超时等：直接抛，交给上层重试机制（不在同一次调用里重发）
                log.warning("端点不支持 response_format，回退纯文本+正则剥壳，后续不再尝试")
                text = self.chat(messages, **override)
                self.use_json_format = False
        else:
            text = self.chat(messages, **override)
        return _parse_json(text)

    def list_models(self) -> List[str]:
        """调用 OpenAI 兼容的 GET /models，返回可用模型 id 列表。失败抛 LLMError。"""
        url = f"{self.base_url}/models"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = self._session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"请求失败（网络/超时）：{exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}：{resp.text[:300]}")
        try:
            data = resp.json()
            items = data.get("data", data) if isinstance(data, dict) else data
            models = [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise LLMError(f"返回体解析失败：{exc}；原文：{resp.text[:300]}") from exc
        return sorted(set(models))


def _parse_json(text: str) -> dict:
    """从模型输出里稳健地解析出一个 JSON 对象。"""
    raw = str(text or "").strip()
    # 1) 直接尝试
    for candidate in (raw,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    # 2) 剥 ```json ... ``` 代码块
    m = _FENCE_RE.search(raw)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    # 3) 抓第一个 {...} 大括号块
    m = _BRACE_RE.search(raw)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    raise LLMError(f"未能从模型输出解析出 JSON：{raw[:300]}")


def reset_client() -> None:
    """强制重置 LLM 客户端单例，供设置保存后调用。"""
    global _CLIENT
    _CLIENT = None
    log.info("✅ 已重置 LLMClient 单例")


def get_client() -> LLMClient:
    """进程内单例（复用连接）。配置在进程启动时定型；改配置需重启 worker。
    
    每次调用时会检测配置文件是否变化；若变化则重新初始化客户端。
    """
    global _CLIENT
    
    # 先检测配置是否变化
    _cfg_llm()
    
    if _CLIENT is None:
        _CLIENT = LLMClient()
    return _CLIENT

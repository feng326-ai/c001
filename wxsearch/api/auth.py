"""多团队鉴权（第一期）：密码哈希 + hmac 自签名 Cookie 会话 + FastAPI 依赖。

无第三方依赖：密码用 hashlib.pbkdf2_hmac；会话用 hmac 签名 Cookie（存 user_id）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from fastapi import Request, HTTPException

from ..db_connector import DatabaseConnector

COOKIE_NAME = "wxsess"
TENANT_COOKIE_NAME = "wxscope"
SESSION_SECRET = os.getenv("SESSION_SECRET", "wx-dev-secret-change-me")
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(7 * 24 * 3600)))  # 7天
SESSION_CLOCK_SKEW = 60
MAX_SESSION_TOKEN_LENGTH = 768
_PBKDF2_ITER = 200_000


@dataclass(frozen=True)
class TenantScopeClaims:
    """Untrusted tenant candidate carried by the signed scope cookie.

    Membership id, role and permissions intentionally never enter the cookie;
    they are always reloaded from PostgreSQL before tenant work.
    """

    user_id: int
    session_id: str
    tenant_id: UUID | None
    issued_at: int
    expires_at: int


# ==================== 密码 ====================

def hash_password(plain: str) -> str:
    """返回 'pbkdf2$<iter>$<salt_hex>$<hash_hex>'（加随机 salt，绝不存明文）。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


# ==================== 会话 Cookie ====================

def _sign(payload: str) -> str:
    return hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session_token(uid: int) -> str:
    payload = f"{uid}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def parse_session_token(token: str):
    """校验签名与时效，返回 uid（int）或 None。"""
    try:
        if not token or len(token) > MAX_SESSION_TOKEN_LENGTH:
            return None
        uid_s, ts_s, sig = token.split(".")
        payload = f"{uid_s}.{ts_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        issued_at = int(ts_s)
        age = time.time() - issued_at
        if issued_at < 0 or age < -SESSION_CLOCK_SKEW:
            return None
        if SESSION_MAX_AGE > 0 and age > SESSION_MAX_AGE:
            return None
        return int(uid_s)
    except Exception:  # noqa: BLE001
        return None


def _scope_sign(payload: str) -> str:
    domain_payload = f"tenant-scope-v1:{payload}"
    return hmac.new(
        SESSION_SECRET.encode("utf-8"),
        domain_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_fingerprint(auth_token: str) -> str:
    return hashlib.sha256(auth_token.encode("utf-8")).hexdigest()[:32]


def _scope_max_age() -> int:
    configured = int(
        os.getenv(
            "TENANT_SCOPE_MAX_AGE",
            str(SESSION_MAX_AGE if SESSION_MAX_AGE > 0 else 24 * 3600),
        )
    )
    if configured <= 0:
        raise RuntimeError("TENANT_SCOPE_MAX_AGE must be a positive integer")
    return configured


def make_tenant_scope_token(
    auth_token: str,
    user_id: int,
    tenant_id: UUID | str | None = None,
    *,
    now: int | None = None,
    session_id: str | None = None,
) -> str:
    """Create a random, login-bound tenant scope candidate.

    ``tenant_id`` is still only a candidate. Every business operation must use
    ``tenant_transaction`` to re-authorize it against current database state.
    """

    if parse_session_token(auth_token) != user_id:
        raise ValueError("auth token does not match user")
    issued_at = int(time.time()) if now is None else int(now)
    if issued_at < 0:
        raise ValueError("scope issued_at must not be negative")
    scope_session_id = session_id or secrets.token_hex(16)
    if not secrets.compare_digest(
        scope_session_id,
        scope_session_id.lower(),
    ) or len(scope_session_id) != 32:
        raise ValueError("scope session_id must be 32 lowercase hex characters")
    try:
        int(scope_session_id, 16)
    except ValueError as error:
        raise ValueError(
            "scope session_id must be 32 lowercase hex characters"
        ) from error
    canonical_tenant = "-" if tenant_id is None else str(UUID(str(tenant_id)))
    expires_at = issued_at + _scope_max_age()
    payload = ".".join(
        (
            "v1",
            str(user_id),
            _auth_fingerprint(auth_token),
            scope_session_id,
            canonical_tenant,
            str(issued_at),
            str(expires_at),
        )
    )
    return f"{payload}.{_scope_sign(payload)}"


def parse_tenant_scope_token(
    token: str,
    auth_token: str,
    user_id: int,
    *,
    now: int | None = None,
) -> TenantScopeClaims | None:
    """Validate a scope token and its binding to the current login cookie."""

    try:
        if not token or len(token) > MAX_SESSION_TOKEN_LENGTH:
            return None
        (
            version,
            user_id_text,
            auth_fingerprint,
            session_id,
            tenant_text,
            issued_at_text,
            expires_at_text,
            signature,
        ) = token.split(".")
        payload = token.rsplit(".", 1)[0]
        if version != "v1" or not hmac.compare_digest(
            signature, _scope_sign(payload)
        ):
            return None
        if int(user_id_text) != user_id:
            return None
        if parse_session_token(auth_token) != user_id:
            return None
        if not hmac.compare_digest(
            auth_fingerprint, _auth_fingerprint(auth_token)
        ):
            return None
        if len(session_id) != 32 or session_id != session_id.lower():
            return None
        int(session_id, 16)
        issued_at = int(issued_at_text)
        expires_at = int(expires_at_text)
        current_time = int(time.time()) if now is None else int(now)
        if (
            issued_at < 0
            or issued_at > current_time + SESSION_CLOCK_SKEW
            or expires_at <= issued_at
            or expires_at - issued_at != _scope_max_age()
            or current_time > expires_at
        ):
            return None
        tenant_id = None if tenant_text == "-" else UUID(tenant_text)
        if tenant_id is not None and str(tenant_id) != tenant_text:
            return None
        return TenantScopeClaims(
            user_id=user_id,
            session_id=session_id,
            tenant_id=tenant_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def tenant_scope_csrf_token(scope_token: str) -> str:
    """Derive an opaque CSRF token that rotates with every scope cookie."""

    digest = hashlib.sha256(scope_token.encode("utf-8")).hexdigest()
    payload = f"tenant-scope-csrf-v1:{digest}"
    return hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def session_cookie_options() -> dict:
    """Return one canonical policy for setting both session cookies."""

    configured = os.getenv("SESSION_COOKIE_SECURE")
    if configured is None:
        secure = os.getenv("ENVIRONMENT", "development").lower() in {
            "production",
            "staging",
        }
    else:
        value = configured.strip().lower()
        if value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
            raise RuntimeError("SESSION_COOKIE_SECURE must be a boolean")
        secure = value in {"1", "true", "yes", "on"}
    return {
        "max_age": SESSION_MAX_AGE,
        "httponly": True,
        "secure": secure,
        "samesite": "lax",
        "path": "/",
    }


# ==================== 用户加载与依赖 ====================

def load_user(uid: int):
    """按 id 载入启用中的用户（含团队名）。返回 dict 或 None。"""
    db = DatabaseConnector()
    rows = db.execute_query(
        """
        SELECT u.id, u.username, u.role, u.team_id, u.enabled, COALESCE(t.name, '')
        FROM users u LEFT JOIN teams t ON t.id = u.team_id
        WHERE u.id = %s
        """,
        (uid,),
    )
    if not rows:
        return None
    r = rows[0]
    if not r[4]:  # enabled=False
        return None
    return {"id": r[0], "username": r[1], "role": r[2], "team_id": r[3], "team_name": r[5]}


def authenticate(username: str, password: str):
    """用户名+密码校验，成功返回用户 dict，失败返回 None。"""
    db = DatabaseConnector()
    rows = db.execute_query(
        "SELECT id, password_hash, enabled FROM users WHERE username = %s", (username,)
    )
    if not rows:
        return None
    uid, pwd_hash, enabled = rows[0]
    if not enabled or not verify_password(password, pwd_hash):
        return None
    return load_user(uid)


def current_user_optional(request: Request):
    """读 Cookie 会话，返回用户 dict 或 None（不抛异常，供页面重定向用）。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    uid = parse_session_token(token)
    if uid is None:
        return None
    return load_user(uid)


def get_current_user(request: Request):
    """FastAPI 依赖：要求已登录，否则 401（供 API 用）。"""
    user = current_user_optional(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话失效")
    return user


def require_admin(request: Request):
    """FastAPI 依赖：要求 admin 或 super 角色。"""
    user = get_current_user(request)
    if user["role"] not in ("admin", "super"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_super(request: Request):
    """FastAPI 依赖：仅超级管理员（采集设置/系统设置等敏感修改专用）。"""
    user = get_current_user(request)
    if user["role"] != "super":
        raise HTTPException(status_code=403, detail="仅超级管理员可操作")
    return user

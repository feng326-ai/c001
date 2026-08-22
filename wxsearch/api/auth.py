"""多团队鉴权（第一期）：密码哈希 + hmac 自签名 Cookie 会话 + FastAPI 依赖。

无第三方依赖：密码用 hashlib.pbkdf2_hmac；会话用 hmac 签名 Cookie（存 user_id）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import Request, HTTPException

from ..db_connector import DatabaseConnector

COOKIE_NAME = "wxsess"
SESSION_SECRET = os.getenv("SESSION_SECRET", "wx-dev-secret-change-me")
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(7 * 24 * 3600)))  # 7天
_PBKDF2_ITER = 200_000


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
        uid_s, ts_s, sig = token.split(".")
        payload = f"{uid_s}.{ts_s}"
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        if SESSION_MAX_AGE > 0 and (time.time() - int(ts_s)) > SESSION_MAX_AGE:
            return None
        return int(uid_s)
    except Exception:  # noqa: BLE001
        return None


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

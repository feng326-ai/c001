"""
数据库连接器单例
支持多环境配置 (开发/生产)
"""

import os
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from psycopg2 import InterfaceError, OperationalError
from typing import Iterator, Optional
from urllib.parse import urlsplit
from uuid import UUID

# 初始化日志
log = logging.getLogger(__name__)


class TenantAccessDenied(PermissionError):
    """The authenticated legacy user has no active access to the tenant."""


class TenantTransactionUsageError(RuntimeError):
    """A caller attempted to escape or nest a tenant transaction."""


@dataclass(frozen=True)
class TenantPrincipal:
    user_id: int
    user_public_id: UUID
    tenant_id: UUID
    membership_id: UUID
    role: str


@dataclass(frozen=True)
class TenantChoice:
    tenant_id: UUID
    code: str
    name: str
    membership_id: UUID
    role: str


class TenantDbTransaction:
    """Narrow fixed-connection interface yielded after tenant authorization.

    It intentionally exposes neither the raw connection nor commit/rollback.
    The owning context manager is the only transaction lifecycle authority.
    """

    def __init__(self, cursor, principal: TenantPrincipal):
        self._cursor = cursor
        self._closed = False
        self.principal = principal

    def _require_open(self) -> None:
        if self._closed:
            raise TenantTransactionUsageError("tenant transaction is closed")

    def execute_query(self, query: str, params: Optional[tuple] = None) -> list:
        self._require_open()
        self._cursor.execute(query, params)
        return self._cursor.fetchall()

    def execute_write(self, query: str, params: Optional[tuple] = None) -> int:
        self._require_open()
        self._cursor.execute(query, params)
        return self._cursor.rowcount

    def _close(self) -> None:
        self._closed = True


class DatabaseConnector:
    """数据库连接管理器 (线程安全)"""
    
    _instance = None
    _pool_init_lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        self._db_config = self._load_db_config()
        
        # 单例在 FastAPI 的并发请求中也会被复用，初始化池必须加锁；Celery prefork
        # 子进程各自延迟创建自己的池，避免共享父进程连接。
        if not hasattr(self, 'pool'):
            with self._pool_init_lock:
                if not hasattr(self, 'pool'):
                    from psycopg2 import pool as pg_pool
                    try:
                        maxconn = max(1, int(os.getenv("DB_POOL_MAX", "8")))
                    except (TypeError, ValueError):
                        maxconn = 8
                    try:
                        connect_timeout = max(1, int(os.getenv("DB_CONNECT_TIMEOUT", "5")))
                    except (TypeError, ValueError):
                        connect_timeout = 5
                    self.pool = pg_pool.ThreadedConnectionPool(
                        1, maxconn,
                        host=self._db_config["host"],
                        port=self._db_config["port"],
                        database=self._db_config["database"],
                        user=self._db_config["user"],
                        password=self._db_config["password"],
                        connect_timeout=connect_timeout,
                    )
                    self._local = threading.local()
                    log.info(
                        "PostgreSQL 连接池初始化成功：%s (max=%s)",
                        self._db_config["database"], maxconn,
                    )
    
    def _load_db_config(self) -> dict:
        """加载数据库配置。

        优先解析 DATABASE_URL（worker/beat 容器只注入了它、且未设 ENVIRONMENT，
        若不优先解析会 fallback 到 localhost/wx_search_dev 连不上真库）；
        解析不到再走 ENVIRONMENT + POSTGRES_* 逻辑。
        """

        url = os.getenv("DATABASE_URL")
        if url:
            p = urlsplit(url)
            return {
                "host": p.hostname or "localhost",
                "port": p.port or 5432,
                "database": (p.path or "/wx_search").lstrip("/") or "wx_search",
                "user": p.username or "admin",
                "password": p.password or "",
            }

        env = os.getenv("ENVIRONMENT", "development")
        
        if env == "production":
            # 生产环境配置
            return {
                "host": os.getenv("DB_HOST", "postgres"),
                "port": int(os.getenv("DB_PORT", "5432")),
                "database": os.getenv("POSTGRES_DB", "wx_search"),
                "user": os.getenv("POSTGRES_USER", "admin"),
                "password": os.getenv("POSTGRES_PASSWORD", "your_secure_password_here")
            }
        else:
            # 开发环境配置
            return {
                "host": os.getenv("DB_HOST", "localhost"),
                "port": int(os.getenv("DB_PORT", "5432")),
                "database": os.getenv("POSTGRES_DB", "wx_search_dev"),
                "user": os.getenv("POSTGRES_USER", "admin"),
                "password": os.getenv("POSTGRES_PASSWORD", "your_secure_password_here")
            }
    
    def cursor(self, cursor_factory=None):
        """获取游标 (自动从连接池借出)。

        借出的连接按线程登记，供后续 commit/rollback/close 使用；
        调用方用完必须调 close() 归还连接，否则会泄漏致连接池耗尽。
        """
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = self.pool.getconn()
        self._local.active_conn = conn
        if cursor_factory:
            return conn.cursor(cursor_factory=cursor_factory)
        return conn.cursor()

    def commit(self):
        """提交当前借出连接的事务。"""
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = getattr(self._local, "active_conn", None)
        if conn is not None:
            conn.commit()

    def rollback(self):
        """回滚当前借出连接的事务。"""
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = getattr(self._local, "active_conn", None)
        if conn is not None:
            conn.rollback()

    def _release_connection(self, conn, broken: bool = False) -> None:
        """回收连接；异常或失效连接从池中剔除，避免污染后续请求。"""
        if conn is None:
            return
        try:
            if not broken and not conn.closed:
                # SELECT 后连接通常仍处于事务中，归还前回滚以复位会话。
                conn.rollback()
            self.pool.putconn(conn, close=broken or bool(conn.closed))
        except Exception:  # noqa: BLE001
            log.warning("归还 PostgreSQL 连接失败，将关闭该连接", exc_info=True)
            try:
                self.pool.putconn(conn, close=True)
            except Exception:  # noqa: BLE001
                pass

    def _reject_autonomous_access_inside_tenant_transaction(self) -> None:
        if getattr(self._local, "tenant_transaction_active", False):
            raise TenantTransactionUsageError(
                "use the TenantDbTransaction methods inside tenant_transaction"
            )

    @staticmethod
    def _canonical_uuid(value: UUID | str) -> UUID:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise TenantAccessDenied("tenant access denied")
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError, TypeError) as error:
            raise TenantAccessDenied("tenant access denied") from error
        if str(parsed) != value.lower():
            raise TenantAccessDenied("tenant access denied")
        return parsed

    @staticmethod
    def _authenticated_user_id(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TenantAccessDenied("tenant access denied")
        return value

    @staticmethod
    def _load_enabled_public_id(cursor, authenticated_user_id: int) -> UUID:
        cursor.execute(
            """
            SELECT public_id
            FROM users
            WHERE id = %s AND enabled = TRUE
            FOR SHARE
            """,
            (authenticated_user_id,),
        )
        row = cursor.fetchone()
        if not row or row[0] is None:
            raise TenantAccessDenied("tenant access denied")
        try:
            return UUID(str(row[0]))
        except (ValueError, TypeError) as error:
            raise TenantAccessDenied("tenant access denied") from error

    @contextmanager
    def tenant_transaction(
        self,
        *,
        authenticated_user_id: int,
        requested_tenant_id: UUID | str,
    ) -> Iterator[TenantDbTransaction]:
        """Authorize and execute tenant work on one connection and transaction.

        This synchronous context must stay inside one service call and must not
        cross an ``await`` boundary. The legacy integer user id comes only from
        a verified server session; request-supplied public ids are never used.
        """

        user_id = self._authenticated_user_id(authenticated_user_id)
        tenant_id = self._canonical_uuid(requested_tenant_id)
        self._reject_autonomous_access_inside_tenant_transaction()

        conn = cursor = transaction = None
        broken = False
        commit_started = False
        self._local.tenant_transaction_active = True
        try:
            conn = self.pool.getconn()
            if conn.autocommit:
                conn.autocommit = False
            cursor = conn.cursor()
            user_public_id = self._load_enabled_public_id(cursor, user_id)
            cursor.execute(
                """
                SELECT membership_id, membership_role
                FROM app_list_active_tenants(%s)
                WHERE tenant_id = %s
                """,
                (str(user_public_id), str(tenant_id)),
            )
            membership = cursor.fetchone()
            if not membership:
                raise TenantAccessDenied("tenant access denied")
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                (str(tenant_id),),
            )
            cursor.fetchone()

            principal = TenantPrincipal(
                user_id=user_id,
                user_public_id=user_public_id,
                tenant_id=tenant_id,
                membership_id=UUID(str(membership[0])),
                role=str(membership[1]),
            )
            transaction = TenantDbTransaction(cursor, principal)
            yield transaction
            transaction._close()
            commit_started = True
            conn.commit()
        except BaseException as error:
            if transaction is not None:
                transaction._close()
            broken = bool(
                commit_started
                or isinstance(error, (OperationalError, InterfaceError))
                or (conn is not None and conn.closed)
            )
            if conn is not None and not conn.closed:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    broken = True
            raise
        finally:
            if transaction is not None:
                transaction._close()
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    broken = True
            self._local.tenant_transaction_active = False
            self._release_connection(conn, broken=broken)

    def list_active_tenants(
        self, *, authenticated_user_id: int
    ) -> list[TenantChoice]:
        """List only active memberships for a verified legacy session user."""

        user_id = self._authenticated_user_id(authenticated_user_id)
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = cursor = None
        broken = False
        try:
            conn = self.pool.getconn()
            if conn.autocommit:
                conn.autocommit = False
            cursor = conn.cursor()
            user_public_id = self._load_enabled_public_id(cursor, user_id)
            cursor.execute(
                """
                SELECT tenant_id, tenant_code, tenant_name,
                       membership_id, membership_role
                FROM public.app_list_active_tenants(%s)
                """,
                (str(user_public_id),),
            )
            return [
                TenantChoice(
                    tenant_id=UUID(str(row[0])),
                    code=str(row[1]),
                    name=str(row[2]),
                    membership_id=UUID(str(row[3])),
                    role=str(row[4]),
                )
                for row in cursor.fetchall()
            ]
        except BaseException as error:
            broken = bool(
                isinstance(error, (OperationalError, InterfaceError))
                or (conn is not None and conn.closed)
            )
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    broken = True
            self._release_connection(conn, broken=broken)
    
    def execute_query(self, query: str, params: Optional[tuple] = None) -> list:
        """
        执行查询并返回所有结果
        
        Args:
            query: SQL 语句
            params: 参数元组
        
        Returns:
            结果列表
        """
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = cur = None
        broken = False
        try:
            conn = self.pool.getconn()
            cur = conn.cursor()
            
            cur.execute(query, params)
            results = cur.fetchall()
            
            return results
        except Exception as e:
            broken = isinstance(e, (OperationalError, InterfaceError))
            log.error(f"查询失败：{query}, {e}")
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            self._release_connection(conn, broken=broken)
    
    def execute_write(self, query: str, params: Optional[tuple] = None) -> int:
        """
        执行写操作并返回影响行数
        
        Args:
            query: SQL 语句
            params: 参数元组
        
        Returns:
            受影响行数
        """
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = cur = None
        broken = False
        try:
            conn = self.pool.getconn()
            cur = conn.cursor()
            
            cur.execute(query, params)
            
            affected_rows = cur.rowcount
            conn.commit()
            
            return affected_rows
        except Exception as e:
            broken = isinstance(e, (OperationalError, InterfaceError))
            log.error(f"写入失败：{query}, {e}")
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
            self._release_connection(conn, broken=broken)
    
    def close(self):
        """将当前借出的连接归还连接池（配合 cursor() 使用）。

        `execute_query` / `execute_write` 自己回收连接；本方法只服务 legacy
        `cursor()` 调用。连接按线程登记，避免并发请求互相归还错误的连接。
        """
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = getattr(self._local, "active_conn", None)
        if conn is not None:
            self._release_connection(conn, broken=bool(conn.closed))
            self._local.active_conn = None
    
    def health_check(self) -> bool:
        """检查数据库连接是否可用"""
        self._reject_autonomous_access_inside_tenant_transaction()
        conn = None
        try:
            conn = self.pool.getconn()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            ok = cur.fetchone()[0] == 1
            cur.close()
            return ok
        except Exception:
            return False
        finally:
            self._release_connection(conn, broken=bool(conn and conn.closed))


# ==================== 快捷函数 ====================

def get_db_connector() -> DatabaseConnector:
    """获取单例实例"""
    return DatabaseConnector()

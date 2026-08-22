#!/usr/bin/env python3
"""
migrations/run.py: 迁移运行器
- 自动发现 docs/migrations/*.sql（按文件名排序）
- 根据 schema_migrations 跟踪表判断哪些已应用
- 逐个执行未应用的迁移，记录版本

用法：
    docker exec wxsearch_worker python -m wxsearch.migrations.run
或本地：
    cd g:/qoder/ss && python -m wxsearch.migrations.run
"""

import os
import sys
import hashlib
import psycopg2
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = BASE_DIR / "docs" / "migrations"

def get_db_connection():
    """从 DATABASE_URL 连接数据库 (兼容 Docker/本地)"""
    from wxsearch.tasks import _db_config
    return psycopg2.connect(**_db_config())


def get_checksum(file_path: Path) -> str:
    """计算文件 MD5 哈希"""
    with open(file_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def get_applied_versions(conn) -> set:
    """获取已应用的版本号集合"""
    cur = conn.cursor()
    cur.execute("SELECT version FROM schema_migrations ORDER BY version")
    versions = {row[0] for row in cur.fetchall()}
    cur.close()
    return versions


def apply_migration(conn, version: str, file_path: Path, description: str):
    """执行单个迁移脚本"""
    print(f"[+] Applying migration {version}: {description}")
    
    # 读取 SQL
    sql = file_path.read_text(encoding="utf-8")
    checksum = get_checksum(file_path)
    
    cur = conn.cursor()
    try:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (version, description, checksum) VALUES (%s, %s, %s)",
            (version, description, checksum)
        )
        conn.commit()
        print(f"    ✓ Applied {version}")
    except Exception as e:
        conn.rollback()
        print(f"    ✗ Failed {version}: {e}")
        raise
    finally:
        cur.close()


def get_migration_files() -> list[Path]:
    """返回所有 .sql 文件路径（按文件名排序）"""
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted([f for f in MIGRATIONS_DIR.glob("*.sql") if f.name != "schema_migrations.sql"])


def main():
    # 1. 初始化 schema_migrations 表
    conn = get_db_connection()
    
    # 2. 获取已应用版本（先检查 schema_migrations 表是否创建）
    cur = conn.cursor()
    try:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        applied = {row[0] for row in cur.fetchall()}
    except psycopg2.ProgrammingError:
        # 表未创建，先回滚中止的事务，再创建它
        conn.rollback()
        # docs/migrations/schema_migrations.sql (Docker: /app/docs/migrations)
        migrations_dir_sql = Path("/app/docs/migrations/schema_migrations.sql")
        if not migrations_dir_sql.exists():
            raise FileNotFoundError(f"Migration schema file not found: {migrations_dir_sql}")
        cur.execute(migrations_dir_sql.read_text(encoding="utf-8"))
        conn.commit()
        applied = set()
    finally:
        cur.close()
    
    # 3. 遍历迁移文件
    files = get_migration_files()
    skipped = 0
    applied_count = 0
    
    for file_path in files:
        # 文件名格式：NNN_description.sql → 提取版本号
        version = file_path.stem.split("_", 1)[0]
        
        if version in applied:
            print(f"[i] Skipped {version}: already applied")
            skipped += 1
            continue
        
        description = "_".join(file_path.stem.split("_")[1:]) if "_" in file_path.stem else "unknown"
        apply_migration(conn, version, file_path, description)
        applied_count += 1
    
    print(f"\n✅ Migration complete. New: {applied_count}, Skipped: {skipped}")
    conn.close()


if __name__ == "__main__":
    main()

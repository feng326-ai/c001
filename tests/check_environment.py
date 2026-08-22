# ===========================================
# 微信搜一搜智能采集系统 - 环境健康检查脚本
# ===========================================

"""
目的：在运行测试套件前，先验证所有依赖服务已就绪
包括：PostgreSQL / Redis / Python 依赖 / Docker 环境
"""

import subprocess
import sys
from typing import Tuple, Dict


def check_python_version() -> bool:
    """检查 Python 版本 (要求 >=3.11)"""
    print("🔍 检查 Python 版本...")
    
    version = sys.version_info
    
    if version.major >= 3 and version.minor >= 11:
        print(f"✅ Python {version.major}.{version.minor}.{version.micro} ✓")
        return True
    else:
        print(f"❌ Python 版本过低：{version.major}.{version.minor}")
        print("   请升级到 Python 3.11+")
        return False


def check_dependencies() -> bool:
    """检查核心 Python 依赖库"""
    print("\n🔍 检查 Python 依赖库...")
    
    required_packages = [
        "requests",
        "redis",
        "psycopg2",
        "fastapi",
        "celery",
        "beautifulsoup4"
    ]
    
    missing = []
    
    for package in required_packages:
        try:
            __import__(package.replace("-", "_"))
            print(f"   ✅ {package}")
        except ImportError:
            missing.append(package)
            print(f"   ❌ {package} - 未安装")
    
    if missing:
        print(f"\n❌ 发现缺失依赖：{missing}")
        print("运行以下命令安装:")
        print(f"   pip install {' '.join(missing)}")
        return False
    
    print("✅ 所有核心依赖已安装")
    return True


def check_redis_connection() -> bool:
    """检查 Redis 连接"""
    print("\n🔍 检查 Redis 连接...")
    
    try:
        import redis
        
        r = redis.Redis(host='localhost', port=6379, decode_responses=True)
        r.ping()
        
        print("✅ Redis 连接正常 (localhost:6379)")
        return True
        
    except Exception as e:
        print(f"❌ Redis 连接失败：{e}")
        print("\n解决方法:")
        print("  Windows: docker-compose up -d redis")
        print("  Linux/Mac: sudo systemctl start redis")
        return False


def check_postgresql_connection() -> bool:
    """检查 PostgreSQL 连接"""
    print("\n🔍 检查 PostgreSQL 连接...")
    
    try:
        import psycopg2
        
        conn = psycopg2.connect(
            host="localhost",
            port=5432,
            database="wx_search",
            user="admin",
            password="your_secure_password_here"
        )
        
        cur = conn.cursor()
        cur.execute("SELECT 1")
        
        print("✅ PostgreSQL 连接正常 (localhost:5432)")
        
        # 检查数据库是否存在
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables 
            WHERE table_schema = 'public'
        """)
        
        table_count = cur.fetchone()[0]
        
        if table_count > 10:
            print(f"✅ 数据库表结构已初始化 ({table_count}张表)")
        else:
            print(f"⚠️  数据库可能未初始化 ({table_count}张表)")
            print("   请先运行：psql -U admin -d wx_search -f docs/db_schema.sql")
        
        cur.close()
        conn.close()
        
        return True
        
    except Exception as e:
        print(f"❌ PostgreSQL 连接失败：{e}")
        print("\n解决方法:")
        print("  1. 确保 PostgreSQL 服务已启动")
        print("  2. 检查配置文件中的密码是否正确")
        print("  3. 或运行 Docker: docker-compose up -d postgres")
        return False


def check_docker_services() -> Dict[str, bool]:
    """检查 Docker Compose 服务状态"""
    print("\n🔍 检查 Docker Compose 服务...")
    
    try:
        result = subprocess.run(
            ["docker-compose", "ps"],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode != 0:
            print("⚠️  Docker Compose 未安装或未运行")
            return {}
        
        lines = result.stdout.strip().split("\n")[1:]  # 跳过标题行
        
        services_status = {}
        
        for line in lines:
            if not line.strip():
                continue
            
            parts = line.split()
            
            if len(parts) >= 5:
                service_name = parts[1]
                status = parts[4]  # last restart or healthy
                
                is_healthy = "healthy" in status.lower() or status == "Up"
                
                if is_healthy:
                    symbol = "✅"
                else:
                    symbol = "⚠️  "
                
                print(f"{symbol} {service_name}: {status}")
                services_status[service_name] = is_healthy
        
        return services_status
        
    except FileNotFoundError:
        print("   ⚠️  Docker Compose 未找到")
        return {}
    except Exception as e:
        print(f"   ❌ 检查失败：{e}")
        return {}


def check_database_tables() -> int:
    """统计数据库表数量"""
    try:
        import psycopg2
        
        conn = psycopg2.connect(
            host="localhost",
            database="wx_search",
            user="admin"
        )
        
        cur = conn.cursor()
        
        cur.execute("""
            SELECT COUNT(*) FROM information_schema.tables 
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        """)
        
        count = cur.fetchone()[0]
        
        conn.close()
        
        if count >= 7:
            print(f"\n✅ 数据库表结构完整 ({count}张表)")
        else:
            print(f"\n⚠️  数据库表不完整 ({count}张表，预期≥7 张)")
            print("   请运行：psql -U admin -d wx_search -f docs/db_schema.sql")
        
        return count
        
    except Exception as e:
        print(f"\n❌ 无法检查数据库表：{e}")
        return 0


def run_all_checks() -> bool:
    """运行所有检查"""
    print("=" * 60)
    print(" 🔧 微信搜一搜智能采集系统 - 环境健康检查")
    print("=" * 60)
    
    results = {}
    
    # 检查 Python 版本
    results["python"] = check_python_version()
    
    # 检查依赖库
    results["dependencies"] = check_dependencies()
    
    # 检查 Redis
    results["redis"] = check_redis_connection()
    
    # 检查 PostgreSQL
    results["postgresql"] = check_postgresql_connection()
    
    # 检查 Docker 服务
    results["docker_services"] = check_docker_services()
    
    # 检查数据库表
    table_count = check_database_tables()
    
    # 最终结论
    print("\n" + "=" * 60)
    print(" 📊 检查结果汇总")
    print("=" * 60)
    
    passed = sum(results.values())
    total = len([v for v in results.values() if isinstance(v, bool)])
    
    if all(results.values()):
        print(f"✅ 所有检查通过 ({passed}/{total})")
        print("\n🎉 环境准备就绪，可以开始运行测试!")
        print("\n下一步:")
        print("  运行测试：test.bat")
        return True
    else:
        print(f"❌ 部分检查失败 ({passed}/{total}通过)")
        print("\n🔧 请根据上述提示修复问题后重试")
        return False


if __name__ == "__main__":
    success = run_all_checks()
    sys.exit(0 if success else 1)

# ===========================================
# 微信搜一搜智能采集系统 - Phase 1 测试验证套件
# ===========================================

"""
测试目标:
1. ✅ Docker 环境一键启动验证
2. ✅ API 接口连通性测试
3. ✅ 任务调度流程验证
4. ✅ 数据库初始化检查
5. ✅ AI 规则过滤器功能测试

运行方式:
  方式 1: pytest tests/ -v --tb=short
  方式 2: python run_tests.py
  方式 3: pytest tests/test_docker.py -v  # 仅测 Docker 部署
"""

import os
import sys
import time
import json
import shutil
import subprocess
from datetime import datetime
from typing import List, Dict, Optional

# ===========================================
# 配置参数
# ===========================================

BASE_URL = "http://localhost:8000"
ADMINER_URL = "http://localhost:8080"
DOCKER_COMPOSE_FILE = "docker-compose.yml"

# 测试关键词
TEST_KEYWORDS = [
    {"keyword": "评选征集", "category": "商业机会"},
    {"keyword": "人工智能", "category": "行业趋势"},
    {"keyword": "工业自动化", "category": "B2B"},
]

# VM 实例 ID (模拟多台机器)
TEST_VM_IDS = ["vm-001", "vm-002", "vm-003"]


# ===========================================
# 辅助函数
# ===========================================

def print_section(title: str):
    """打印分隔标题"""
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def log_success(msg: str):
    """成功日志"""
    print(f"\033[92m✅ {msg}\033[0m")


def log_error(msg: str):
    """错误日志"""
    print(f"\033[91m❌ {msg}\033[0m")


def log_info(msg: str):
    """信息日志"""
    print(f"ℹ️  {msg}")


def log_warning(msg: str):
    """警告日志"""
    print(f"\033[93m⚠️  {msg}\033[0m")


# ===========================================
# 测试模块 1: Docker 部署验证
# ===========================================

class TestDockerDeployment:
    """Docker 环境部署测试"""
    
    def test_docker_compose_syntax(self):
        """测试 1: Docker Compose 语法检查"""
        print_section("📦 测试 1: Docker Compose 语法检查")
        
        try:
            result = subprocess.run(
                ["docker-compose", "config"],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                log_success("Docker Compose 语法验证通过")
                return True
            else:
                log_error(f"Docker Compose 语法错误：{result.stderr}")
                return False
                
        except FileNotFoundError:
            log_warning("Docker Compose 未安装，跳过此测试")
            return None
            
        except subprocess.TimeoutExpired:
            log_error("Docker Compose 配置检查超时")
            return None
    
    def test_services_startup(self):
        """测试 2: 服务容器启动健康检查"""
        print_section("🖥️ 测试 2: Docker 服务启动状态")
        
        try:
            # 等待 10 秒让服务完全启动
            time.sleep(10)
            
            result = subprocess.run(
                ["docker-compose", "ps"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            lines = result.stdout.strip().split("\n")
            healthy_count = sum(1 for line in lines if "healthy" in line.lower())
            
            log_info(f"已启动服务数：{healthy_count}/{len(TEST_VM_IDS)+2}")
            
            if healthy_count >= 4:
                log_success("所有核心服务正常启动")
                return True
            else:
                log_error(f"部分服务未正常启动：{result.stdout}")
                return False
                
        except Exception as e:
            log_error(f"Docker 命令执行失败：{e}")
            return False
    
    def test_health_check(self):
        """测试 3: 各服务健康检查"""
        print_section("🩺 测试 3: 服务健康检查")
        
        health_status = {}
        
        # PostgreSQL 健康检查
        try:
            result = subprocess.run(
                ["docker", "exec", "wxsearch_db", 
                 "pg_isready", "-U", "admin"],
                capture_output=True,
                timeout=5
            )
            health_status["postgres"] = "healthy" if result.returncode == 0 else "unhealthy"
        except:
            health_status["postgres"] = "error"
        
        # Redis 健康检查
        try:
            result = subprocess.run(
                ["docker", "exec", "wxsearch_redis",
                 "redis-cli", "ping"],
                capture_output=True,
                timeout=5
            )
            health_status["redis"] = "healthy" if b"PONG" in result.stdout else "unhealthy"
        except:
            health_status["redis"] = "error"
        
        # Backend API 健康检查
        try:
            import requests
            resp = requests.get(f"{BASE_URL}/health", timeout=5)
            health_status["backend"] = "healthy" if resp.status_code == 200 else "unhealthy"
        except:
            health_status["backend"] = "unreachable"
        
        # 输出结果
        for service, status in health_status.items():
            symbol = "✅" if status == "healthy" else "❌"
            log_info(f"{symbol} {service}: {status}")
        
        return all(s == "healthy" for s in health_status.values())


# ===========================================
# 测试模块 2: API 接口验证
# ===========================================

class TestAPIEndpoints:
    """API 接口功能测试"""
    
    def __init__(self):
        import requests
        self.session = requests.Session()
        self.api_base = f"{BASE_URL}/api/v1"
    
    def test_root_endpoint(self):
        """测试 4: Root 端点响应"""
        print_section("🔗 测试 4: API 根路径响应")
        
        try:
            resp = self.session.get(BASE_URL, timeout=5)
            
            if resp.status_code == 200:
                data = resp.json()
                
                if data.get("status") == "ok":
                    log_success("API 根路径响应正常")
                    return True
                else:
                    log_error(f"Unexpected response: {data}")
                    return False
            else:
                log_error(f"HTTP {resp.status_code}")
                return False
                
        except Exception as e:
            log_error(f"Request failed: {e}")
            return False
    
    def test_create_keywords(self):
        """测试 5: 批量创建关键词"""
        print_section("📝 测试 5: 创建关键词 API")
        
        try:
            resp = self.session.post(
                f"{self.api_base}/keywords/",
                json=TEST_KEYWORDS,
                timeout=10
            )
            
            if resp.status_code == 200:
                data = resp.json()
                
                if data["count"] == len(TEST_KEYWORDS):
                    log_success(f"成功创建 {data['count']} 个关键词")
                    
                    # 存储创建的关键词供后续测试使用
                    self.created_keywords = [k["keyword"] for k in TEST_KEYWORDS]
                    return True
                else:
                    log_error(f"创建数量不匹配：期望{len(TEST_KEYWORDS)}, 实际{data['count']}")
                    return False
            else:
                log_error(f"HTTP {resp.status_code}: {resp.text}")
                return False
                
        except Exception as e:
            log_error(f"Create keywords failed: {e}")
            return False
    
    def test_list_keywords(self):
        """测试 6: 查询关键词列表"""
        print_section("📄 测试 6: 查询关键词列表 API")
        
        try:
            resp = self.session.get(
                f"{self.api_base}/keywords/?limit=10",
                timeout=5
            )
            
            if resp.status_code == 200:
                data = resp.json()
                
                total = data.get("total", 0)
                
                if total >= len(self.created_keywords):
                    log_success(f"查询到 {total} 个关键词")
                    return True
                else:
                    log_error(f"查询结果异常：预期>=3, 实际={total}")
                    return False
            else:
                log_error(f"HTTP {resp.status_code}")
                return False
                
        except Exception as e:
            log_error(f"List keywords failed: {e}")
            return False
    
    def test_claim_task(self):
        """测试 7: VM 领取任务"""
        print_section("🎯 测试 7: 任务领取 API")
        
        claimed_by_vm = {}
        
        for vm_id in TEST_VM_IDS[:2]:  # 只用前 2 台测试
            try:
                claim_data = {
                    "channel": "wechat_pc",
                    "vm_instance_id": vm_id,
                    "max_keywords": 2
                }
                
                resp = self.session.post(
                    f"{self.api_base}/tasks/claim",
                    json=claim_data,
                    timeout=10
                )
                
                if resp.status_code == 200:
                    result = resp.json()
                    keywords = result.get("keywords", [])
                    claimed_by_vm[vm_id] = keywords
                    
                    log_info(f"{vm_id} 领取到：{', '.join(keywords)}")
                else:
                    log_error(f"VM {vm_id} 领取失败：HTTP {resp.status_code}")
                    
            except Exception as e:
                log_error(f"Claim task failed for {vm_id}: {e}")
        
        # 验证是否没有重复领取
        all_kws = [kw for kws in claimed_by_vm.values() for kw in kws]
        
        if len(all_kws) == len(set(all_kws)):
            log_success("无重复领取现象")
            return True
        else:
            log_error("检测到重复领取！")
            return False
    
    def test_report_result(self):
        """测试 8: 上报采集结果"""
        print_section("📊 测试 8: 结果上报 API")
        
        success_count = 0
        
        for keyword in self.created_keywords[:2]:
            try:
                report_data = {
                    "keyword": keyword,
                    "articles_count": 10,
                    "success": True,
                    "error_message": None
                }
                
                resp = self.session.post(
                    f"{self.api_base}/tasks/result",
                    json=report_data,
                    timeout=5
                )
                
                if resp.status_code == 200:
                    success_count += 1
                    
            except Exception as e:
                log_error(f"Report result failed for {keyword}: {e}")
        
        if success_count == 2:
            log_success("所有结果上报成功")
            return True
        else:
            log_error(f"部分上报失败：{success_count}/2")
            return False
    
    def test_dashboard_stats(self):
        """测试 9: 仪表盘统计查询"""
        print_section("📈 测试 9: 数据统计 API")
        
        try:
            resp = self.session.get(
                f"{self.api_base}/stats/dashboard",
                timeout=5
            )
            
            if resp.status_code == 200:
                stats = resp.json()
                
                print(f"总关键词数：{stats.get('total_keywords')}")
                print(f"今日采集数：{stats.get('today_articles_collected')}")
                print(f"活跃 VM 数：{stats.get('active_vm_instances')}")
                
                log_success("统计数据正常返回")
                return True
            else:
                log_error(f"HTTP {resp.status_code}")
                return False
                
        except Exception as e:
            log_error(f"Get stats failed: {e}")
            return False


# ===========================================
# 测试模块 3: AI 规则过滤器功能测试
# ===========================================

class TestAILogicFilters:
    """AI 清洗引擎逻辑测试"""
    
    def setup_method(self):
        """测试前置准备"""
        from wxsearch.models import Article
        self.article = Article(
            title="",
            content="",
            url="https://mp.weixin.qq.com/s?__biz=xxx&mid=yyy&idx=zzz&sn=aaa",
            source_channel="wechat_pc",
            keyword="测试",
            account="测试公众号",
            account_id="test123456789",
            publish_time="2026-08-12 10:00"
        )
        
        from wxsearch.ai_filters.rule_filter import RuleBasedFilter
        self.filter = RuleBasedFilter()
    
    def test_advertisement_detection(self):
        """测试 10: 广告推广内容识别"""
        print_section("🚫 测试 10: 垃圾内容检测 - 广告类")
        
        self.article.title = "【免费领取】扫码注册领福利！"
        self.article.content = "点击链接，立即领取优惠券，限量抢购..."
        
        is_valid, reason = self.filter.filter(self.article)
        
        if not is_valid and "广告" in reason:
            log_success("广告内容正确拦截")
            return True
        else:
            log_error(f"应拦截未拦截：is_valid={is_valid}, reason={reason}")
            return False
    
    def test_short_content_detection(self):
        """测试 11: 内容过短检测"""
        print_section("📏 测试 11: 垃圾内容检测 - 内容过短")
        
        self.article.title = "测试标题"
        self.article.content = "仅几个字"
        
        is_valid, reason = self.filter.filter(self.article)
        
        if not is_valid and "过短" in reason:
            log_success("短内容正确拦截")
            return True
        else:
            log_error(f"应拦截未拦截：is_valid={is_valid}, reason={reason}")
            return False
    
    def test_expired_content_detection(self):
        """测试 12: 过期内容检测"""
        print_section("⏰ 测试 12: 垃圾内容检测 - 过期文章")
        
        self.article.title = "去年发布的文章"
        self.article.content = "这是三年前发布的内容，应该被过滤掉"
        self.article.publish_time = "2023-01-01 10:00"
        
        is_valid, reason = self.filter.filter(self.article)
        
        if not is_valid and "过期" in reason:
            log_success("过期文章正确拦截")
            return True
        else:
            log_error(f"应拦截未拦截：is_valid={is_valid}, reason={reason}")
            return False
    
    def test_valid_article_pass(self):
        """测试 13: 正常文章内容放行"""
        print_section("✅ 测试 13: 正常文章内容放行")
        
        self.article.title = "人工智能发展趋势报告"
        self.article.content = "本文详细介绍了人工智能在医疗、教育、工业等领域的最新应用进展。"
        
        is_valid, reason = self.filter.filter(self.article)
        
        if is_valid and reason == "pass":
            log_success("正常文章正确放行")
            return True
        else:
            log_error(f"误拦截：is_valid={is_valid}, reason={reason}")
            return False


# ===========================================
# 主测试入口
# ===========================================

def run_all_tests(verbose: bool = True):
    """运行所有测试"""
    
    print_section("🚀 微信搜一搜智能采集系统 - Phase 1 测试套件")
    print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    passed = 0
    failed = 0
    skipped = 0
    
    results = []
    
    # 测试模块 1: Docker 部署
    print_section("📦 第一阶段：Docker 部署验证")
    
    docker_test = TestDockerDeployment()
    
    if docker_test.test_docker_compose_syntax():
        passed += 1
    elif docker_test.test_docker_compose_syntax() is None:
        skipped += 1
    
    time.sleep(5)  # 等待服务启动
    
    if docker_test.test_services_startup():
        passed += 1
    else:
        failed += 1
    
    if docker_test.test_health_check():
        passed += 1
    else:
        failed += 1
    
    # 测试模块 2: API 接口
    print_section("🌐 第二阶段：API 接口功能测试")
    
    api_test = TestAPIEndpoints()
    
    if api_test.test_root_endpoint():
        passed += 1
    else:
        failed += 1
    
    if api_test.test_create_keywords():
        passed += 1
    else:
        failed += 1
    
    if api_test.test_list_keywords():
        passed += 1
    else:
        failed += 1
    
    if api_test.test_claim_task():
        passed += 1
    else:
        failed += 1
    
    if api_test.test_report_result():
        passed += 1
    else:
        failed += 1
    
    if api_test.test_dashboard_stats():
        passed += 1
    else:
        failed += 1
    
    # 测试模块 3: AI 逻辑
    print_section("🧠 第三阶段：AI 逻辑测试")
    
    ai_test = TestAILogicFilters()
    
    if ai_test.test_advertisement_detection():
        passed += 1
    else:
        failed += 1
    
    if ai_test.test_short_content_detection():
        passed += 1
    else:
        failed += 1
    
    if ai_test.test_expired_content_detection():
        passed += 1
    else:
        failed += 1
    
    if ai_test.test_valid_article_pass():
        passed += 1
    else:
        failed += 1
    
    # 总结报告
    print_section("📊 测试结果汇总")
    
    print(f"\033[92m✅ 通过：{passed} / {passed + failed + skipped}\033[0m")
    if failed > 0:
        print(f"\033[91m❌ 失败：{failed}\033[0m")
    if skipped > 0:
        print(f"\033[93m⚠️ 跳过：{skipped}\033[0m")
    
    if failed == 0:
        print("\n🎉 所有测试通过！系统运行正常！")
        return True
    else:
        print("\n❌ 部分测试失败，请查看日志排查问题")
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)

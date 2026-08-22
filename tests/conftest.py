"""
pytest 配置文件
"""

import pytest
from _pytest.config import Config


def pytest_configure(config):
    """测试前配置"""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m 'not slow'')"
    )
    
    # 设置日志级别
    pytest.verbose_level = 1
    
    print("\n🚀 开始运行 Phase 1 测试套件...")


@pytest.fixture(scope="session")
def base_url():
    """API 基础 URL"""
    return "http://localhost:8000"


@pytest.fixture
def api_client(base_url):
    """API 客户端"""
    import requests
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session


@pytest.mark.parametrize("keyword,category", [
    ("评选征集", "商业机会"),
    ("人工智能", "行业趋势"),
])
def test_keyword_registration(keyword, category):
    """参数化测试：关键词注册"""
    from wxsearch.task_scheduler import DistributedTaskScheduler
    
    scheduler = DistributedTaskScheduler()
    
    try:
        count = scheduler.register_keywords([keyword], category)
        
        assert count >= 0  # 可能已存在
        
    finally:
        del scheduler


def test_database_health_check():
    """测试数据库健康检查"""
    from wxsearch.db_connector import DatabaseConnector
    
    connector = DatabaseConnector()
    health = connector.health_check()
    
    assert health == True


def test_rule_filter_instance():
    """测试规则过滤器初始化"""
    from wxsearch.ai_filters.rule_filter import RuleBasedFilter
    
    filter_instance = RuleBasedFilter()
    
    assert filter_instance is not None
    assert hasattr(filter_instance, 'filter')
    assert hasattr(filter_instance, 'ADVERT_KEYWORDS')

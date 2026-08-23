"""
AI 智能清洗引擎 - 第一部分：规则过滤器
用于快速拦截明显的垃圾内容 (广告/推广/低质文章)
成本低、速度快，作为 LLM 的前置过滤层
"""

import re
import logging
from typing import Tuple, Optional
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


class RuleBasedFilter:
    """基于规则的快速过滤器"""
    
    # ==================== 配置参数 ====================
    
    # 黑名单关键词 (可配置化)
    ADVERT_KEYWORDS = {
        # 推广类
        "广告投放", "品牌推广", "合作咨询", "代理加盟", "招商加盟",
        "加微信领取", "扫码注册", "限时优惠", "点击链接",
        "会员充值", "积分兑换", "免费领取", "0 元购",
        
        # 金融类 (高风险)
        "刷单返利", "投资理财", "博彩赌博", "色情服务",
        "贷款口子", "信用卡套现", "高息理财",
        
        # 其他垃圾
        "点击关注", "点赞转发", "抽奖活动", "砍一刀"
    }
    
    # 最小正文长度阈值 (字符数)
    LOW_QUALITY_MIN_WORDS = 100
    
    # 最大特殊字符占比 (超过视为乱码/垃圾)
    MAX_SPECIAL_CHAR_RATIO = 0.3
    
    # 内容有效期天数
    VALID_DAYS_LIMIT = 30
    
    # 列表页 URL 模式 (非正文页)
    LIST_PAGE_PATTERNS = [
        r'/s\?.*type=\d+.*page=\d+',
        r'/category/\d+',
        r'/topic/',
        r'/list/',
        r'/archives/\d+',
    ]
    
    def __init__(self, config=None):
        """config: 可选 CleaningConfig（或含同名属性的对象）。缺省用类内置默认值。

        这样规则阈值/黑名单可在 config.json 的 cleaning 节热调，无需改代码。
        """
        self.enabled = getattr(config, "enabled", True)
        kws = getattr(config, "advert_keywords", None)
        self.advert_keywords = set(kws) if kws is not None else set(self.ADVERT_KEYWORDS)
        self.min_content_words = getattr(config, "min_content_words", self.LOW_QUALITY_MIN_WORDS)
        self.max_special_char_ratio = getattr(config, "max_special_char_ratio", self.MAX_SPECIAL_CHAR_RATIO)
        self.valid_days_limit = getattr(config, "valid_days_limit", self.VALID_DAYS_LIMIT)
        pats = getattr(config, "list_page_patterns", None)
        self.list_page_patterns = list(pats) if pats is not None else list(self.LIST_PAGE_PATTERNS)
        self.min_account_id_len = getattr(config, "min_account_id_len", 10)

    def filter(self, article) -> Tuple[bool, str]:
        """
        过滤文章
        
        Args:
            article: Article 对象 (包含 title, content, url, publish_time, account_id)
        
        Returns:
            (is_valid, reason)
            is_valid=True: 通过初筛
            reason: 不通过原因
        """
        
        # 清洗总开关：关闭时全部放行
        if not self.enabled:
            return True, "cleaning_disabled"

        # 检查 1: 黑名单关键词
        is_valid, reason = self._check_advert_keywords(article)
        if not is_valid:
            return False, reason
        
        # 检查 2: 内容长度
        is_valid, reason = self._check_content_length(article)
        if not is_valid:
            return False, reason
        
        # 检查 3: 无意义字符占比
        is_valid, reason = self._check_special_characters(article)
        if not is_valid:
            return False, reason
        
        # 检查 4: 发布时间是否过期
        is_valid, reason = self._check_publish_time(article)
        if not is_valid:
            return False, reason
        
        # 检查 5: 是否为列表页而非正文
        is_valid, reason = self._check_is_list_page(article.url)
        if not is_valid:
            return False, reason
        
        # 检查 6: 账号资质验证 (可选)
        # PC UIA 的轻量 Article 不保证提取公众号 __biz；该字段缺失时跳过可选
        # 资质校验，不能让整套规则清洗因模型字段差异异常退出。
        is_valid, reason = self._verify_account_quality(getattr(article, "account_id", ""))
        if not is_valid:
            return False, reason
        
        return True, "pass"
    
    # ==================== 核心检测逻辑 ====================
    
    def _check_advert_keywords(self, article) -> Tuple[bool, str]:
        """检查是否包含广告推广关键词"""
        
        text = f"{article.title} {article.content}"
        
        for keyword in self.advert_keywords:
            if keyword in text:
                log.debug(f"广告关键词匹配：{keyword}")
                return False, f"广告推广内容 ({keyword})"
        
        return True, "pass"
    
    def _check_content_length(self, article) -> Tuple[bool, str]:
        """检查内容是否过短"""
        
        content_len = len(article.content)
        
        if content_len < self.min_content_words:
            log.debug(f"内容过短：{content_len} 字符")
            return False, f"内容过短 ({content_len}<={self.min_content_words})"
        
        return True, "pass"
    
    def _check_special_characters(self, article) -> Tuple[bool, str]:
        """检查特殊字符占比是否过高"""
        
        text = article.content
        
        if len(text) == 0:
            return True, "pass"
        
        # 统计非中文、非英文、非数字的字符
        special_chars = len(re.findall(r'[^\u4e00-\u9fa5a-zA-Z0-9\u3000-\u303f]', text))
        char_ratio = special_chars / len(text)
        
        if char_ratio > self.max_special_char_ratio:
            log.debug(f"特殊字符过多：{char_ratio:.2%}")
            return False, f"无意义字符过多 ({char_ratio:.2%}>{self.max_special_char_ratio})"
        
        return True, "pass"
    
    def _check_publish_time(self, article) -> Tuple[bool, str]:
        """检查发布时间是否在有效期内"""
        
        publish_time = article.publish_time
        
        if not publish_time:
            log.warning("未找到发布时间，放行")
            return True, "pass"
        
        try:
            # 解析发布时间字符串
            pub_dt = self._parse_publish_time(publish_time)
            
            if pub_dt:
                hours_ago = (datetime.now() - pub_dt).total_seconds() / 3600
                
                if pub_dt < datetime.now() - timedelta(days=self.valid_days_limit):
                    log.debug(f"内容过期：{hours_ago:.1f}小时前")
                    return False, f"已超过{self.valid_days_limit}天有效期 ({hours_ago:.0f}小时前)"
        
        except Exception as e:
            log.warning(f"时间解析失败：{publish_time}, {e}")
            # 解析失败不影响流程
            return True, "pass"
        
        return True, "pass"
    
    def _check_is_list_page(self, url: str) -> Tuple[bool, str]:
        """检查是否为列表页而非正文页"""
        
        for pattern in self.list_page_patterns:
            if re.search(pattern, url):
                log.debug(f"列表页 URL 匹配：{pattern}")
                return False, "非正文页 (列表页)"
        
        return True, "pass"
    
    def _verify_account_quality(self, account_id: str) -> Tuple[bool, str]:
        """
        验证账号质量
        
        TODO: 接入企业微信认证 API / 天眼查 API
        
        简化版：只要存在即给基础分
        """
        
        if not account_id:
            log.debug("缺少 __biz，跳过资质验证")
            return True, "pass"
        
        # 基本长度校验
        if len(account_id) < self.min_account_id_len:
            return False, "公众号 ID 异常"
        
        return True, "pass"
    
    # ==================== 辅助方法 ====================
    
    def _parse_publish_time(self, time_str: str) -> Optional[datetime]:
        """解析发布时间字符串"""
        
        # 尝试多种格式
        formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y年%m月%d日 %H:%M",
            "%Y年%m月%d日",
            "%Y/%m/%d %H:%M",
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt)
            except ValueError:
                continue
        
        # 最后尝试自动解析
        try:
            from dateutil import parser
            return parser.parse(time_str)
        except:
            return None
    
    # ==================== 批量过滤接口 ====================
    
    def filter_batch(self, articles: list) -> Tuple[list, dict]:
        """
        批量过滤
        
        Returns:
            (passed_articles, stats_dict)
            stats_dict = {"total": N, "filtered": {...}}
        """
        
        passed = []
        stats = {
            "total": len(articles),
            "advert": 0,
            "short_content": 0,
            "special_chars": 0,
            "expired": 0,
            "list_page": 0,
            "low_quality_account": 0,
            "passed": 0
        }
        
        for article in articles:
            is_valid, reason = self.filter(article)
            
            if is_valid:
                passed.append(article)
                stats["passed"] += 1
            else:
                # 分类计数
                if "广告" in reason:
                    stats["advert"] += 1
                elif "过短" in reason:
                    stats["short_content"] += 1
                elif "特殊" in reason:
                    stats["special_chars"] += 1
                elif "过期" in reason:
                    stats["expired"] += 1
                elif "列表页" in reason:
                    stats["list_page"] += 1
                elif "资质" in reason or "ID 异常" in reason:
                    stats["low_quality_account"] += 1
        
        return passed, stats


# ==================== 使用示例 ====================

if __name__ == "__main__":
    from wxsearch.models import Article
    
    # 测试用例 1: 正常文章
    article1 = Article(
        title="人工智能发展趋势",
        content="我司为 XX 制造企业，现因新项目需要...",
        url="https://mp.weixin.qq.com/s?__biz=xxx&mid=yyy&idx=zzz&sn=aaa",
        source_channel="wechat_pc",
        keyword="人工智能",
        account="XX 科技有限公司官方账号",
        account_id="xxx123456789",
        publish_time="2026-08-12 10:30"
    )
    
    # 测试用例 2: 广告推广
    article2 = Article(
        title="【免费领取】扫码注册领福利！",
        content="点击链接，立即领取优惠券，限量抢购...",
        url="https://mp.weixin.qq.com/s?...",
        source_channel="wechat_pc",
        keyword="优惠",
        account="营销号",
        account_id="spamservice",
        publish_time="2026-08-12 09:00"
    )
    
    # 运行测试
    filter_instance = RuleBasedFilter()
    
    print("=== 测试用例 1 ===")
    is_valid1, reason1 = filter_instance.filter(article1)
    print(f"是否通过：{is_valid1}")
    print(f"原因：{reason1}")
    
    print("\n=== 测试用例 2 ===")
    is_valid2, reason2 = filter_instance.filter(article2)
    print(f"是否通过：{is_valid2}")
    print(f"原因：{reason2}")
    
    # 批量测试
    test_articles = [article1, article2]
    passed, stats = filter_instance.filter_batch(test_articles)
    
    print("\n=== 批量过滤统计 ===")
    print(stats)

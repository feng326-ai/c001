"""
Content Hasher - 内容指纹生成器
用于多渠道内容去重 (SHA256 + SimHash)
"""

import hashlib
import re
from typing import Optional


class ContentHasher:
    """通用内容指纹生成器 (支持所有渠道)"""
    
    # ==================== 核心方法 ====================
    
    @staticmethod
    def extract_text_from_source(content: str, source_channel: str) -> str:
        """
        根据不同渠道特点提取纯净文本
        
        Args:
            content: HTML/纯文本内容
            source_channel: wechat_pc / sogou_wap / baidu_news / weixin_mobile
        
        Returns:
            清理后的纯文本
        """
        
        if source_channel == "wechat_pc":
            return ContentHasher._extract_from_weixin_mp(content)
        
        elif source_channel == "sogou_wap":
            return ContentHasher._extract_from_sogou(content)
        
        elif source_channel == "baidu_news":
            return ContentHasher._extract_from_baidu_news(content)
        
        elif source_channel == "weixin_mobile":
            return ContentHasher._extract_from_weixin_mobile_app(content)
        
        else:
            # 通用降级处理
            return ContentHasher._general_extract(content)
    
    @staticmethod
    def _extract_from_weixin_mp(html_content: str) -> str:
        """从微信公众号 mp.weixin.qq.com 提取正文"""
        try:
            from bs4 import BeautifulSoup
            
            soup = BeautifulSoup(html_content, "html.parser")
            
            # 1. 移除不需要的元素
            for tag in soup.find_all(["script", "style", "iframe", "ads"]):
                tag.decompose()
            
            # 2. 获取正文容器
            content_div = soup.select_one(".rich_media_content, .article-content")
            
            if not content_div:
                # 回退①：取所有 P 标签
                texts = [p.get_text().strip() for p in soup.select("p") if p.get_text().strip()]
                if texts:
                    return "\n".join(texts)
                # 回退②：PC UIA 采集到的正文是**纯文本**（UIA TextControl 拼接，无任何 HTML 标签），
                # 上面两个 HTML 选择器都会落空并返回空串——那会让所有文章的 content_hash 同为
                # 空串 SHA256，导致第②层精确去重把彼此无关的文章全判为 exact_duplicate 而丢弃。
                # 故降级到通用提取（去标签 + 归一空白），保证纯文本输入也能得到真实指纹。
                return ContentHasher._general_extract(html_content)
            
            # 3. 清理文本
            text = content_div.get_text(separator="\n", strip=True)
            text = re.sub(r'\s+', ' ', text)
            
            return text[:5000]  # 限制长度
            
        except Exception:
            return ContentHasher._general_extract(html_content)
    
    @staticmethod
    def _extract_from_sogou(html_content: str) -> str:
        """从搜狗 WAP 页提取"""
        try:
            from bs4 import BeautifulSoup
            
            soup = BeautifulSoup(html_content, "html.parser")
            
            # 搜狗通常直接展示全文
            text = soup.get_text(separator="\n", strip=True)
            
            # 去除搜狗特有元素
            text = re.sub(r'(我来说两句 | 相关推荐 | 广告)', '', text)
            text = re.sub(r'\s+', ' ', text)
            
            return text[:3000]
            
        except Exception:
            return ContentHasher._general_extract(html_content)
    
    @staticmethod
    def _extract_from_weixin_mobile_app(html_content: str) -> str:
        """从手机微信客户端提取"""
        try:
            from bs4 import BeautifulSoup
            
            soup = BeautifulSoup(html_content, "html.parser")
            
            # 移除广告、分享按钮
            for tag in soup.find_all(class_=re.compile(r"(ad|share|recommend)")):
                tag.decompose()
            
            # 尝试多种可能的正文类名
            selectors = [
                ".rich_media_content",
                "#js_content",
                ".article-content", 
                ".wx_article"
            ]
            
            for selector in selectors:
                content = soup.select_one(selector)
                if content:
                    text = content.get_text(separator="\n", strip=True)
                    return re.sub(r'\s+', ' ', text)[:5000]
            
            # 兜底：全部文本
            return soup.get_text(separator="\n", strip=True)[:5000]
            
        except Exception:
            return ContentHasher._general_extract(html_content)
    
    @staticmethod
    def _extract_from_baidu_news(html_content: str) -> str:
        """从百度新闻提取"""
        try:
            from bs4 import BeautifulSoup
            
            soup = BeautifulSoup(html_content, "html.parser")
            
            # 百度新闻可能是 JavaScript 渲染的 JSON
            json_script = soup.select_one('script[type="application/json"]')
            if json_script and json_script.string:
                import json
                try:
                    data = json.loads(json_script.string)
                    content = data.get("content", data.get("summary", ""))
                    return content[:5000]
                except:
                    pass
            
            # HTML 降级处理
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r'(来源 | 时间 | 编辑 | 违法和不良信息举报)', '', text)
            text = re.sub(r'\s+', ' ', text)
            
            return text[:3000]
            
        except Exception:
            return ContentHasher._general_extract(html_content)
    
    @staticmethod
    def _general_extract(html_or_text: str) -> str:
        """通用文本提取 (无依赖)"""
        # 去掉 HTML 标签
        text = re.sub(r'<[^>]+>', ' ', html_or_text)
        # 统一空白符
        text = re.sub(r'\s+', ' ', text).strip()
        
        return text[:5000]
    
    @staticmethod
    def generate_content_hash(clean_text: str) -> str:
        """
        生成内容指纹 (SHA256)
        用于精确匹配 (完全相同的转载)
        
        Args:
            clean_text: 已清理的纯净文本
        
        Returns:
            64 位十六进制哈希值
        """
        
        # 1. 进一步净化
        text = re.sub(r'(推荐阅读 | 相关视频 | 大家都在搜 | 广告 | 分享到朋友圈)', '', clean_text)
        text = re.sub(r'(微信号 | weixin[^\s]* | 公众号)', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        
        # 2. 截取前 N 字作为核心段
        core_text = text[:3000]
        
        # 3. 计算 SHA256
        return hashlib.sha256(core_text.encode('utf-8')).hexdigest()
    
    @staticmethod
    def generate_similarity_hash(clean_text: str, bits: int = 64) -> int:
        """
        生成 SimHash 指纹 (用于近似重复检测)
        可以识别标题微调/正文改动的情况
        
        Args:
            clean_text: 已清理的纯净文本
            bits: 指纹位数 (默认 64 位)
        
        Returns:
            整数形式的 SimHash 指纹
        """
        
        # 引入 jieba 分词
        try:
            import jieba
            
            words = list(jieba.cut(clean_text))
            
        except ImportError:
            # 没有 jieba 时简化版分词
            words = list(clean_text.replace('\n', '').replace(' ', ''))
        
        # 简单版 SimHash: 每个词权重 → 累加到指纹位
        fingerprint = [0] * bits
        
        for word in words:
            # 词的哈希值
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            
            # 映射到指纹位
            for i in range(bits):
                if h & 1:
                    fingerprint[i] += 1
                else:
                    fingerprint[i] -= 1
                h >>= 1
        
        # 转换为最终指纹 (阈值 >0 为 1, <=0 为 0)
        final_fp = 0
        for i, val in enumerate(fingerprint):
            if val > 0:
                final_fp |= (1 << i)
        
        return final_fp
    
    @staticmethod
    def hamming_distance(fp1: int, fp2: int, bits: int = 64) -> int:
        """
        计算两个 SimHash 指纹的汉明距离
        距离越小越相似
        
        Args:
            fp1: 第一个指纹
            fp2: 第二个指纹
            bits: 指纹位数
        
        Returns:
            汉明距离 (不同位的数量)
        """
        xor = fp1 ^ fp2
        return bin(xor).count('1')
    
    @staticmethod
    def calculate_similarity(fp1: int, fp2: int, threshold_ratio: float = 0.9) -> bool:
        """
        判断两个指纹是否高度相似
        
        Args:
            fp1: 第一个指纹
            fp2: 第二个指纹
            threshold_ratio: 相似度阈值 (>0.9 视为相似)
        
        Returns:
            是否相似
        """
        
        distance = ContentHasher.hamming_distance(fp1, fp2)
        max_distance = int(64 * (1 - threshold_ratio))
        
        return distance <= max_distance
    
    # ==================== 批量处理方法 ====================
    
    @classmethod
    def batch_process(cls, articles: list) -> dict:
        """
        批量处理文章并返回指纹统计
        
        Args:
            articles: Article 对象列表
        
        Returns:
            {"exact_hashes": {hash: count}, "similarity_clusters": {...}}
        """
        
        exact_hashes = {}
        similarity_info = []
        
        for article in articles:
            clean_text = cls.extract_text_from_source(
                article.content, 
                article.source_channel
            )
            
            sha256_hash = cls.generate_content_hash(clean_text)
            simhash = cls.generate_similarity_hash(clean_text)
            
            exact_hashes[sha256_hash] = exact_hashes.get(sha256_hash, 0) + 1
            
            similarity_info.append({
                "article_id": id(article),
                "channel": article.source_channel,
                "sha256": sha256_hash,
                "simhash": simhash,
                "title": article.title
            })
        
        return {
            "total_articles": len(articles),
            "exact_duplicates": sum(1 for cnt in exact_hashes.values() if cnt > 1),
            "unique_hashes": len(exact_hashes),
            "similarity_samples": similarity_info[:10]  # 仅返回 10 个样本
        }


# ==================== 使用示例 ====================

if __name__ == "__main__":
    from wxsearch.models import Article
    
    # 测试用例 1: 相同内容的不同渠道
    article1 = Article(
        title="人工智能发展趋势报告",
        content="本文详细介绍了人工智能在医疗领域的最新应用...",
        url="https://mp.weixin.qq.com/s?__biz=xxx&mid=yyy&idx=zzz&sn=aaa",
        source_channel="wechat_pc",
        keyword="人工智能"
    )
    
    article2 = Article(
        title="人工智能发展趋势报告 (转载)",
        content="本文详细介绍了人工智能在医疗领域的最新应用...",  # 内容相同
        url="https://news.baidu.com/n?word=人工智能",
        source_channel="baidu_news",
        keyword="人工智能"
    )
    
    # 测试内容哈希
    print("=== 测试内容哈希 ===")
    hash1 = ContentHasher.generate_content_hash(article1.content)
    hash2 = ContentHasher.generate_content_hash(article2.content)
    
    print(f"文章 1 哈希：{hash1}")
    print(f"文章 2 哈希：{hash2}")
    print(f"是否完全相同：{hash1 == hash2}")
    
    # 测试 SimHash 相似度
    print("\n=== 测试 SimHash 相似度 ===")
    simhash1 = ContentHasher.generate_similarity_hash(article1.content)
    simhash2 = ContentHasher.generate_similarity_hash(article2.content)
    
    similar = ContentHasher.calculate_similarity(simhash1, simhash2)
    distance = ContentHasher.hamming_distance(simhash1, simhash2)
    
    print(f"SimHash 1: {bin(simhash1)}")
    print(f"SimHash 2: {bin(simhash2)}")
    print(f"汉明距离：{distance}")
    print(f"是否相似：{similar}")
    
    # 批量测试
    print("\n=== 批量处理测试 ===")
    test_articles = [article1, article2]
    stats = ContentHasher.batch_process(test_articles)
    
    print(f"总文章数：{stats['total_articles']}")
    print(f"完全重复：{stats['exact_duplicates']}")
    print(f"唯一指纹：{stats['unique_hashes']}")

"""配置加载模块。

从 JSON 文件读取配置，缺省字段回退到内置默认值，避免配置文件不完整时崩溃。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List


# ---- 内置默认配置（当 config.json 缺字段时兜底） ----
_DEFAULTS: Dict[str, Any] = {
    "keywords": ["人工智能"],
    "result_type": "article",
    "database": {"path": "data/wechat_search.db"},
    "collect": {
        "max_scrolls": 30,
        "max_items_per_keyword": 200,
        "scroll_pause_sec": 1.2,
        "stop_after_no_new_rounds": 3,
        "fetch_url": False,
    },
    "delays": {
        "action_pause_sec": 0.8,
        "window_wait_sec": 8.0,
        "input_settle_sec": 1.0,
    },
    "selectors": {
        "main_window_class_candidates": ["Chrome_WidgetWin_0", "WeChatMainWndForPC", "mmui::MainWindow", "Weixin"],
        "main_window_name_candidates": ["微信", "Weixin"],
        "search_edit_name_candidates": ["搜索"],
        "search_entry_keyword_candidates": ["搜一搜", "搜索一下"],
        "search_result_window_name_candidates": ["搜一搜", "搜索"],
        "article_tab_name_candidates": ["文章"],
        "more_button_name_candidates": ["更多", "...", "···"],
        "copy_link_menu_candidates": ["复制链接", "复制链接地址"],
        "search_doc_keyword": "搜一搜",
        "app_menu_button_class": "AppMenuButton",
        "article_url_prefix": "https://mp.weixin.qq.com",
        "chat_window_class": "mmui::MainWindow",
        "search_entry_button_name": "搜一搜",
        "search_entry_button_class": "mmui::XTabBarItem",
        # 4.1.x 聊天主窗为 Qt 窗口（ClassName 形如 Qt51514QWindowIcon），UIA 不透明
        # （GetChildren 为空），无法在其内定位「搜一搜」按钮，只能盲点左侧栏图标坐标。
        # 命中判据：ClassName 含此子串且窗口名在 main_window_name_candidates 内。
        "qt_window_class_hint": "Qt",
        # 「搜一搜」图标相对 Qt 主窗左上角的偏移（窗口相对坐标，图标顶栏锚定，随窗移动稳定）。
        "sidebar_search_offset_x": 30,
        "sidebar_search_offset_y": 350,
        # 4.1.x 键盘入口路径（_open_search_via_qt_searchbox）逐台标定用：
        # 最大化后相对 Qt 主窗左上角的偏移。聊天图标（归一到聊天页）与顶部搜索框。
        # 每台 VM 布局/DPI 不同需各自标定；缺省沿用 win10-0 实测值。
        "searchbox_chat_icon_x": 40,
        "searchbox_chat_icon_y": 120,
        "searchbox_entry_x": 167,
        "searchbox_entry_y": 65,
        "filter_row_labels": ["排序", "类型", "时间", "范围"],
        "filter_sort": "最新",
        "filter_type": "文章",
        "filter_time": "最近七天",
        "filter_scope": "",
    },
    # AI 清洗第一层（规则过滤）参数，全部可在 config.json 覆盖。
    "cleaning": {
        "enabled": True,
        # 广告/推广黑名单关键词（命中标题或正文即拦截）
        "advert_keywords": [
            "广告投放", "品牌推广", "合作咨询", "代理加盟", "招商加盟",
            "加微信领取", "扫码注册", "限时优惠", "点击链接",
            "会员充值", "积分兑换", "免费领取", "0 元购",
            "刷单返利", "投资理财", "博彩赌博", "色情服务",
            "贷款口子", "信用卡套现", "高息理财",
            "点击关注", "点赞转发", "抽奖活动", "砍一刀",
        ],
        # 正文最小字符数（低于视为低质）
        "min_content_words": 100,
        # 最大特殊字符占比（超过视为乱码/垃圾）
        "max_special_char_ratio": 0.3,
        # 内容有效期天数（超过视为过期）
        "valid_days_limit": 30,
        # 列表页 URL 正则（非正文页）
        "list_page_patterns": [
            r"/s\?.*type=\d+.*page=\d+",
            r"/category/\d+",
            r"/topic/",
            r"/list/",
            r"/archives/\d+",
        ],
        # 公众号 __biz 最小长度（存在且过短视为异常）
        "min_account_id_len": 10,
    },
    # 去重策略：basic=仅 URL/标题指纹；smart=三层(URL指纹→SHA256内容哈希→SimHash近似)。
    "dedup": {
        "mode": "smart",
        # SimHash 近似阈值（1.0 表示仅汉明距离=0，即指纹完全相同才判近似重复）。
        # 该值被两次收紧，同一病根：现行 SimHash 对同质公文（征集/评选通知）区分度不足。
        #   0.9(dist<=6)  → 语义无关文章被大量误判；
        #   0.95(dist<=3) → 仍然误判，194 篇真实语料实测误杀率约 12%，且距离 4/6 处反而
        #                   存在标题几乎相同的真转载，说明 3~6 这一段不携带有效信号；
        #   1.0(dist=0)   → 真转载（实测均落在距离 0）照旧拦下，无关文章不再被误杀。
        # 部分改写的转载由第②层 SHA256 精确兜底；放宽阈值并不能可靠捕获它，故无损失。
        "simhash_threshold": 1.0,
        # 正文短于此长度时不做内容哈希/SimHash（避免空正文误判为重复）
        "min_content_len": 30,
    },
    # 分布式投递：enabled=false(默认) 走本地 SQLite；true 则把采集到的文章投递到
    # Celery(Redis broker)，由 worker 端做 PostgreSQL 三层去重入库。
    "distributed": {
        "enabled": False,
        # Celery broker / result backend（对齐 docker-compose 的 redis：宿主机用 localhost，带 requirepass 密码）。
        "broker_url": "redis://:your_redis_password@localhost:6379/0",
        "result_backend": "redis://:your_redis_password@localhost:6379/1",
        # worker 端注册的任务名（-A wxsearch.tasks 启动，故为完整点分路径）。
        "task_name": "wxsearch.tasks.process_article_task",
        # wait_result=true：同步等 worker 返回真实去重结果(慢但计数精确，适合验证/低速)；
        # false：fire-and-forget 只投递不等待(高吞吐，采集器日志按“已投递”计数)。
        "wait_result": True,
        # 等待结果的超时秒数（仅 wait_result=true 时生效）。
        "result_timeout": 30.0,
    },
    # 无人值守循环：enabled=false(默认) 时 main.py 保持“跑一次即退出”；
    # true 时配合 --unattended，采集器长跑“领关键词→采集→上报→休眠→下一轮”。
    # broker/backend 复用 distributed 段，不在此重复配置。
    "unattended": {
        "enabled": False,
        # 逻辑调度渠道统一为 souyisou；wechat_pc 只描述设备/采集实现，不作队列身份。
        "channel": "souyisou",
        "vm_instance_id": "vm-01",
        "device_type": "pc",
        "max_keywords": 5,
        "idle_sleep_sec": 60,   # 领不到词时的休眠秒数
        "round_sleep_sec": 30,  # 一批采完后的休眠秒数
        "claim_timeout": 30,    # 调用 claim/report 任务等待结果的超时秒数
        # 独立心跳必须显著短于服务端 DEVICE_ONLINE_TIMEOUT（现网 600s）。
        "heartbeat_interval_sec": 60,
        "heartbeat_result_timeout_sec": 10,
        # 连续心跳失败达到阈值后，完成已领取批次但暂停领取新批次。
        "heartbeat_failure_threshold": 3,
        "report_retry_attempts": 3,
        "report_retry_backoff_sec": 2,
    },
    "logging": {"level": "INFO", "file": "logs/collector.log"},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并两个字典，override 优先。"""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class CollectConfig:
    max_scrolls: int = 30
    max_items_per_keyword: int = 200
    scroll_pause_sec: float = 1.2
    stop_after_no_new_rounds: int = 3
    fetch_url: bool = False


@dataclass
class DelayConfig:
    action_pause_sec: float = 0.8
    window_wait_sec: float = 8.0
    input_settle_sec: float = 1.0


@dataclass
class Selectors:
    main_window_class_candidates: List[str] = field(default_factory=list)
    main_window_name_candidates: List[str] = field(default_factory=list)
    search_edit_name_candidates: List[str] = field(default_factory=list)
    search_entry_keyword_candidates: List[str] = field(default_factory=list)
    search_result_window_name_candidates: List[str] = field(default_factory=list)
    article_tab_name_candidates: List[str] = field(default_factory=list)
    more_button_name_candidates: List[str] = field(default_factory=list)
    copy_link_menu_candidates: List[str] = field(default_factory=list)
    search_doc_keyword: str = "搜一搜"
    app_menu_button_class: str = "AppMenuButton"
    article_url_prefix: str = "https://mp.weixin.qq.com"
    chat_window_class: str = "mmui::MainWindow"
    search_entry_button_name: str = "搜一搜"
    search_entry_button_class: str = "mmui::XTabBarItem"
    qt_window_class_hint: str = "Qt"
    sidebar_search_offset_x: int = 30
    sidebar_search_offset_y: int = 350
    # 4.1.x 键盘入口逐台标定旋钮（相对最大化 Qt 主窗左上角，缺省=win10-0 实测值）。
    searchbox_chat_icon_x: int = 40
    searchbox_chat_icon_y: int = 120
    searchbox_entry_x: int = 167
    searchbox_entry_y: int = 65
    filter_row_labels: List[str] = field(default_factory=lambda: ["排序", "类型", "时间", "范围"])
    filter_sort: str = "最新"
    filter_type: str = "文章"
    filter_time: str = "最近七天"
    filter_scope: str = ""


@dataclass
class CleaningConfig:
    """AI 清洗第一层（规则过滤）参数。"""
    enabled: bool = True
    advert_keywords: List[str] = field(default_factory=list)
    min_content_words: int = 100
    max_special_char_ratio: float = 0.3
    valid_days_limit: int = 30
    list_page_patterns: List[str] = field(default_factory=list)
    min_account_id_len: int = 10


@dataclass
class DedupConfig:
    """去重策略参数。mode=basic|smart。"""
    mode: str = "smart"
    simhash_threshold: float = 1.0
    min_content_len: int = 30


@dataclass
class DistributedConfig:
    """分布式投递参数。enabled=false 时保持本地 SQLite 行为不变。"""
    enabled: bool = False
    broker_url: str = "redis://:your_redis_password@localhost:6379/0"
    result_backend: str = "redis://:your_redis_password@localhost:6379/1"
    task_name: str = "wxsearch.tasks.process_article_task"
    wait_result: bool = True
    result_timeout: float = 30.0


@dataclass
class UnattendedConfig:
    """无人值守循环参数。enabled=false 时 main.py 保持“跑一次即退出”。

    broker/backend 复用 DistributedConfig，不在此重复配置。
    """
    enabled: bool = False
    channel: str = "souyisou"
    vm_instance_id: str = "vm-01"
    device_type: str = "pc"
    max_keywords: int = 5
    idle_sleep_sec: int = 60
    round_sleep_sec: int = 30
    claim_timeout: int = 30
    heartbeat_interval_sec: int = 60
    heartbeat_result_timeout_sec: int = 10
    heartbeat_failure_threshold: int = 3
    report_retry_attempts: int = 3
    report_retry_backoff_sec: int = 2


@dataclass
class AppConfig:
    keywords: List[str]
    result_type: str
    db_path: str
    collect: CollectConfig
    delays: DelayConfig
    selectors: Selectors
    cleaning: CleaningConfig
    dedup: DedupConfig
    distributed: DistributedConfig
    unattended: UnattendedConfig
    log_level: str
    log_file: str

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AppConfig":
        merged = _deep_merge(_DEFAULTS, raw or {})
        return cls(
            keywords=list(merged["keywords"]),
            result_type=str(merged["result_type"]),
            db_path=str(merged["database"]["path"]),
            collect=CollectConfig(**merged["collect"]),
            delays=DelayConfig(**merged["delays"]),
            selectors=Selectors(**merged["selectors"]),
            cleaning=CleaningConfig(**merged["cleaning"]),
            dedup=DedupConfig(**merged["dedup"]),
            distributed=DistributedConfig(**merged["distributed"]),
            unattended=UnattendedConfig(**merged["unattended"]),
            log_level=str(merged["logging"]["level"]),
            log_file=str(merged["logging"]["file"]),
        )


def load_config(path: str = "config.json") -> AppConfig:
    """从 JSON 文件加载配置；文件不存在时使用默认配置。"""
    raw: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    return AppConfig.from_dict(raw)


def load_raw(path: str = "config.json") -> Dict[str, Any]:
    """读取原始 JSON（不做默认值合并）；文件不存在返回空字典。"""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(updates: Dict[str, Any], path: str = "config.json") -> Dict[str, Any]:
    """将 updates 递归合并到现有 config.json 并写回，返回合并后的完整配置。

    仅更新传入的键，其余保持不变（保留用户手写的其它字段）。
    """
    current = load_raw(path)
    merged = _deep_merge(current, updates or {})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged

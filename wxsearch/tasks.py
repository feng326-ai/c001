"""
Celery Tasks - 智能去重 Worker 任务定义
处理采集器提交的文章数据，自动去重并入库
"""

import celery
from celery import Celery
from celery.schedules import crontab
from datetime import datetime
import json
import logging
import os
import re
from urllib.parse import urlsplit


# ==================== 连接配置（从环境变量读取，与 docker-compose 一致） ====================

_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _db_config() -> dict:
    """从 DATABASE_URL 解析 PostgreSQL 连接参数；缺省回退到 compose 默认值。

    形如 postgresql://admin:pwd@postgres:5432/wx_search
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
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", "5432")),
        "database": os.getenv("POSTGRES_DB", "wx_search"),
        "user": os.getenv("POSTGRES_USER", "admin"),
        "password": os.getenv("POSTGRES_PASSWORD", "your_secure_password_here"),
    }


# ==================== 配置 Celery 实例 ====================

celery_app = Celery(
    'articles',
    broker=_REDIS_URL,
    backend=_REDIS_URL.rsplit('/', 1)[0] + '/1'
)

# 配置日志
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


# ==================== 导入项目模块 ====================

from wxsearch.models import Article, QualifiedLead
from wxsearch.smart_dedup_store import SmartDedupStore
from wxsearch.content_hasher import ContentHasher
from wxsearch.ai_filters.ai_analyzer import AIAnalyzer

# AI 分析层：worker 进程级单例，开关由环境变量 AI_ENABLED 控制（默认关闭）。
# 关闭时 analyze() 立即返回“跳过”，不影响既有去重入库流程。
_ai_analyzer = AIAnalyzer.from_env()


# ==================== 无人值守调度：开关 + beat 周期配置 ====================

def _truthy(val) -> bool:
    """环境变量布尔解析（缺省/无法识别一律 False）。"""
    return str(val or "").strip().lower() in {"1", "true", "yes", "on"}


def _scheduler_enabled() -> bool:
    """调度地基总开关，默认开启；设 0/false 关闭全部周期任务。"""
    return _truthy(os.getenv("SCHEDULER_ENABLED", "true"))


def _stale_claim_minutes() -> int:
    """崩溃遗留判定阈值（分钟），默认 15。"""
    try:
        return int(os.getenv("SCHEDULER_STALE_CLAIM_MINUTES", "15"))
    except (TypeError, ValueError):
        return 15


# celery-beat 周期调度：到期重排 / 崩溃自愈 / 健康心跳（秒为单位）
celery_app.conf.timezone = "Asia/Shanghai"
celery_app.conf.beat_schedule = {
    "requeue-due-keywords": {
        "task": "wxsearch.tasks.requeue_due_keywords_task",
        "schedule": 300.0,
    },
    "recover-stale-claims": {
        "task": "wxsearch.tasks.recover_stale_claims_task",
        "schedule": 300.0,
    },
    "heartbeat": {
        "task": "wxsearch.tasks.heartbeat_task",
        "schedule": 60.0,
    },
    "alert-check": {
        "task": "wxsearch.tasks.alert_check_task",
        "schedule": 60.0,
    },
    # 数据备份：每日凌晨 3:30 全库 pg_dump（避开整点，错峰）。
    "daily-backup": {
        "task": "wxsearch.tasks.backup_database_task",
        "schedule": crontab(hour=3, minute=30),
    },
    # 后台 LLM 清洗：每 60s 一轮，按优先级批量补全线索活动信息（开关 LLM_CLEAN_ENABLED，默认关）。
    "llm-clean": {
        "task": "wxsearch.tasks.llm_clean_task",
        "schedule": 60.0,
    },
    # 设备监控：每 60s 检测离线设备(心跳超时)→置 offline+告警；
    # 任务由 stale lease recovery 延迟回收，避免无 fencing token 时并发采集。
    "device-monitor": {
        "task": "wxsearch.tasks.device_monitor_task",
        "schedule": 60.0,
    },
    # 主办方库聚合：每 30 分钟全量重建 organizers 档案（纯 SQL 聚合、不调 LLM、开关 SCHEDULER_ENABLED）。
    "rebuild-organizers": {
        "task": "wxsearch.tasks.rebuild_organizers_task",
        "schedule": 1800.0,
    },
    # 本地采集库 -> 正式业务库：事务发件箱每分钟发送一批；默认关闭。
    "production-sync": {
        "task": "wxsearch.tasks.production_sync_task",
        "schedule": 60.0,
    },
}


# ==================== 核心任务：文章入库去重 ====================

@celery_app.task(name="wxsearch.tasks.production_sync_task")
def production_sync_task():
    """发送一批待同步事件；开关、鉴权和重试策略由 production_sync 管理。"""
    from wxsearch.production_sync import sync_once
    return sync_once()


@celery_app.task(bind=True, max_retries=3)
def process_article_task(self, article_json: str):
    """
    处理单篇文章：提取 → 去重 → 入库
    
    Args:
        article_json: JSON 格式的文章对象
    
    Returns:
        {"success": bool, "reason": str, "id": int}
    """
    
    try:
        # 1. 反序列化 JSON
        article_dict = json.loads(article_json)
        
        # 2. 转换为 Article 对象
        article = Article(**article_dict)
        
        # 3. 初始化去重服务
        store = SmartDedupStore(_db_config())
        
        # 4. 执行去重插入
        success, reason = store.add_article(article)
        
        result = {
            "success": success,
            "reason": reason,
            "channel": article.source_channel,
            "keyword": article.keyword,
            "title": article.title[:50]
        }
        
        if success:
            log.info(f"✅ 新增成功 ({result['channel']}): {result['title']}")

            # 5. AI 分析层（可开关，默认关闭）：价值评分/意图分类/线索识别。
            #    关闭时 analyze() 直接返回 analyzed=False，下方逻辑整体跳过。
            ai = _ai_analyzer.analyze(article)
            if ai.analyzed:
                result["ai"] = ai.to_dict()
                # 把评分回写 articles_core 的 7 个 AI 字段（按 add_article 的 RETURNING id 定位）。
                m = re.search(r"ID:(\d+)", reason)
                article_id = int(m.group(1)) if m else None
                if article_id is not None:
                    store.save_scoring(article_id, ai)
                if ai.is_lead:
                    result["is_lead"] = True
                    # 高价值线索提升进 qualified_leads 管理表，供看板查看/标注/跟进。
                    if article_id is not None:
                        store.promote_lead(article_id)
                    log.info(f"⭐ 线索命中 [{ai.priority_level}/{ai.intent_category}]: {result['title']}")

        else:
            log.info(f"⚠️ 重复跳过 ({result['channel']}): {result['title']} - {reason}")
        
        store.close()
        
        return result
        
    except Exception as exc:
        # 重试机制：最多重试 3 次
        log.error(f"❌ 处理失败，准备重试：{exc}")
        
        raise self.retry(exc=exc, countdown=60)  # 60 秒后重试


# ==================== 批量任务：批量处理文章 ====================

@celery_app.task(bind=True, max_retries=3)
def process_batch_articles(self, articles_json_list: list):
    """
    批量处理文章列表
    
    Args:
        articles_json_list: 文章 JSON 列表
    
    Returns:
        {"total": N, "new": X, "exact_duplicate": Y, "similar_duplicate": Z, "errors": W}
    """
    
    try:
        # 1. 转换为 Article 对象
        articles = [Article(**json.loads(item)) for item in articles_json_list]
        
        # 2. 初始化去重服务
        store = SmartDedupStore(_db_config())
        
        # 3. 批量插入
        stats = store.bulk_insert(articles)
        
        result = {
            "total": stats["total"],
            "new": stats["new"],
            "url_duplicate": stats["url_duplicate"],
            "exact_duplicate": stats["exact_duplicate"],
            "similar_duplicate": stats["similar_duplicate"],
            "errors": stats["errors"]
        }
        
        log.info(f"✅ 批量处理完成：{json.dumps(result)}")
        
        store.close()
        
        return result
        
    except Exception as exc:
        log.error(f"❌ 批量处理失败，准备重试：{exc}")
        raise self.retry(exc=exc, countdown=120)


# ==================== 内容指纹分析任务 ====================

@celery_app.task
def analyze_content_fingerprints(articles_json_list: list):
    """
    分析一批文章的内容指纹统计信息
    
    Args:
        articles_json_list: 文章 JSON 列表
    
    Returns:
        指纹统计分析结果
    """
    
    try:
        from wxsearch.content_hasher import ContentHasher
        
        # 转换 Article 对象
        articles = [Article(**json.loads(item)) for item in articles_json_list]
        
        # 批量处理
        stats = ContentHasher.batch_process(articles)
        
        log.info(f"📊 指纹分析完成：{stats}")
        
        return stats
        
    except Exception as e:
        log.error(f"❌ 指纹分析失败：{e}")
        return {"error": str(e)}


# ==================== 镜像链接统计任务 ====================

@celery_app.task
def mirror_link_statistics(days: int = 7):
    """
    统计最近 N 天的镜像链接情况
    
    Args:
        days: 统计天数
    
    Returns:
        镜像统计数据
    """
    
    try:
        import psycopg2.extras
        from wxsearch.db_connector import DatabaseConnector
        
        db = DatabaseConnector()
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        cur.execute("""
            SELECT 
                ac.content_hash,
                ac.mirror_count,
                ARRAY_AGG(DISTINCT am.mirror_source_channel) as channels,
                COUNT(DISTINCT am.mirror_url) as total_mirrors
            FROM article_clusters ac
            LEFT JOIN article_mirrors am ON ac.id = am.cluster_id
            WHERE ac.created_at > NOW() - make_interval(days => %s)
            GROUP BY ac.id, ac.content_hash, ac.mirror_count
            ORDER BY ac.mirror_count DESC
            LIMIT 100
        """, (days,))
        
        results = [dict(row) for row in cur.fetchall()]
        
        log.info(f"📈 镜像统计完成：{len(results)} 个集群")
        
        return {
            "days": days,
            "total_clusters": len(results),
            "clusters": results[:10]  # 仅返回前 10 个
        }
        
    except Exception as e:
        log.error(f"❌ 镜像统计失败：{e}")
        return {"error": str(e)}
    
    finally:
        db.close()


# ==================== 无人值守：周期调度 / 自愈 / 心跳任务 ====================
#
# 由 celery-beat 定时触发，在 worker 执行。三个任务统一：
#   - 开头判断 SCHEDULER_ENABLED，关闭则直接跳过（与 AI 层同款“默认可跑、可关”）；
#   - 惰性 DistributedTaskScheduler.from_env()，避免 import 期连 Redis；
#   - 任何异常只 log 不抛，绝不拖垮 worker。

@celery_app.task
def requeue_due_keywords_task():
    """到期重排：把到期的 completed 词翻回 pending，维持 20 分钟周期自转。"""
    if not _scheduler_enabled():
        return {"skipped": "scheduler_disabled"}
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        n = DistributedTaskScheduler.from_env().requeue_due_keywords()
        return {"requeued": n}
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 到期重排任务异常：{e}")
        return {"error": str(e)}


@celery_app.task
def recover_stale_claims_task():
    """崩溃自愈：回收崩溃/掉线 VM 遗留的 running 词，复位为 pending。"""
    if not _scheduler_enabled():
        return {"skipped": "scheduler_disabled"}
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        recovered = DistributedTaskScheduler.from_env().recover_stale_claims(
            _stale_claim_minutes()
        )
        return {"recovered": recovered}
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 崩溃自愈任务异常：{e}")
        return {"error": str(e)}


@celery_app.task
def heartbeat_task():
    """健康心跳：将 PG/Redis/池状态写入 Redis（TTL 120s），供后续告警使用。"""
    if not _scheduler_enabled():
        return {"skipped": "scheduler_disabled"}
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        sched = DistributedTaskScheduler.from_env()
        snapshot = sched.health_snapshot()
        sched.redis.setex(
            sched.HEARTBEAT_KEY, 120, json.dumps(snapshot, ensure_ascii=False)
        )
        log.info(f"💓 心跳：{snapshot}")
        return snapshot
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 心跳任务异常：{e}")
        return {"error": str(e)}


@celery_app.task
def alert_check_task():
    """告警巡查：基于心跳/基础设施可达性/连续失败产出告警（日志 + 可选 webhook）。

    开关由 ALERT_ENABLED 控制（默认开）；同键告警有冷却去重；任何异常只 log 不抛。
    """
    try:
        from wxsearch.alerting import Alerter
        return Alerter.from_env().evaluate_and_alert()
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 告警巡查任务异常：{e}")
        return {"error": str(e)}


@celery_app.task
def backup_database_task():
    """数据备份：每日全库 pg_dump → 落盘宿主机卷 → 保留最近 N 份。

    开关由 BACKUP_ENABLED 控制（默认开）；任何异常只 log 不抛。
    """
    try:
        from wxsearch.backup import Backuper
        return Backuper.from_env().run_backup()
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 数据备份任务异常：{e}")
        return {"error": str(e)}


@celery_app.task
def rebuild_organizers_task():
    """主办方库聚合重建（beat 每 30 分钟）：把线索表已抽出的主办方字段聚合成 organizers 档案。

    纯 SQL 聚合、不调 LLM，与主办方库独立模块口径一致；开关 SCHEDULER_ENABLED；异常只 log 不抛。
    """
    if not _scheduler_enabled():
        return {"skipped": "scheduler_disabled"}
    try:
        from wxsearch.db_connector import DatabaseConnector
        from wxsearch.organizer_aggregator import rebuild_organizers
        return rebuild_organizers(DatabaseConnector())
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 主办方库重建任务异常：{e}")
        return {"error": str(e)}


@celery_app.task
def llm_clean_task():
    """后台 LLM 清洗：按优先级捞一批待清洗线索交大模型补全活动信息/价值判定。

    开关由 docker-compose LLM_CLEAN_ENABLED > rule_config.llm.clean_enabled 控制（默认关）；
    与采集入库彻底解耦，失败自动重试/熔断，绝不阻断主流程。任何异常只 log 不抛。
    """
    # 环境变量是部署角色的硬开关；未显式设置时才使用共享规则配置。
    env_clean = os.getenv("LLM_CLEAN_ENABLED", "false").lower() == "true"
    try:
        from wxsearch.ai_filters.llm_client import get_clean_enabled
        active = get_clean_enabled(default=env_clean)
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 检查清洗开关失败，降级用环境变量：{e}")
        active = env_clean
    if not active:
        return {"skipped": "clean_disabled"}
    try:
        from wxsearch.llm_cleaner import LLMCleaner
        return LLMCleaner.from_env().run_once()
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ LLM 清洗任务异常：{e}")
        return {"error": str(e)}


# ==================== 无人值守：VM 领取 / 上报薄任务 ====================
#
# 供装微信的 VM 侧采集器用 send_task(...).get() 调用（VM 只带 celery+redis 客户端，
# 不装 psycopg2、不直连 PG）。真正的 Redis 锁 + PG 读写都在 worker 端执行。
# 与其它调度任务同款风格：惰性 import scheduler、异常只 log 不抛、返回 JSON 可序列化结果。

@celery_app.task
def claim_keywords_task(channel: str, vm_instance_id: str, max_keywords: int = 5,
                        lease_aware: bool = False):
    """VM 领取任务；新节点可请求带 lease_id 的 v2 响应，旧节点仍得到字符串列表。"""
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        kws = DistributedTaskScheduler.from_env().claim_task(
            channel=channel, vm_instance_id=vm_instance_id, max_keywords=max_keywords,
            lease_aware=lease_aware,
        )
        return list(kws)
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 领取关键词任务异常（{channel}/{vm_instance_id}）：{e}")
        return []


@celery_app.task
def report_result_task(keyword: str, articles_count: int, success: bool,
                       error_message: str = None, device_id: str = None,
                       channel: str = None, lease_id: str = None):
    """VM 上报单个关键词的采集结果。返回 bool（异常时返回 False，不拖垮 VM 循环）。

    device_id/channel：设备级归属（写 collect_tasks 历史，支撑每机统计）。
    """
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        return bool(DistributedTaskScheduler.from_env().report_result(
            keyword=keyword, articles_count=articles_count,
            success=success, error_message=error_message,
            device_id=device_id, channel=channel, lease_id=lease_id,
        ))
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 上报结果任务异常（{keyword}）：{e}")
        return False


@celery_app.task
def heartbeat_device_task(device_id: str, device_type: str = "pc",
                          channel: str = "souyisou", current_keyword: str = None,
                          active_keywords=None):
    """VM/设备上报心跳并续租活跃词。第五参数可选，兼容旧 VM 四参数调用。"""
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        return bool(DistributedTaskScheduler.from_env().heartbeat_device(
            device_id=device_id, device_type=device_type,
            channel=channel, current_keyword=current_keyword,
            active_keywords=active_keywords,
        ))
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 设备心跳任务异常（{device_id}）：{e}")
        return False


@celery_app.task
def device_drain_status_task(device_id: str, channel: str = "souyisou"):
    """受控灰度使用的设备排空探针；只返回布尔态与数量。"""
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        return DistributedTaskScheduler.from_env().device_drain_status(
            device_id=device_id,
            channel=channel,
        )
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 设备排空探针异常（{device_id}/{channel}）：{e}")
        return {
            "drained": False,
            "owned_claims": -1,
            "current_keyword_active": True,
            "channel_match": False,
            "protocol_floor": -1,
        }


@celery_app.task
def device_monitor_task():
    """设备级监控（beat 每 60s）：

    1. 离线检测：心跳超时的设备置 offline 并告警；关键词等待 stale lease recovery；
    2. 连续失败：某设备 fail_streak 超阈值告警（可能微信掉线/被封）。
    复用 alerting.Alerter（冷却去重 + 可选 webhook）；异常只 log，不拖垫 beat。
    """
    try:
        from wxsearch.task_scheduler import DistributedTaskScheduler
        from wxsearch.alerting import Alerter, Alert

        sched = DistributedTaskScheduler.from_env()
        alerter = Alerter.from_env()
        timeout = int(os.getenv("DEVICE_ONLINE_TIMEOUT", "600"))
        threshold = int(os.getenv("DEVICE_FAIL_THRESHOLD", "3"))

        alerts = []
        # 1. 离线检测。没有 fencing token 前禁止首次离线立即释放，避免旧执行者
        # 与新 owner 同时采集；停止心跳后由 recover_stale_claims 延迟回收。
        newly_offline = sched.mark_offline_devices(timeout_seconds=timeout)
        for dev in newly_offline:
            alerts.append(Alert(
                f"device_offline:{dev}", "warning", f"设备离线：{dev}",
                f"设备 {dev} 心跳超时（>{timeout}s）已置离线；其关键词将在租约到期后安全回收。"))

        # 2. 连续失败告警
        try:
            rows = sched._db().execute_query("SELECT device_id FROM devices WHERE status = 'online'")
            for (dev,) in (rows or []):
                v = sched.redis.get(f"wxsearch:device:fail_streak:{dev}")
                fs = int(v) if v else 0
                if fs >= threshold:
                    alerts.append(Alert(
                        f"device_fail_streak:{dev}", "warning", f"设备连续失败：{dev}",
                        f"设备 {dev} 连续采集失败 {fs} 次（阈值 {threshold}），可能微信掉线/被封，请检查。"))
        except Exception as e:  # noqa: BLE001
            log.error(f"设备失败流检查异常：{e}")

        sent = alerter.dispatch(alerts) if (alerter.enabled and alerts) else []
        if newly_offline:
            log.warning(f"[设备监控] 新离线 {len(newly_offline)} 台：{newly_offline}，等待租约到期回收")
        return {"newly_offline": newly_offline, "released": {},
                "alerts": [a.key for a in alerts], "sent": sent}
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 设备监控任务异常：{e}")
        return {"error": str(e)}


# 采集参数默认值（与 api/main.py 的 _DEFAULT_COLLECT_SETTINGS 保持一致）
_DEFAULT_COLLECT_SETTINGS = {
    "wechat": {
        "filter_sort": "最新", "filter_type": "文章", "filter_time": "最近一天",
        "filter_scope": "", "max_items_per_keyword": 200, "max_scrolls": 30,
    },
    "sogou": {
        "enabled": False, "filter_time": "最近一天", "filter_type": "文章",
        "max_items_per_keyword": 100,
        "batch": 5, "interval_seconds": 60, "concurrency": 1, "proxies": [],
    },
}


@celery_app.task
def get_collection_settings_task():
    """VM 拉取采集参数（搜一搜/搜狗筛选）。读 rule_config.json 的 collect_settings，
    以内置默认为底合并（缺字段不报错）。异常时返回默认，不拖垮 VM 循环。"""
    try:
        from wxsearch.ai_filters.rule_scorer import load_rule_config
        saved = (load_rule_config() or {}).get("collect_settings", {}) or {}
        result = {}
        for group, defaults in _DEFAULT_COLLECT_SETTINGS.items():
            merged = dict(defaults)
            merged.update(saved.get(group, {}) or {})
            result[group] = merged
        return result
    except Exception as e:  # noqa: BLE001
        log.error(f"❌ 拉取采集参数任务异常：{e}")
        return dict(_DEFAULT_COLLECT_SETTINGS)


# ==================== 使用示例 ====================

if __name__ == "__main__":
    import json
    from wxsearch.models import Article
    
    # 模拟一篇文章的 JSON
    article = Article(
        title="测试文章",
        content="文章内容...",
        url="https://mp.weixin.qq.com/s?__biz=xxx&mid=yyy&idx=zzz&sn=aaa",
        source_channel="wechat_pc",
        keyword="测试关键词",
        account="测试公众号",
        account_id="test123456789",
        publish_time="2026-08-12 10:00"
    )
    
    article_json = json.dumps(article.__dict__, ensure_ascii=False)
    
    print("=== 单篇处理测试 ===")
    result = process_article_task(article_json)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    print("\n=== 批量处理测试 ===")
    batch_articles = [
        Article(title=f"测试{i}", content=f"内容{i}", url=f"https://example.com/{i}",
                source_channel="wechat_pc", keyword="测试").__dict__
        for i in range(5)
    ]
    
    batch_json = [json.dumps(a, ensure_ascii=False) for a in batch_articles]
    batch_result = process_batch_articles(batch_json)
    print(json.dumps(batch_result, indent=2, ensure_ascii=False))

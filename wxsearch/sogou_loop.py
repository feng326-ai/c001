"""搜狗微信采集 · 常驻循环（在采集机/VM 上后台长跑）。

循环：拉配置(HTTP) → 领词(claim) → 并发采集(SogouDriver 线程池) → 投递入库(DistributedSink) → 上报+心跳 → sleep → 再来。

管理端联动（/admin/sogou 页面）：
    每轮开头 GET {API_BASE}/api/v1/settings/collection 拉 collect_settings.sogou，
    启用开关/时间档/每轮词数/轮间隔/每词上限/并发数/代理池 —— 页面改完下一轮即生效，无需重启。
    运行日志挂 RemoteLogHandler 批量 POST {API_BASE}/api/v1/collect_logs/report，管理页实时看。

端点全部走环境变量（不得在源码中提供真实地址或凭据）：
    REDIS_URL     Celery broker
    DATABASE_URL  任务调度数据库
    API_BASE      配置拉取/日志上报地址
    SOGOU_API_TOKEN 日志上报令牌（与服务端 _COLLECT_LOG_TOKEN 一致）

可用环境变量覆盖的运行参数（页面未配置时的回退值）：
    SOGOU_DEVICE   设备号（设备监控页显示），默认 sogou-vm-01
    SOGOU_TIME     时间档文本（一天/一周/一月/一年），默认 一天
    SOGOU_BATCH    每轮领词数，默认 5
    SOGOU_INTERVAL 每轮间隔秒，默认 60
    SOGOU_MAX_ITEMS 每词最多采集条数，默认 30
    SOGOU_CONCURRENCY 并发 worker 数，默认 1
    SOGOU_BROWSER_RESTART 每 worker 每 N 个词重启浏览器防泄漏，默认 50

设计：单次异常绝不退出（记日志继续下一轮/下一词）；采集出错或定期到点则重建该 worker 浏览器。
配合外层自动重启脚本(start_sogou_loop.bat)，进程若整体退出会被拉起，实现多日常驻。
"""
from __future__ import annotations

import itertools
import logging
import os
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured in the runtime environment")
    return value


_required_env("REDIS_URL")
_required_env("DATABASE_URL")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

CHANNEL = "sogou"
DEVICE = os.getenv("SOGOU_DEVICE", "sogou-vm-01")
API_BASE = _required_env("API_BASE").rstrip("/")
API_TOKEN = _required_env("SOGOU_API_TOKEN")
BROWSER_RESTART = int(os.getenv("SOGOU_BROWSER_RESTART", "50") or 50)


def _env_defaults() -> dict:
    """环境变量回退值（服务端拉不到配置 / 字段缺失时用）。"""
    return {
        "enabled": True,   # 默认跑（本机常驻进程的使命就是采集）；页面关掉才停
        "filter_time": os.getenv("SOGOU_TIME", "一天"),
        "max_items_per_keyword": int(os.getenv("SOGOU_MAX_ITEMS", "30") or 30),
        "batch": int(os.getenv("SOGOU_BATCH", "5") or 5),
        "interval_seconds": int(os.getenv("SOGOU_INTERVAL", "60") or 60),
        "concurrency": int(os.getenv("SOGOU_CONCURRENCY", "1") or 1),
        "proxies": [],
    }


# ==================== 远程配置（每轮拉取，页面改完下一轮生效） ====================

def fetch_remote_settings(last: dict | None) -> dict:
    """GET /api/v1/settings/collection 取 collect_settings.sogou；失败回退上次值/环境变量。"""
    try:
        import requests
        r = requests.get(f"{API_BASE}/api/v1/settings/collection", timeout=10)
        r.raise_for_status()
        s = r.json().get("sogou", {}) or {}
        base = last or _env_defaults()
        merged = dict(base)
        for k in base:
            if k in s and s[k] is not None:
                merged[k] = s[k]
        return merged
    except Exception as e:  # noqa: BLE001
        if last:
            logging.getLogger("sogou-loop").warning(f"拉取远程配置失败，沿用上次配置：{e}")
            return dict(last)
        logging.getLogger("sogou-loop").warning(f"拉取远程配置失败，用环境变量默认：{e}")
        return _env_defaults()


def parse_proxy(s: str) -> dict | None:
    """页面一行代理 → Playwright proxy dict。支持 host:port / http://host:port / http://user:pass@host:port。"""
    s = (s or "").strip()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    p = urlparse(s)
    if not p.hostname or not p.port:
        return None
    d = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        d["username"] = p.username
    if p.password:
        d["password"] = p.password
    return d


def _settings_signature(s: dict) -> tuple:
    """用于检测配置是否变化（变了才重建 worker/驱动）。"""
    return (int(s.get("concurrency") or 1),
            tuple(s.get("proxies") or []),
            str(s.get("filter_time") or ""),
            int(s.get("max_items_per_keyword") or 30))


# ==================== 远程日志上报（管理页实时看日志/错误提醒） ====================

class RemoteLogHandler(logging.Handler):
    """缓冲日志行，定时/攒够批量 POST 到服务端 collect_logs。失败静默（不影响采集主流程）。"""

    def __init__(self, device_id: str, base_url: str, token: str,
                 flush_interval: float = 20.0, max_buffer: int = 100):
        super().__init__(level=logging.INFO)
        self.device_id = device_id
        self.url = f"{base_url}/api/v1/collect_logs/report?x_token={token}"
        self.flush_interval = flush_interval
        self.max_buffer = max_buffer
        self._buf: list = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s"))

    def emit(self, record):
        try:
            line = self.format(record)
            with self._lock:
                self._buf.append({"device_id": self.device_id,
                                  "level": record.levelname, "message": line[:990]})
                need = len(self._buf) >= self.max_buffer
            if need or time.time() - self._last_flush >= self.flush_interval:
                self.flush()
        except Exception:  # noqa: BLE001
            pass

    def flush(self):
        with self._lock:
            items, self._buf = self._buf, []
            self._last_flush = time.time()
        if not items:
            return
        try:
            import requests
            requests.post(self.url, json={"logs": items}, timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("sogou-loop")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
        logger.addHandler(RemoteLogHandler(DEVICE, API_BASE, API_TOKEN))
    return logger


# ==================== 采集配置对象 ====================

def make_config(settings: dict):
    """每轮按最新设置重建配置对象（时间档/每词上限变化即时生效）。"""
    broker = os.environ["REDIS_URL"]
    dist = types.SimpleNamespace(
        enabled=True, broker_url=broker,
        result_backend=broker.rsplit("/", 1)[0] + "/1",
        task_name="wxsearch.tasks.process_article_task",
        wait_result=True, result_timeout=60.0,
    )
    collect = types.SimpleNamespace(
        max_items_per_keyword=int(settings.get("max_items_per_keyword") or 30),
        max_scrolls=5, fetch_url=True)
    selectors = types.SimpleNamespace(filter_time=settings.get("filter_time") or "一天")
    unattended = types.SimpleNamespace(channel=CHANNEL, vm_instance_id=DEVICE, device_type="pc")
    return types.SimpleNamespace(distributed=dist, collect=collect,
                                 selectors=selectors, unattended=unattended)


def main():
    log = _setup_logging()
    from wxsearch.task_scheduler import DistributedTaskScheduler
    from wxsearch.collectors.sogou_pw import SogouDriver
    from wxsearch.distributed_sink import DistributedSink

    sched = DistributedTaskScheduler.from_env()
    try:
        sched.seed_keyword_channels([CHANNEL])   # 确保 sogou kcs 已播种（幂等）
    except Exception as e:  # noqa: BLE001
        log.warning(f"播种 kcs 失败（继续）：{e}")

    settings = fetch_remote_settings(None)
    cfg = make_config(settings)
    sink = DistributedSink(cfg.distributed, log)
    sig = _settings_signature(settings)

    # worker 池：每个 worker 一个独立 SogouDriver（独立浏览器/代理）
    drivers: dict = {}            # worker_id -> SogouDriver
    worker_counts: dict = {}      # worker_id -> 已采词数（达 BROWSER_RESTART 重建防泄漏）
    counters = itertools.count(1)  # 全局轮次号（日志可读）
    pool: ThreadPoolExecutor | None = None
    pool_size = 0

    def rebuild_pool(n: int):
        nonlocal pool, pool_size
        for d in drivers.values():
            try: d.close()
            except Exception: pass  # noqa: E722
        drivers.clear(); worker_counts.clear()
        if pool is not None:
            pool.shutdown(wait=False)
        pool = ThreadPoolExecutor(max_workers=n)
        pool_size = n
        log.info(f"并发池已按配置重建：{n} 个 worker")

    def worker_run(worker_id: int, kw: str):
        """单关键词采集（在池线程内跑；每 worker 独占一个浏览器）。"""
        round_no = next(counters)
        proxies = settings.get("proxies") or []
        proxy = parse_proxy(proxies[worker_id % len(proxies)]) if proxies else None
        drv = drivers.get(worker_id)
        need_new = drv is None or worker_counts.get(worker_id, 0) >= BROWSER_RESTART
        if need_new:
            if drv is not None:
                try: drv.close()
                except Exception: pass  # noqa: E722
            drv = SogouDriver(cfg, log, proxy=proxy)
            drivers[worker_id] = drv
            worker_counts[worker_id] = 0
            if proxy:
                log.info(f"[w{worker_id}] 浏览器已绑定代理 {proxy['server']}")
        new = 0
        try:
            page = drv.open_search(kw)
            drv.apply_filters(page)
            for art in drv.iter_articles(page, kw):
                if sink.save(art):
                    new += 1
            sched.report_result(kw, new, True, device_id=DEVICE, channel=CHANNEL)
            worker_counts[worker_id] = worker_counts.get(worker_id, 0) + 1
            log.info(f"[轮{round_no}][w{worker_id}] 「{kw}」入库 {new} 条")
        except Exception as e:  # noqa: BLE001
            log.exception(f"[轮{round_no}][w{worker_id}] 「{kw}」采集失败：{e}")
            try:
                sched.report_result(kw, 0, False, error_message=str(e)[:200],
                                    device_id=DEVICE, channel=CHANNEL)
            except Exception:  # noqa: BLE001
                pass
            # 页面/浏览器可能已坏 → 该 worker 重建
            try: drv.close()
            except Exception: pass  # noqa: E722
            drivers.pop(worker_id, None)

    log.info(f"搜狗常驻循环启动：device={DEVICE} 端点={API_BASE}")

    try:
        while True:
            # 每轮拉远程配置（/admin/sogou 页面改的参数在这里生效）
            settings = fetch_remote_settings(settings)

            if not settings.get("enabled", True):
                # 心跳保持在线，让设备页能看到"在线但已停用"
                try:
                    sched.heartbeat_device(DEVICE, device_type="pc", channel=CHANNEL)
                except Exception:  # noqa: BLE001
                    pass
                log.info("管理页已停用搜狗采集，60s 后重新检查开关")
                time.sleep(60)
                continue

            if _settings_signature(settings) != sig:
                log.info("检测到配置变化（并发/代理/时间档/每词上限），重建 worker 池与配置")
                cfg = make_config(settings)
                sig = _settings_signature(settings)
                rebuild_pool(int(settings.get("concurrency") or 1))
            if pool is None or pool_size != int(settings.get("concurrency") or 1):
                rebuild_pool(int(settings.get("concurrency") or 1))

            batch = int(settings.get("batch") or 5)
            interval = int(settings.get("interval_seconds") or 60)

            # 心跳：让设备监控页看到本机在线
            try:
                sched.heartbeat_device(DEVICE, device_type="pc", channel=CHANNEL)
            except Exception:  # noqa: BLE001
                pass

            try:
                kws = sched.claim_task(channel=CHANNEL, vm_instance_id=DEVICE, max_keywords=batch)
            except Exception as e:  # noqa: BLE001
                log.warning(f"领词失败，{interval}s 后重试：{e}")
                time.sleep(interval); continue

            if not kws:
                log.info(f"无可采关键词（可能都在周期内），{interval}s 后再领")
                time.sleep(interval); continue

            log.info(f"领到 {len(kws)} 个词，{pool_size} 并发开采：{kws}")
            futs = [pool.submit(worker_run, i % pool_size, kw) for i, kw in enumerate(kws)]
            for f in futs:
                try: f.result()
                except Exception as e:  # noqa: BLE001
                    log.warning(f"worker 异常（继续）：{e}")

            # 本轮结束兜底刷一次远程日志
            for h in log.handlers:
                if isinstance(h, RemoteLogHandler):
                    h.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("收到中断，退出。")
    finally:
        for d in drivers.values():
            try: d.close()
            except Exception: pass  # noqa: E722
        if pool is not None:
            try: pool.shutdown(wait=False)
            except Exception: pass  # noqa: BLE001
        for h in log.handlers:
            if isinstance(h, RemoteLogHandler):
                h.flush()
        try: sink.close()
        except Exception: pass  # noqa: BLE001


if __name__ == "__main__":
    main()

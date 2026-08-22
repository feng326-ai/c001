"""搜狗微信采集驱动：桌面 Playwright + 手机 MQQBrowser UA 伪装（无需模拟器）。

流程（源自 tools/_pw_multi.py 验证过的暖会话链）：
    m.sogou.com 暖会话 -> searchList?keyword -> 点微信 tab -> weixinwap 富页
    -> 时间筛选面板(一周内等) -> 追加式翻页抽取列表(标题/公众号/时间/跳转链)
    -> (可选)逐条打开 /link? 页，从 window.biz/mid/idx 拼 mp 真链 + 抽正文

产出统一 db.Article(source="sogou_weixin")，与 PC 搜一搜驱动接口一致
(open_search / apply_filters / iter_articles)，供 Collector 无感切换。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from urllib.parse import quote

from ..db import Article
from ..wechat_driver import DriverError

MQQ_UA = ("Mozilla/5.0 (Linux; U; Android 14; zh-cn;) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Version/4.0 Chrome/107.0.0.0 MQQBrowser/13.9 Mobile Safari/537.36")

# 时间档：搜狗筛选面板 span.conditionTime[data-type]  1天/2周/3月/4年
_DTYPE_BY_TEXT = [
    (("一天", "1天", "24小时", "today"), "1"),
    (("七天", "一周", "周", "week"), "2"),
    (("一月", "30天", "月", "month"), "3"),
    (("一年", "年", "year"), "4"),
]

# 列表抽取：li[id^=sogou_vr] 的 标题/公众号/时间(文本+unix)/跳转链
JS_EXTRACT = r"""() => {
  const out = [];
  document.querySelectorAll('li[id^="sogou_vr"]').forEach(li => {
    const a = li.querySelector('h4 a, .txt-box h4 a, a');
    const s2 = li.querySelector('.s2, span[data-sourcename]');
    const s3 = li.querySelector('.s3, span[data-lastmodified]');
    const link = li.querySelector('a[href*="/link?"]') || a;
    out.push({
      title: a ? a.innerText.trim() : '',
      account: s2 ? (s2.getAttribute('data-sourcename') || s2.innerText.trim()) : '',
      time: s3 ? s3.innerText.trim() : '',
      ts: s3 ? (s3.getAttribute('data-lastmodified') || '') : '',
      href: link ? link.getAttribute('href') : ''
    });
  });
  return JSON.stringify(out);
}"""

# /link? 页里 mp 文章标识变量（拼 mp 永久链的 __biz/mid/idx/sn）
JS_VARS = r"""() => ({biz: window.biz||'', mid: window.mid||'', idx: window.idx||'', sn: window.sn||''})"""


class SogouDriver:
    """搜狗微信采集驱动。接口对齐 WeChatSearchDriver：open_search/apply_filters/iter_articles。"""

    def __init__(self, config, logger, proxy: dict | None = None):
        self.cfg = config
        self.log = logger
        # proxy：Playwright 格式 {"server": "http://host:port", "username": "", "password": ""}，
        # None 表示直连；由调用方（搜狗循环）按管理页代理池轮询绑定。
        self.proxy = proxy or None
        col = getattr(config, "collect", None)
        self.max_items = int(getattr(col, "max_items_per_keyword", 50) or 50)
        self.max_pages = int(getattr(col, "max_scrolls", 30) or 30)
        # 是否逐条进 /link 取 mp 真链 + 正文（默认开：利于去重指纹与 LLM 清洗）
        self.fetch_detail = bool(getattr(col, "fetch_url", True))
        self.dtype = self._dtype()
        self.headless = True

        self._pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    # ---- 生命周期 ----
    def _ensure_browser(self):
        if self.page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        launch_kw = dict(headless=self.headless, args=["--disable-blink-features=AutomationControlled"])
        if self.proxy:
            launch_kw["proxy"] = self.proxy
        self.browser = self._pw.chromium.launch(**launch_kw)
        self.ctx = self.browser.new_context(
            user_agent=MQQ_UA, viewport={"width": 390, "height": 844},
            device_scale_factor=3, is_mobile=True, has_touch=True, locale="zh-CN")
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self.page = self.ctx.new_page()
        try:
            self.page.goto("https://m.sogou.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(1)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[搜狗] 暖会话失败（继续）：{exc}")

    def close(self):
        for closer in (
            lambda: self.browser and self.browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self.page = self.ctx = self.browser = self._pw = None

    def _dtype(self) -> str:
        sel = getattr(self.cfg, "selectors", None)
        text = str(getattr(sel, "filter_time", "") or "")
        for keys, code in _DTYPE_BY_TEXT:
            if any(k in text for k in keys):
                return code
        return "2"  # 默认一周内

    # ---- 采集接口（对齐 PC 驱动，供 Collector 复用）----
    def open_search(self, keyword: str):
        """搜索并进入微信垂类富页；返回 page 作为“窗口”。失败抛 DriverError。"""
        self._ensure_browser()
        page = self.page
        try:
            page.goto("https://m.sogou.com/web/searchList.jsp?keyword=" + quote(keyword),
                      wait_until="domcontentloaded", timeout=30000)
            try:
                page.click("#weixin", timeout=8000)
                page.wait_for_url("**weixinwap**", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.5)
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"搜狗搜索失败（{keyword}）：{exc}")
        if "发布时间" not in (page.content() or ""):
            raise DriverError(f"搜狗未拿到微信富页（可能被降级/反爬）：{keyword}")
        return page

    def apply_filters(self, page):
        """驱动筛选面板选时间档（AJAX 重载，点完等网络稳定）。失败不阻断，只 log。"""
        try:
            page.click("#select_start", timeout=8000)
            time.sleep(0.6)
            page.click(f'span.conditionTime[data-type="{self.dtype}"]', timeout=8000)
            time.sleep(0.6)
            page.click("#select_confirm", timeout=8000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[搜狗] 时间筛选未生效（继续采全部）：{exc}")

    def _items(self, page):
        try:
            return json.loads(page.evaluate(JS_EXTRACT))
        except Exception:  # noqa: BLE001
            return []

    def iter_articles(self, page, keyword: str):
        """追加式翻页抽取；按链接去重；逐条产出 db.Article。"""
        # 翻页：点 #next_page 等 DOM 增长，直到消失/不再增长/达上限
        for _ in range(self.max_pages - 1):
            cur = len(self._items(page))
            if cur >= self.max_items:
                break
            try:
                if not page.is_visible("#next_page"):
                    break
                page.click("#next_page", timeout=6000)
            except Exception:  # noqa: BLE001
                break
            grew = False
            for _ in range(15):
                time.sleep(1)
                if len(self._items(page)) > cur:
                    grew = True
                    break
            if not grew:
                break

        seen = set()
        count = 0
        for it in self._items(page):
            if count >= self.max_items:
                break
            href = it.get("href") or ""
            key = href or it.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            full = href if href.startswith("http") else ("https://weixin.sogou.com" + href if href else "")
            url, content = full, ""
            if self.fetch_detail and full:
                url, content = self._resolve_detail(full) or (full, "")
            art = Article(
                keyword=keyword,
                title=it.get("title", ""),
                account=it.get("account", ""),
                publish_time=self._pub_time(it),
                summary="",
                content=content,
                url=url or full,
                source="sogou_weixin",
            )
            count += 1
            yield art

    # ---- 明细：进 /link? 取 mp 真链(去重指纹) + 正文 ----
    def _resolve_detail(self, link_url: str):
        """打开 /link? 页：读 window.biz/mid/idx 拼 mp 规范链(用于去重指纹) + 抽正文。

        取不到 mp 标识时回退用原 /link 链。返回 (url, content)。
        """
        sub = None
        try:
            sub = self.ctx.new_page()
            sub.goto(link_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.5)
            v = sub.evaluate(JS_VARS)
            content = ""
            try:
                content = sub.inner_text("body") or ""
            except Exception:  # noqa: BLE001
                pass
            if v.get("biz") and v.get("mid"):
                # sn 常为空：__biz-mid-idx 已足够作去重指纹（worker _normalize_url 解析这 4 参）
                mp = ("https://mp.weixin.qq.com/s?__biz=%s&mid=%s&idx=%s&sn=%s"
                      % (v["biz"], v["mid"], v.get("idx", "1") or "1", v.get("sn", "") or ""))
                return mp, content
            return link_url, content
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"[搜狗] 明细解析失败（用跳转链）：{exc}")
            return link_url, ""
        finally:
            if sub is not None:
                try:
                    sub.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _pub_time(it: dict) -> str:
        """优先用 data-lastmodified(unix) 归一为 'YYYY-MM-DD HH:MM'，否则用文本。"""
        ts = it.get("ts") or ""
        try:
            if ts:
                return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            pass
        return it.get("time", "") or ""

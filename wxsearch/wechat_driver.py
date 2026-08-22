"""Weixin 4.0 PC 客户端 UI 自动化驱动。

架构要点（基于 Weixin 4.0 实测）：
- 主窗口：Name='微信'，ClassName='Chrome_WidgetWin_0'（Chromium 外壳）。
- 搜一搜：主窗口内的一个标签页 DocumentControl（RegexName 含「搜一搜」），
  而非独立窗口。
- 结果标题为 ButtonControl；点击后在新标签页打开公众号文章。
- 文章正文可直接读取其 DocumentControl 下全部 TextControl 文本。
- 文章真实 URL：点 ···（AppMenuButton）→ 菜单项「复制链接」→ 读剪贴板。
"""

from __future__ import annotations

import re
import time

import pyperclip
import uiautomation as auto

from .db import Article


class DriverError(Exception):
    """驱动层不可恢复错误。"""


class FilterNotApplied(Exception):
    """筛选未确认生效——可重试（重开搜索重新筛选）。

    绝不退化为采集「全部」的未筛选混合结果：宁可本轮跳过、下轮重试，
    也不能污染线索库。属可重试异常（非 DriverError），由 _open_and_filter
    捕获后重开搜索重试。
    """


# 发布时间文本模式（结果页与文章页通用）
_TIME_RE = re.compile(
    r"^(?:"
    r"\d+\s*(?:分钟|小时|天)前"
    r"|昨天|前天|刚刚"
    r"|\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2})?"
    r"|\d{1,2}月\d{1,2}日"
    r"|\d{4}-\d{1,2}-\d{1,2}(?:\s*\d{1,2}:\d{2})?"
    r")$"
)


class WeChatSearchDriver:
    def __init__(self, config, logger):
        self.cfg = config
        self.log = logger
        self.sel = config.selectors
        self.delays = config.delays
        auto.SetGlobalSearchTimeout(max(3, int(self.delays.window_wait_sec)))
        self._win = None

    # ---------------- 窗口 / 文档定位 ----------------
    def _find_search_window(self, timeout: float = 2):
        """查找搜一搜/文章所在的 Chromium 搜索窗口（Chrome_WidgetWin_0）。

        优先尝试搜索窗口自身名称（如「评选征集 - 搜一搜」含 Keyword），失败再回退
        到主窗名称（"微信"），兼容独立窗口模式和多关键词场景。
        """
        kw = getattr(self.sel, "search_doc_keyword", "搜一搜")
        # 1. 先试搜索结果窗名称（支持含 keyword 的自定义名）
        for name in getattr(self.sel, "search_result_window_name_candidates", ["搜一搜", "搜索"]):
            w = auto.WindowControl(searchDepth=1, Name=name, ClassName="Chrome_WidgetWin_0")
            if w.Exists(timeout):
                return w
            # 2. 若窗口名含 keyword → 直接命中（如 "评选征集 - 搜一搜"）
            try:
                kids = auto.GetRootControl().GetChildren()
            except Exception:  # noqa: BLE001
                continue
            for c in kids:
                if c.ClassName == "Chrome_WidgetWin_0":
                    cnm = (c.Name or "")
                    if kw and kw in cnm:
                        self._win = c
                        return c
        # 3. 回退：主窗名称
        for name in self.sel.main_window_name_candidates:
            w = auto.WindowControl(searchDepth=1, Name=name, ClassName="Chrome_WidgetWin_0")
            if w.Exists(timeout):
                return w
        return None

    def _find_chat_window(self):
        """查找微信聊天主窗口（mmui::MainWindow），左侧栏含搜一搜入口。

        仅适用 4.0：该窗 UIA 可读。（4.1.x 为 Qt 不透明窗，见 _find_qt_chat_window。）
        """
        for name in self.sel.main_window_name_candidates:
            w = auto.WindowControl(searchDepth=1, Name=name, ClassName=self.sel.chat_window_class)
            if w.Exists(2):
                return w
        return None

    def _find_qt_chat_window(self):
        """查找 4.1.x 的 Qt 聊天主窗（ClassName 含 Qt，如 Qt51514QWindowIcon）。

        该窗 UIA 不透明（GetChildren 为空），无法在其内定位搜一搜按钮，
        只能盲点左侧栏图标坐标。
        """
        hint = getattr(self.sel, "qt_window_class_hint", "Qt") or "Qt"
        names = set(self.sel.main_window_name_candidates)
        try:
            children = auto.GetRootControl().GetChildren()
        except Exception:  # noqa: BLE001
            return None
        for w in children:
            try:
                cn = w.ClassName or ""
                nm = (w.Name or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if hint in cn and nm in names:
                return w
        return None

    def _open_search_window(self, keyword=None):
        """打开（或聚焦）搜一搜窗口并返回之。

        4.0：聊天主窗（mmui）内有命名「搜一搜」ButtonControl，直接 Click。
        4.1.x：聊天主窗为 Qt 不透明窗，回退为盲点左侧栏「搜一搜」图标坐标；
        再回退为顶部搜索框 + 键盘选中「搜索网络结果」。
        """
        # 4.0 路径：mmui 主窗内命名按钮
        chat = self._find_chat_window()
        if chat is not None:
            try:
                chat.SetActive()
                time.sleep(self.delays.action_pause_sec)
                btn = chat.ButtonControl(
                    Name=self.sel.search_entry_button_name,
                    ClassName=self.sel.search_entry_button_class,
                )
                if not btn.Exists(3):
                    btn = chat.ButtonControl(Name=self.sel.search_entry_button_name)
                if btn.Exists(2):
                    btn.Click(simulateMove=False)
                    time.sleep(3)
                    w = self._find_search_window(5)
                    if w is not None:
                        self._win = w
                        return w
            except Exception:  # noqa: BLE001
                pass
        # 4.1.x 路径：Qt 主窗盲点侧栏图标
        w = self._open_search_via_qt_sidebar()
        if w is not None:
            self._win = w
            return w
        # 4.1.x 回退：顶部搜索框 + 粘贴 + Down/Enter 选中「搜索网络结果」
        w = self._open_search_via_qt_searchbox(keyword)
        if w is not None:
            self._win = w
            return w
        raise DriverError(
            "未能打开搜一搜窗口：未找到微信主窗（mmui 侧栏按钮、4.1.x Qt 盲点与搜索框键盘路径均失败）。"
            "请确认微信已登录并处于打开状态。"
        )

    def _open_search_via_qt_sidebar(self):
        """4.1.x：盲点 Qt 主窗左侧栏「搜一搜」图标坐标唤起搜索窗；失败返回 None。

        图标位于窗口相对 (offset_x, offset_y)（顶栏锚定，随窗移动稳定），坐标可在
        config selectors 的 sidebar_search_offset_x/y 调整。
        """
        qt = self._find_qt_chat_window()
        if qt is None:
            return None
        try:
            qt.SetActive()
            time.sleep(self.delays.action_pause_sec)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = qt.BoundingRectangle
            x = r.left + int(getattr(self.sel, "sidebar_search_offset_x", 30))
            y = r.top + int(getattr(self.sel, "sidebar_search_offset_y", 350))
            auto.Click(x, y)
            self.log.info(f"4.1.x：盲点 Qt 侧栏搜一搜图标 ({x},{y})，等待窗口…")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"盲点侧栏失败：{exc}")
            return None
        for _ in range(12):
            time.sleep(0.5)
            w = self._find_search_window(1)
            if w is not None:
                return w
        return None

    def _open_search_via_qt_searchbox(self, keyword=None):
        """4.1.x 回退入口：最大化 Qt 主窗 → 点聊天图标归一 → 点顶部搜索框 →
        粘贴关键词 → Down+Enter 选中下拉首条建议，唤起「关键词 - 搜一搜」标签页。

        实测（win10-0 / WeChat 4.1.12.55）：下拉层为 Chromium 渲染， guest 内
        合成鼠标点击无响应，但键盘选择（Down/Enter）可用；搜索窗复用常驻的
        Chrome_WidgetWin_0 容器窗，故成功判定以「文档名含搜一搜」为准。
        坐标相对窗口左上角，可在 config selectors 调整。
        """
        qt = self._find_qt_chat_window()
        if qt is None:
            return None
        try:
            qt.SetFocus()
            time.sleep(0.3)
            qt.GetWindowPattern().SetWindowVisualState(
                auto.WindowVisualState.Maximized
            )
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
        try:
            qt.SetActive()
            time.sleep(self.delays.action_pause_sec)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = qt.BoundingRectangle
            # 先点聊天图标，把面板归一到聊天列表（搜索框布局稳定）
            auto.Click(
                r.left + int(getattr(self.sel, "searchbox_chat_icon_x", 40)),
                r.top + int(getattr(self.sel, "searchbox_chat_icon_y", 120)),
            )
            time.sleep(0.8)
            # 点顶部搜索框唤起下拉
            auto.Click(
                r.left + int(getattr(self.sel, "searchbox_entry_x", 167)),
                r.top + int(getattr(self.sel, "searchbox_entry_y", 65)),
            )
            time.sleep(1.2)
            kw = keyword or "搜一搜"
            pyperclip.copy(kw)
            auto.SendKeys("{Ctrl}v")
            time.sleep(2.0)
            # 下拉首条建议（0 联系人命中时即关键词本身）→ Enter 打开搜一搜
            auto.SendKeys("{Down}")
            time.sleep(0.4)
            auto.SendKeys("{Enter}")
            self.log.info(f"4.1.x：搜索框键盘路径已提交（{kw}），等待搜一搜文档…")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"搜索框键盘路径失败：{exc}")
            return None
        # 4.1.12+：搜索面板默认内嵌在 Qt 主窗内，UIA 读不到其文档；
        # 先短等独立窗，没有则点面板右上「弹出独立窗口」方框图标脱离成
        # Chrome_WidgetWin_0（最大化下实测位于 right-59/top+57），再等文档可读。
        for _ in range(4):
            time.sleep(0.5)
            w = self._find_search_window(1)
            if w is not None and self._has_search_doc(w):
                return w
        try:
            r = qt.BoundingRectangle
            px, py = r.right - 59, r.top + 57
            auto.Click(px, py)
            self.log.info(f"4.1.x：点击弹出独立窗口图标 ({px},{py})，等待独立搜索窗…")
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"弹出独立窗口图标点击失败：{exc}")
        for _ in range(16):
            time.sleep(0.5)
            w = self._find_search_window(1)
            if w is not None and self._has_search_doc(w):
                return w
        return None

    def _has_search_doc(self, w) -> bool:
        """窗口内是否已出现「搜一搜」文档（UIA 可读判据）。"""
        try:
            kw = self.sel.search_doc_keyword
            return w.DocumentControl(RegexName=f".*{re.escape(kw)}.*").Exists(1)
        except Exception:  # noqa: BLE001
            return False

    def _find_main_window(self):
        """返回搜索窗口（Chrome_WidgetWin_0）；不存在时自动经侧栏打开。"""
        if self._win is not None and self._win.Exists(0.5):
            return self._win
        w = self._find_search_window(2)
        if w is not None:
            self._win = w
            return w
        return self._open_search_window()

    def _ensure_search_ready(self, keyword=None) -> None:
        """确保搜索窗口存在且已打开「搜一搜」标签页。
    
        优化：若搜一搜已在最前且有关键词匹配的文挡 → 直接返回，无需重新打开主窗/侧栏。
        适用于：用户手动切到搜一搜、或搜一搜独立窗口模式等场景。
        """
        kw = self.sel.search_doc_keyword
        # 1. 快速检查：搜一搜已在最前？能读到文挡 → 直接用
        try:
            w = self._find_search_window(1)
            if w is not None:
                doc = w.DocumentControl(RegexName=f".*{re.escape(kw)}.*")
                if doc.Exists(1):
                    self._win = w
                    self.log.info(f"搜一搜已就绪（前台窗），直接使用。")
                    return
        except Exception:  # noqa: BLE001
            pass
        # 2. 正常流程：查主窗→搜一搜标签页
        w = self._find_search_window(2)
        if w is not None:
            self._win = w
            doc = w.DocumentControl(RegexName=f".*{re.escape(kw)}.*")
            if doc.Exists(2):
                return
            # 4.1.x：搜一搜结果恒在最左标签 (tab#0)。活动标签可能是残留文章页或
            # 误开的其它页，先点回最左标签把结果列表切回前台。
            self.log.info("搜索窗口当前标签非「搜一搜」，尝试切回最左结果标签…")
            if self._activate_results_tab() is not None:
                return
            # 仍不行：关掉残留标签逐个回退，直到露出搜一搜
            self.log.info("尝试关闭残留标签恢复搜一搜…")
            try:
                w.SetActive()
                time.sleep(0.2)
                for _ in range(3):
                    if w.DocumentControl(RegexName=f".*{re.escape(kw)}.*").Exists(0.5):
                        return
                    auto.SendKeys("{Ctrl}w")
                    time.sleep(0.8)
            except Exception:  # noqa: BLE001
                pass
        # 搜索窗口不存在，或无法恢复出搜一搜标签 → 经侧栏重新打开
        self._win = None
        self._open_search_window(keyword)

    def _find_search_doc(self, timeout: float = 8):
        w = self._find_main_window()
        kw = self.sel.search_doc_keyword
        doc = w.DocumentControl(RegexName=f".*{re.escape(kw)}.*")
        if doc.Exists(timeout):
            return doc
        # 4.1.x 搜一搜窗是多标签浏览器，结果列表恒在最左标签(tab#0)。当前活动标签
        # 若被文章/筛选误点/广告页顶掉，按活动标签就读不到搜一搜文档——点回最左
        # 标签把结果列表切回前台再取（b1 已验证的可靠复位）。
        doc = self._activate_results_tab()
        if doc is not None:
            return doc
        raise DriverError(
            f"未找到「{kw}」标签页。请先在微信里打开『搜一搜』（顶部搜索框 → 搜一搜），再运行。"
        )

    def _activate_results_tab(self):
        """点击最左侧标签把搜一搜结果列表切回前台并返回其文档；失败返回 None。

        4.1.x 搜一搜窗为多标签 Chromium：结果列表恒为最左标签(tab#0)，点开
        公众号文章会在其右侧新开标签，误点/筛选也可能把活动标签切走。按 left
        坐标定位所有 Tab（最左优先）逐个点击，命中「搜一搜」文档即返回。
        """
        try:
            w = self._find_main_window()
        except Exception:  # noqa: BLE001
            return None
        kw = self.sel.search_doc_keyword
        tabs = []
        cnt = [0]

        def walk(node, depth=0):
            if depth > 18 or cnt[0] > 3000:
                return
            try:
                kids = node.GetChildren()
            except Exception:  # noqa: BLE001
                return
            for c in kids:
                cnt[0] += 1
                try:
                    if c.ControlTypeName == "PaneControl" and (c.ClassName or "") == "Tab":
                        tabs.append((c.BoundingRectangle.left, c))
                except Exception:  # noqa: BLE001
                    pass
                walk(c, depth + 1)

        walk(w)
        tabs.sort(key=lambda t: t[0])
        for _, tab in tabs:
            try:
                tab.Click(simulateMove=False)
            except Exception:  # noqa: BLE001
                continue
            time.sleep(self.delays.action_pause_sec)
            d = w.DocumentControl(RegexName=f".*{re.escape(kw)}.*")
            if d.Exists(1.5):
                return d
        return None

    @staticmethod
    def _find_named(node, target, types):
        for x in node.GetChildren():
            if x.ControlTypeName in types and (x.Name or "").strip() == target:
                return x
            r = WeChatSearchDriver._find_named(x, target, types)
            if r is not None:
                return r
        return None

    # ---------------- 对外 API ----------------
    def open_search(self, keyword: str):
        """确保搜一搜就绪，输入关键词并回车，返回搜索结果文档。

        自动处理以下异常场景：
        1. 找不到"搜一搜"按钮 → 等待窗口 + 状态复位后重试
        2. 结果列表被文章页顶掉 → 切最左标签复位
        3. 页面残留旧关键词 → 清输入框重新搜索
        """
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                self._ensure_search_ready(keyword)
                w = self._find_main_window()
                w.SetActive()
                time.sleep(self.delays.action_pause_sec)

                doc = self._find_search_doc()
                edit = doc.EditControl()
                if not edit.Exists(3):
                    raise DriverError("未找到搜一搜页内的搜索输入框。")
                edit.Click(simulateMove=False)
                time.sleep(0.3)
                # 优先 ValuePattern 直接设值：该输入框对 Ctrl+A 全选不生效，
                # 键盘清空会失败、新词追加到旧词后导致搜索跑偏（实测「月嫂评选征集」）。
                set_ok = False
                try:
                    vp = edit.GetValuePattern()
                    if vp is not None:
                        vp.SetValue(keyword)
                        set_ok = True
                except Exception:  # noqa: BLE001
                    set_ok = False
                if not set_ok:
                    edit.SendKeys("{Ctrl}a", waitTime=0.1)
                    edit.SendKeys("{Delete}", waitTime=0.1)
                    pyperclip.copy(keyword)
                    edit.SendKeys("{Ctrl}v", waitTime=0.2)
                time.sleep(0.4)
                edit.SendKeys("{Enter}", waitTime=0.2)
                self.log.info(f"已提交搜索：{keyword}")
                # 轮询等结果页文档改名为当前关键词（比固定 sleep 更快更稳）
                deadline = time.time() + max(4.0, self.delays.input_settle_sec + 2.0)
                while time.time() < deadline:
                    try:
                        d = self._find_search_doc(1)
                        if d is not None and keyword in (d.Name or ""):
                            return d
                    except Exception:  # noqa: BLE001
                        # 结果页改名瞬间 DOM 刷新，Name 读取可能撞瞬时 COM 错误
                        pass
                    time.sleep(0.4)
                return self._find_search_doc()
            except DriverError as exc:
                if attempt < max_attempts - 1:
                    self.log.warning(f"打开搜一搜失败（{exc}），重试第 {attempt + 2}/2...")
                    time.sleep(3)
                    continue
                raise

    def apply_filters(self, doc) -> None:
        """点开「全部」筛选面板，依次设置 排序/类型/时间/范围。

        默认目标：最新 / 文章 / 最近七天 / 范围(不设)（可在 config 的
        selectors 中调整）。面板为一个合并下拉：点顶部任一筛选标签即展开，
        含「排序/类型/时间/范围」四行，选项位于各自行标签右侧同一 Y 上。
        逐档设置，选项为空或找不到则跳过。
        """
        rows = list(self.sel.filter_row_labels)  # [排序，类型，时间，范围]
        if len(rows) < 4:
            rows = ["排序", "类型", "时间", "范围"]
        plan = [
            (rows[0], self.sel.filter_sort),
            (rows[1], self.sel.filter_type),
            (rows[2], self.sel.filter_time),
            (rows[3], self.sel.filter_scope),
        ]
        # 4.1.12：筛选面板内自带「类型」行（不限/文章/视频），不再预点顶部分类
        # 标签——预点会在「文章/全部」视图间来回切换，徒增扰动与耗时。
        # 面包屑「已选择」只在面板收起后才在 UIA 树中，故验证必须放在收起后：
        # 流程 = 预检面包屑 → 展开集中点击缺失项 → 收起回读 → 仍缺补点一轮。
        self.log.info("开始应用筛选...")
        plan = [(r, o) for r, o in plan if o]
        confirmed = [o for _, o in plan if o in self._breadcrumb_selected()]
        todo = [(r, o) for r, o in plan if o not in confirmed]
        if todo and self._open_filter_panel():
            for row_label, option in todo:
                if self._select_filter(row_label, option):
                    self.log.info(f"已点击筛选：{row_label} → {option}")
                else:
                    self.log.warning(f"筛选选项未找到：{row_label} → {option}")
            self._collapse_filter_panel()
        elif todo:
            self.log.warning("筛选面板未能展开，跳过筛选点击。")
        confirmed = [o for _, o in plan if o in self._breadcrumb_selected()]
        missing = [(r, o) for r, o in plan if o not in confirmed]
        if missing and self._open_filter_panel():
            for row_label, option in missing:
                if self._select_filter(row_label, option, click_parent=True):
                    self.log.info(f"补点筛选（父容器）：{row_label} → {option}")
            self._collapse_filter_panel()
            confirmed = [o for _, o in plan if o in self._breadcrumb_selected()]
        for row_label, option in plan:
            if option in confirmed:
                self.log.info(f"筛选已生效：{row_label} → {option}")
            else:
                self.log.warning(f"筛选未生效：{row_label} → {option}")
        # 关键修正：配置的筛选项(如 最新/文章/最近一天)只要有一项未确认生效，
        # 绝不退化为采集「全部」的混合结果（视频/无关内容）——抛可重试异常，
        # 由 _open_and_filter 重开搜索重试；重试耗尽则本轮跳过该关键词、下轮
        # 再来，用「宁缺毋滥」保证入库线索都是已正确筛选的。
        missing = [o for _, o in plan if o not in confirmed]
        if plan and missing:
            raise FilterNotApplied("、".join(missing))
        # 等待列表稳定
        self._wait_results_settle()
        # 记录「时间」档是否确认生效，供 iter_articles 安全阀防止无上限翻历史文章
        self._time_filter_confirmed = (
            (self.sel.filter_time in confirmed) if self.sel.filter_time else None
        )

    def _collapse_filter_panel(self) -> None:
        """点击「收起」收拢筛选面板；找不到则回退 Esc。

        展开的面板会浮盖在结果列表上方，遮挡前若干条结果的标题按钮，
        导致点击失败，故设置完筛选必须收起。
        """
        for name in ("收起", "收起 "):
            texts = self._filter_texts(self._find_search_doc(3))
            hit = next((t for t in texts if t[0].strip() == name.strip()), None)
            if hit is not None:
                try:
                    hit[3].Click(simulateMove=False)
                    time.sleep(self.delays.action_pause_sec + 0.6)
                    self.log.info("筛选面板已收起。")
                    return
                except Exception:  # noqa: BLE001
                    break
        # 4.1.12：「收起」文本常点不到，用 ≡ 图标二次点击切换收拢并校验
        if self._click_filter_menu_icon():
            first_row = (self.sel.filter_row_labels or ["排序"])[0]
            for _ in range(5):
                time.sleep(0.4)
                texts = self._filter_texts(self._find_search_doc(2))
                if not any(t[0].startswith(first_row) for t in texts):
                    self.log.info("筛选面板已收拢（≡ 切换）。")
                    return
        try:
            auto.SendKeys("{Esc}")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(self.delays.action_pause_sec + 0.6)

    def _wait_results_settle(self, max_wait: float = 7.0, interval: float = 0.6) -> None:
        """等待结果列表按「最新」重排稳定后再采集。

        应用筛选后列表会异步重排/刷新，若立即读取可能抓到上一次搜索
        残留的旧首屏（它在刷新前也是静态的，会骗过单次比对），导致漏采
        当天最新文章。先给重排一个基础等待，再轮询首屏标题，连续三次
        一致才认为已沉降。
        """
        # 先给「最新」重排与结果刷新一个基础时间，避免读到上次搜索的旧列表
        time.sleep(max(1.2, self.delays.input_settle_sec))
        prev = None
        stable = 0
        waited = 0.0
        while waited < max_wait:
            try:
                titles = self._collect_result_titles(self._find_search_doc(3))
            except Exception:  # noqa: BLE001
                titles = []
            head = tuple(titles[:5])
            if head and head == prev:
                stable += 1
                if stable >= 2:  # 连续三次读到相同首屏
                    self.log.info("结果列表已稳定，开始采集。")
                    return
            else:
                stable = 0
            prev = head
            time.sleep(interval)
            waited += interval
        self.log.info("结果列表沉降等待超时，按当前顺序采集。")

    def _filter_texts(self, doc):
        """收集文档内所有非空 TextControl → (name, left, top, control)。

        DOM 刷新期（搜索刚提交/列表重排中）遍历会撞瞬时 COM 错误
        （如 -2147220991「事件无法调用任何订阅者」），整树重走最多 4 次；
        仍失败返回空集而非抛出，让上层流程（面板展开/收起重试）自行兜底。
        """
        out = []

        def walk(node, depth=0):
            if depth > 32:
                return
            try:
                kids = node.GetChildren()
            except Exception:  # noqa: BLE001
                return
            for x in kids:
                if x.ControlTypeName == "TextControl":
                    nm = (x.Name or "").strip()
                    if nm:
                        try:
                            r = x.BoundingRectangle
                            out.append((nm, r.left, r.top, x))
                        except Exception:  # noqa: BLE001
                            pass
                walk(x, depth + 1)

        last_exc = None
        for _ in range(4):
            out.clear()
            try:
                walk(doc)
                return out
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(1.0)
        self.log.warning(f"UIA 树遍历多次失败（{last_exc}），按空结果继续。")
        return []

    def _open_filter_panel(self) -> bool:
        """确保筛选面板展开（出现「排序」行）。返回是否成功。

        兼容三种状态：
        1) 面板已展开（「排序」行可见）→ 直接可用；
        2) 面板收起但有「已选择:…」面包屑 → 点面包屑标签展开；
        3) 停在聚合/其它分类页 → 先激活「全部」分类标签再展开。
        """
        rows = self.sel.filter_row_labels or ["排序"]
        first_row = rows[0]

        def rows_visible():
            return any(t[0] == first_row for t in self._filter_texts(self._find_search_doc(3)))

        if rows_visible():
            return True
        # 状态2：点面包屑展开
        if self._click_breadcrumb_to_expand():
            if rows_visible():
                return True
        # 状态3：激活「全部」分类标签，再尝试展开
        if self._activate_all_tab():
            time.sleep(self.delays.action_pause_sec + 0.6)
            if rows_visible():
                return True
            if self._click_breadcrumb_to_expand():
                return rows_visible()
        # 4.1.12 独立窗：排序/时间面板藏在「全部」右侧 ≡ 图标里，点开再复核
        if self._click_filter_menu_icon():
            time.sleep(self.delays.action_pause_sec + 0.6)
            if rows_visible():
                return True
        return False

    def _click_category_tab(self, name: str) -> bool:
        """点击顶部分类标签行里名为 name 的标签（如「文章」）；找不到返回 False。"""
        if not name:
            return False
        texts = self._filter_texts(self._find_search_doc(3))
        cands = [t for t in texts if t[0] == name]
        if not cands:
            return False
        top = min(cands, key=lambda t: t[2])  # 标签行恒为同名文本中最靠上者
        try:
            top[3].Click(simulateMove=False)
            time.sleep(self.delays.action_pause_sec + 0.6)
            self.log.info(f"已点击分类标签：{name}")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _click_filter_menu_icon(self) -> bool:
        """点击「全部」分类标签右侧的 ≡ 筛选菜单图标（4.1.12 独立窗形态）。

        ≡ 无文本，几何定位：取「全部」与右侧相邻分类标签（文章/视频…）
        之间的中点，比固定偏移更抗布局差异。
        """
        texts = self._filter_texts(self._find_search_doc(3))
        cands = [t for t in texts if t[0] == "全部"]
        if not cands:
            return False
        tgt = min(cands, key=lambda t: t[2])
        try:
            r = tgt[3].BoundingRectangle
        except Exception:  # noqa: BLE001
            return False
        cats = ("文章", "视频", "账号", "划线", "直播", "表情", "听一听",
                "新闻", "贴图", "问一问", "朋友圈", "百科")
        nxt = [t for t in texts if t[0] in cats and abs(t[2] - tgt[2]) <= 6 and t[1] > r.right]
        x = (r.right + min(nxt, key=lambda t: t[1])[1]) // 2 if nxt else r.right + 12
        y = tgt[2] + 2
        try:
            auto.Click(x, y)
            self.log.info(f"已点击 ≡ 筛选菜单图标 ({x},{y})。")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _click_breadcrumb_to_expand(self) -> bool:
        """以「已选择:」或最靠上的「清空」为锚点，点击面包屑首个筛选标签以展开面板。"""
        texts = self._filter_texts(self._find_search_doc(3))
        anchor = next((t for t in texts if t[0].startswith("已选择")), None)
        if anchor is None:
            clears = sorted([t for t in texts if t[0] == "清空"], key=lambda t: t[2])
            anchor = clears[0] if clears else None
        if anchor is None:
            return False
        bar_y = anchor[2]
        labels = [
            t for t in texts
            if abs(t[2] - bar_y) <= 4
            and t[0] not in ("、", "清空") and not t[0].startswith("已选择")
        ]
        labels.sort(key=lambda t: t[1])
        if not labels:
            return False
        try:
            labels[0][3].Click(simulateMove=False)
        except Exception:  # noqa: BLE001
            return False
        time.sleep(self.delays.action_pause_sec + 0.6)
        return True

    def _activate_all_tab(self) -> bool:
        """点击顶部分类标签行里的「全部」（AI搜索之后的第一个 unit__item）。"""
        w = self._find_main_window()
        items = []

        def walk(node, depth=0):
            if depth > 34:
                return
            try:
                kids = node.GetChildren()
            except Exception:  # noqa: BLE001
                return
            for c in kids:
                try:
                    if c.ControlTypeName == "ListItemControl" and "unit__item" in (c.ClassName or ""):
                        r = c.BoundingRectangle
                        items.append((r.top, r.left, c))
                except Exception:  # noqa: BLE001
                    pass
                walk(c, depth + 1)

        walk(w)
        if not items:
            return False
        top_y = min(i[0] for i in items)
        row = sorted([i for i in items if abs(i[0] - top_y) <= 12], key=lambda i: i[1])
        for _, _, c in row:
            if "ai_first" in (c.ClassName or ""):
                continue  # 跳过「AI搜索」
            try:
                c.Click(simulateMove=False)
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    def _breadcrumb_selected(self) -> set:
        """读取顶部「已选择: …」面包屑里列出的已生效筛选项集合（展开/收起均可读）。

        面包屑只罗列非默认的选择（如「最新、文章、最近七天」），默认档
        （综合排序 / 不限）不会出现。故它是判断某档到底“真的选上了吗”的
        唯一可靠信号——筛选选项本身是裸 TextControl，UIA 读不出选中态。
        """
        texts = self._filter_texts(self._find_search_doc(3))
        anchor = next((t for t in texts if t[0].startswith("已选择")), None)
        if anchor is None:
            return set()
        bar_y = anchor[2]
        return {
            t[0] for t in texts
            if abs(t[2] - bar_y) <= 4
            and not t[0].startswith("已选择") and t[0] not in ("、", "清空")
        }

    def _select_filter_verified(self, row_label: str, option: str, tries: int = 4) -> bool:
        """点击某档筛选并用「已选择」面包屑回读校验，未生效则重试。

        实测：4.1.x 筛选选项点击存在偶发不落实（排序/类型两行尤甚，疑似选中
        后结果异步重排、使紧接着的下一次点击落到重排中的目标上），且点击
        回报“成功”并不代表真的选上。故每点一次就回读面包屑确认，缺目标则
        重试：第 2 次起改点选项的父级 pill（GroupControl，命中面积更大更稳），
        仍不中则如实返回 False（由上层记 WARNING、按未筛选处理，绝不谎报成功）。
        """
        if option in self._breadcrumb_selected():
            return True
        for attempt in range(1, tries + 1):
            clicked = self._select_filter(row_label, option, click_parent=(attempt >= 2))
            if clicked:
                # 面包屑回读改为轮询：选项生效后列表异步重排，立即单次回读
                # 常漏判（实测「最新/文章」已选上却被记为未生效）。
                for _ in range(6):
                    time.sleep(0.5)
                    if option in self._breadcrumb_selected():
                        return True
            time.sleep(0.4)
        return option in self._breadcrumb_selected()

    def _select_filter(self, row_label: str, option: str, click_parent: bool = False) -> bool:
        """在筛选面板中选中某一行的某个选项（点一次，不校验；校验见 _select_filter_verified）。

        选项判定：名称精确匹配 option，且与行标签同一 Y（±3），且在行标签
        右侧（x 更大）。click_parent=True 时改点选项文本的父级 pill 容器。

        为什么容差必须收紧到 ±3（实测踩坑）：展开面板顶部有一条「已选择」
        面包屑汇总行，里面挤着与真选项同名的 token（如「最新/文章/最近一天」），
        其 Y 仅比「排序」行标签高 6px。旧实现用 ±6 且「取最左候选」，会把
        「排序」的真选项（如 x≈693 的「最新」）让位给面包屑里 x≈488 的同名 token，
        点了个空——排序永远停在「综合排序」，首击点偏后类型/时间也连锁错位。
        真选项与行标签的 Y 完全对齐（实测 Δ≤1），面包屑恒在 Δ6，故 ±3 能
        干净隔离面包屑且不误伤任何真选项。选项为裸 TextControl（ClassName 空、
        无 SelectionItem/Toggle 状态），UIA 读不出选中态，只能靠这个几何锚定。
        """
        if not self._open_filter_panel():
            return False
        texts = self._filter_texts(self._find_search_doc(3))
        row_hits = sorted(
            [t for t in texts if t[0] == row_label or t[0].startswith(row_label)],
            key=lambda t: t[1],
        )
        if not row_hits:
            return False
        row_x, row_y = row_hits[0][1], row_hits[0][2]
        cands = [
            t for t in texts
            if t[0] == option and t[0] != row_label
            and abs(t[2] - row_y) <= 3 and t[1] > row_x
        ]
        if not cands:
            return False
        cands.sort(key=lambda t: t[1])
        target = cands[0][3]
        try:
            if click_parent:
                try:
                    target = target.GetParentControl() or target
                except Exception:  # noqa: BLE001
                    pass
            target.Click(simulateMove=False)
        except Exception:  # noqa: BLE001
            return False
        time.sleep(self.delays.action_pause_sec + 0.8)
        return True

    def iter_articles(self, doc, keyword: str):
        """滚动遍历结果，逐条点开文章读正文+复制链接，产出 Article。

        每次只处理当前可视快照中最靠上的未处理标题（其按钮此刻在 DOM 中、
        可点），处理完再重新取快照，避免因虚拟列表回收而引用到已失效的
        标题按钮；当前可视项全部处理完才下拉。持续下拉直到列表触底（连续
        stop_after_no_new_rounds 次下拉都无新标题）。
        max_items 单关键词硬上限（<=0 不限）；max_scrolls 滚动安全上限（<=0 不限）。
        """
        max_items = self.cfg.collect.max_items_per_keyword
        max_scrolls = self.cfg.collect.max_scrolls
        no_new_limit = self.cfg.collect.stop_after_no_new_rounds

        # 安全阀：若配置要求了「时间」筛选却未确认生效（apply_filters 回读面包屑判定），
        # 说明结果是未按时间约束的综合结果，可能有成百上千条历史文章——此时不做
        # 大/无上限采集，收紧到安全条数，避免像综合结果那样失控狂翻。
        if getattr(self, "_time_filter_confirmed", None) is False:
            safe_cap = 30
            max_items = safe_cap if max_items <= 0 else min(max_items, safe_cap)
            self.log.warning(f"时间筛选未确认生效，为防失控本轮最多采 {max_items} 条。")

        seen = set()
        produced = 0
        no_new = 0
        scrolls = 0
        errors = 0

        while True:
            try:
                titles = self._collect_result_titles(self._find_search_doc(3))
            except Exception as exc:  # noqa: BLE001
                errors += 1
                if errors > 5:
                    self.log.error(f"连续读取结果列表失败，放弃该关键词：{exc}")
                    return
                self.log.warning(f"读取结果列表出错（第 {errors} 次），尝试恢复搜一搜后重试：{exc}")
                try:
                    self._ensure_search_ready()  # 关掉可能残留的文章标签，让搜一搜回到前台
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(2.0)
                continue
            errors = 0
            next_title = next((t for t in titles if t not in seen), None)

            if next_title is not None:
                no_new = 0
                seen.add(next_title)
                try:
                    article = self._open_and_read(next_title, keyword)
                except Exception as exc:  # noqa: BLE001
                    # 单篇出现瞬时 UIA/COM 错误，跳过该篇继续，不中断整个关键词
                    self.log.warning(f"处理文章出错，跳过：{next_title[:24]}（{exc}）")
                    self._close_article_tab()  # 尽力回到搜一搜页
                    time.sleep(1.0)
                    article = None
                if article is not None:
                    produced += 1
                    yield article
                    if max_items > 0 and produced >= max_items:
                        self.log.info(f"已达单关键词上限 {max_items} 条，停止。")
                        return
                continue  # 处理完一条立即重取快照，取下一条最靠上的未处理标题

            # 当前可视标题已全部处理 → 下拉加载更多
            no_new += 1
            if no_new >= no_new_limit:
                self.log.info(f"连续 {no_new_limit} 次下拉无新结果，已到结果底部，停止。")
                return
            self._scroll_results()
            time.sleep(self.cfg.collect.scroll_pause_sec)
            scrolls += 1
            if max_scrolls > 0 and scrolls >= max_scrolls:
                self.log.info(f"达到滚动次数安全上限 {max_scrolls}，停止。")
                return

    # ---------------- 结果解析 ----------------
    def _collect_result_titles(self, doc):
        """抽取真实文章标题，并按视觉自上而下（最新在前）排序。

        每条文章标题按钮的 ClassName 均含 `search_list_item`（位于
        search_row/search_result 卡片内）。而结果列表中内嵌的「大家
        都在搜」推荐词块，其词条按钮不带该类名——若误当标题点击
        会触发一次新搜索、导致结果整列跑偏离题。故按 ClassName 含
        `search_list_item` 正向识别文章标题，最可靠且抗缩放，同时不会因
        长度阈值而漏掉短标题。图片搜索入口含「上传」也排除；非公众号
        文章由后续 URL 前缀校验剔除。
        兜底：若类名体系变化导致正向识别全空，回退「按钮名>=10字」旧启发式，避免漏采。
        """
        found = {}   # name -> (top, left)：命中 search_list_item 的文章标题
        raw = {}     # 兜底：>=10 字的长按钮
        noise = ("上传", "拖拽")

        def box_of(x):
            try:
                r = x.BoundingRectangle
                return (r.top, r.left)
            except Exception:  # noqa: BLE001
                return (10 ** 9, 0)

        def walk(node, depth=0):
            if depth > 30:
                return
            try:
                kids = node.GetChildren()
            except Exception:  # noqa: BLE001
                return
            for x in kids:
                if x.ControlTypeName == "ButtonControl":
                    nm = (x.Name or "").strip()
                    cls = x.ClassName or ""
                    tokens = set(cls.split())
                    # 文章标题正向识别：4.0 用 search_list_item；4.1.x 改为 class 含
                    # `article` 词（如 `expose_log_elem article active__mask search_...`）。
                    # 「相关搜索/大家都在搜」推荐项 class 含 related（related__item），需排除，
                    # 否则误当标题点击会触发新搜索、导致结果整列跑偏。
                    is_title = ("search_list_item" in cls) or ("article" in tokens)
                    is_reco = "related" in cls
                    if nm and not any(k in nm for k in noise):
                        if is_title and not is_reco and nm not in found:
                            found[nm] = box_of(x)
                        elif len(nm) >= 10 and not is_reco and nm not in raw:
                            raw[nm] = box_of(x)
                walk(x, depth + 1)

        walk(doc)
        picked = found if found else raw
        if not found and raw:
            self.log.warning("未按 search_list_item 命中文章标题，回退长按钮候选。")
        ordered = sorted(picked.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        return [name for name, _ in ordered]

    def _scroll_results(self) -> None:
        try:
            doc = self._find_search_doc(2)
            doc.WheelDown(wheelTimes=10, waitTime=0.05)
        except Exception:  # noqa: BLE001
            try:
                self._find_search_doc(2).SendKeys("{PageDown}")
            except Exception:  # noqa: BLE001
                pass

    def _ensure_visible(self, btn) -> bool:
        """将结果标题按钮滚动到搜索窗口可视区内，避免点击落在窗口外而无效。

        搜一搜结果为虚拟列表：首屏之后的标题按钮虽在 DOM 中，但其屏幕
        坐标在窗口可视范围之外，直接 Click 会点空——文章标签根本不会打开。
        先尝试 ScrollIntoView，再按窗口上下边界用滚轮微调到可视区。
        """
        try:
            sp = btn.GetScrollItemPattern()
            if sp is not None:
                sp.ScrollIntoView()
                time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        try:
            w = self._find_main_window()
            wr = w.BoundingRectangle
            doc = self._find_search_doc(2)
            for _ in range(8):
                r = btn.BoundingRectangle
                if r.top == 0 and r.bottom == 0:
                    return False  # 不在 DOM/无法定位
                cy = (r.top + r.bottom) // 2
                if wr.top + 80 <= cy <= wr.bottom - 80:
                    return True
                if cy > wr.bottom - 80:
                    doc.WheelDown(wheelTimes=3, waitTime=0.05)
                else:
                    doc.WheelUp(wheelTimes=3, waitTime=0.05)
                time.sleep(0.25)
            r = btn.BoundingRectangle
            cy = (r.top + r.bottom) // 2
            return wr.top <= cy <= wr.bottom
        except Exception:  # noqa: BLE001
            return True

    def _activate_title(self, btn) -> bool:
        """激活结果标题打开文章，优先不移动鼠标，避免点到窗口外（如任务栏）。

        1) 先用 UIA Invoke / LegacyIAccessible 默认动作——不移动物理鼠标，
           也不受 Chromium 平滑滚动动画影响，从根本上杜绝「点到任务栏」；
        2) 都不支持时才回退坐标点击，且仅当按钮中心稳定落在搜索窗口可视区
           内（离上下边界≥40px）才点，否则宁可放弃本次点击也不点到窗外。
        """
        self._ensure_visible(btn)
        # 优先：无鼠标激活
        for getter, action in (
            ("GetInvokePattern", "Invoke"),
            ("GetLegacyIAccessiblePattern", "DoDefaultAction"),
        ):
            try:
                pat = getattr(btn, getter)()
                if pat is not None:
                    getattr(pat, action)()
                    return True
            except Exception:  # noqa: BLE001
                pass
        # 回退：坐标点击，但必须确认落点在窗口内、离底边足够远，绝不点到任务栏
        try:
            w = self._find_main_window()
            wr = w.BoundingRectangle
            r1 = btn.BoundingRectangle
            time.sleep(0.2)
            r2 = btn.BoundingRectangle  # 两次一致 → 滚动已停，避免动画中点偏
            if (r1.top, r1.bottom) != (r2.top, r2.bottom):
                return False
            cy = (r2.top + r2.bottom) // 2
            cx = (r2.left + r2.right) // 2
            if not (wr.top + 40 <= cy <= wr.bottom - 40 and wr.left <= cx <= wr.right):
                self.log.info(f"标题不在可视区内（y={cy}），本次不点击以免点到窗外。")
                return False
            btn.Click(simulateMove=False)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------------- 单篇：打开 → 读正文 → 取链接 → 关标签 ----------------
    def _open_and_read(self, title: str, keyword: str):
        doc = self._find_search_doc(3)
        btn = doc.ButtonControl(Name=title)
        if not btn.Exists(3):
            self.log.warning(f"结果标题按钮已失效，跳过：{title[:30]}")
            return None

        # 点击标题并轮询等文章标签真正打开（含慢加载）；未开则重试一次点击
        art = None
        for attempt in range(2):
            if not self._activate_title(btn):  # 无鼠标激活优先，回退坐标点击带窗口护栏
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                self.log.warning(f"标题无法激活，跳过：{title[:30]}")
                return None
            art = self._wait_article_doc(title, timeout=10.0)
            if art is not None:
                break
            # 点击可能落空（列表正在重排/滚动），稍候重新定位按钮重试
            if attempt == 0:
                self.log.info(f"文章未打开，重试点击：{title[:24]}")
                time.sleep(1.2)
                try:
                    doc = self._find_search_doc(3)
                    btn = doc.ButtonControl(Name=title)
                    if not btn.Exists(2):
                        break
                except Exception:  # noqa: BLE001
                    break

        if art is None:
            # 未检测到文章标签打开：可能真没打开，也可能打开了却没识别到。
            # 调用带守卫的 _close_article_tab——搜一搜仍在前台则它直接返回、
            # 不发关闭键；若其实有残留文章标签则关掉它，避免残留标签顶掉搜一搜
            # 导致后续采集全部找不到搜一搜而失败。
            self.log.warning(f"文章未打开，跳过：{title[:30]}")
            self._close_article_tab()
            return None

        parsed = self._parse_article_doc(art)
        # 4.1.x：文章标签的真实 mp URL 直接暴露在其 DocumentControl 的 ValuePattern 上，
        # 优先直接读取；读不到再回退 ··· 菜单「复制链接」（兼容 4.0）。
        # 再规范化去掉会话态参数（search_click_id/key/pass_ticket/exportkey 等），
        # 仅保留 __biz/mid/idx/sn 作为稳定指纹，否则同一文章跨次采集 URL 不同会去重失效。
        url = self._normalize_mp_url(self._read_doc_url(art) or self._copy_link())
        # 文章标签已确认打开，无论解析成败都关闭它，确保回到搜一搜结果页
        self._close_article_tab()
        time.sleep(self.delays.action_pause_sec)

        if parsed is None:
            self.log.warning(f"正文解析为空，跳过：{title[:30]}")
            return None

        account, publish_time, content = parsed
        prefix = self.sel.article_url_prefix
        if url and not url.startswith(prefix):
            self.log.info(f"非公众号文章（{url[:36]}…），跳过：{title[:30]}")
            return None

        return Article(
            keyword=keyword,
            title=title,
            account=account,
            publish_time=publish_time,
            summary=content[:200],
            content=content,
            url=url,
            source="wechat_pc",
        )

    def _find_article_doc(self, title: str):
        """在搜索窗口内定位已打开的文章标签页文档（非搜一搜页）。"""
        w = self._find_main_window()
        kw = self.sel.search_doc_keyword
        art = w.DocumentControl(Name=title)
        if art.Exists(0.4):
            return art
        key = title[:6]
        if key:
            art = w.DocumentControl(RegexName=f".*{re.escape(key)}.*")
            if art.Exists(0.4) and kw not in (art.Name or ""):
                return art
        return None

    @staticmethod
    def _doc_text_count(art, limit: int = 3) -> int:
        """浅遍历统计文档内非空 TextControl 数（达 limit 即提前返回）。"""
        cnt = 0
        stack = [art]
        while stack and cnt < limit:
            node = stack.pop()
            try:
                for x in node.GetChildren():
                    if x.ControlTypeName == "TextControl" and (x.Name or "").strip():
                        cnt += 1
                        if cnt >= limit:
                            break
                    stack.append(x)
            except Exception:  # noqa: BLE001
                pass
        return cnt

    def _wait_article_doc(self, title: str, timeout: float = 10.0):
        """轮询等待文章标签页打开且正文已渲染出文本。

        固定等待无法适应慢加载文章，直接读取易得到空文本。此处轮询
        直到文档出现且含至少几个文本节点，才认为可读；超时则返回最后一次
        找到的文档（尽力而为）或 None。
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            art = self._find_article_doc(title)
            if art is not None:
                last = art
                if self._doc_text_count(art, 3) >= 3:
                    return art
            time.sleep(0.5)
        return last

    def _parse_article_doc(self, art):
        """解析文章文档 → (公众号, 发布时间, 正文全文)。"""
        texts = []

        def walk(node, depth=0):
            if depth > 30:
                return
            try:
                kids = node.GetChildren()
            except Exception:  # noqa: BLE001
                return
            for x in kids:
                nm = (x.Name or "").strip()
                if x.ControlTypeName == "TextControl" and nm:
                    texts.append(nm)
                walk(x, depth + 1)

        walk(art)
        if not texts:
            return None

        publish_time = ""
        account = ""
        for i, t in enumerate(texts):
            if _TIME_RE.match(t):
                publish_time = t
                if i > 0:
                    account = texts[i - 1]
                break
        content = "\n".join(texts)
        return account, publish_time, content

    @staticmethod
    def _normalize_mp_url(url: str) -> str:
        """将 mp.weixin.qq.com 文章 URL 规范化为稳定指纹形式。

        4.1.x 搜一搜读到的文章 URL 携带大量会话态参数
        （search_click_id/key/pass_ticket/exportkey/uin 等），每次点开都不同，
        直接入库会导致跨次/跨渠道去重失败。仅保留 __biz/mid/idx/sn
        四个标识参数（即公众号文章的真实身份）。非 mp 链接原样返回。
        """
        if not url or "mp.weixin.qq.com" not in url:
            return url
        try:
            from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
            parts = urlsplit(url)
            keep = [(k, v) for k, v in parse_qsl(parts.query) if k in ("__biz", "mid", "idx", "sn")]
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(keep), ""))
        except Exception:  # noqa: BLE001
            return url

    @staticmethod
    def _read_doc_url(art) -> str:
        """从文章标签的 DocumentControl 直接读取其真实 URL（ValuePattern.Value）。

        4.1.x 搜一搜点开公众号文章会新开标签，其文档 Value 即为真实地址，形如
        https://mp.weixin.qq.com/s?...&__biz=..&mid=..&idx=..&sn=..，可直接取用，
        无需依赖 ··· 菜单（4.1.x 未必存在 AppMenuButton）。
        """
        try:
            return (art.GetValuePattern().Value or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _copy_link(self) -> str:
        """点击 ···（AppMenuButton）→「复制链接」→ 返回剪贴板中的 URL。"""
        w = self._find_main_window()
        try:
            pyperclip.copy("")
        except Exception:  # noqa: BLE001
            pass

        btn = w.ButtonControl(ClassName=self.sel.app_menu_button_class)
        if not btn.Exists(3):
            self.log.warning("未找到 ··· 更多按钮（AppMenuButton），未取到链接。")
            return ""
        try:
            btn.Click(simulateMove=False)
        except Exception:  # noqa: BLE001
            return ""
        time.sleep(1.0)

        item = None
        for name in self.sel.copy_link_menu_candidates:
            c = auto.MenuItemControl(searchDepth=25, Name=name)
            if c.Exists(2):
                item = c
                break
        if item is None:
            try:
                auto.SendKeys("{Esc}")
            except Exception:  # noqa: BLE001
                pass
            self.log.warning("菜单中未找到「复制链接」，未取到 URL。")
            return ""
        try:
            item.Click(simulateMove=False)
        except Exception:  # noqa: BLE001
            return ""
        time.sleep(0.6)
        try:
            return pyperclip.paste().strip()
        except Exception:  # noqa: BLE001
            return ""

    def _close_article_tab(self) -> None:
        """关闭当前文章标签回到搜一搜；带焦点保障与关闭后校验，防标签堆积。

        背景标签不在 UIA 树中，故若搜一搜文档当前可见，说明没有文章标签
        需要关闭，此时绝不能发 Ctrl+W——否则会误关搜一搜标签，导致后续
        采集全部失败。关闭键发出后必须轮询校验搜一搜回到前台，未生效则
        重新聚焦再发，避免焦点被抢导致 Ctrl+W 落空、标签越积越多。
        """
        w = self._find_main_window()
        kw = self.sel.search_doc_keyword
        for attempt in range(3):
            try:
                w.SetFocus()
            except Exception:  # noqa: BLE001
                pass
            try:
                w.SetActive()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
            # 搜一搜已是当前标签 → 无文章标签可关，直接返回，避免误关搜一搜
            if w.DocumentControl(RegexName=f".*{re.escape(kw)}.*").Exists(0.6):
                return
            auto.SendKeys("{Ctrl}w")
            for _ in range(6):
                time.sleep(0.4)
                if w.DocumentControl(RegexName=f".*{re.escape(kw)}.*").Exists(0.5):
                    return
        self.log.warning("文章标签关闭多次未生效，请检查标签堆积。")

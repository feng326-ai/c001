# 评选业务市场情报与商机系统

> **文档状态提醒**：本 README 保留早期单机采集器的使用说明，不是当前分布式系统、微信 4.1.x、部署或新业务开发的权威文档。开工先读 `AGENTS.md`；目标系统见 `docs/总体架构与演进路线.md`，领域迁移见 `docs/领域模型与渐进迁移方案.md`，VM/UIA 现状见 `docs/坑位手册.md`。不要按本文的 SQLite/Weixin 4.0 示例覆盖现网配置。

当前系统由本地生产采集/智能加工平面、Windows 微信采集 VM、云端正式业务平面组成，通过幂等事件同步为业务员提供资源。后续按“共享情报中台 + 三个严格隔离的公司租户”渐进演进，详见上述权威文档。

## Legacy：早期单机采集器说明

根据关键词在 **微信 PC 客户端「搜一搜」** 中搜索，逐篇打开公众号文章，采集
**标题 / 公众号 / 发布时间 / 正文全文 / 真实链接**，存入 SQLite 数据库（自动去重）。

> 采集方式：PC 微信客户端 UI 自动化 ｜ 结果类型：公众号文章 ｜ 存储：SQLite
>
> 适配版本：**Weixin 4.0**（实测通过）。

## 工作原理（Weixin 4.0 架构）

Weixin 4.0 是双窗口结构，程序据此驱动：

1. **聊天主窗口** `Name=微信, ClassName=mmui::MainWindow`：左侧竖栏有「搜一搜」入口按钮
   （`ButtonControl, ClassName=mmui::XTabBarItem`）。
2. **搜索窗口** `Name=微信, ClassName=Chrome_WidgetWin_0`（Chromium 外壳）：点上面的入口
   才会打开，搜一搜与文章都以**标签页 DocumentControl** 形式存在于此窗口内。

完整流程：

```text
找聊天窗 → 点左侧「搜一搜」入口 → 打开搜索窗
 → 在搜一搜页输入关键词回车
 → 展开「全部」筛选面板，设置 排序=最新 / 类型=文章 / 时间=最近七天 → 收起面板
 → 遍历结果标题（长按钮 ≥10 字）逐条：
      点开文章 → 读正文全文（DocumentControl 下全部文本）
               → 点 ···(AppMenuButton) → 「复制链接」→ 读剪贴板得真实 URL
               → 解析 公众号/发布时间
               → 存入 SQLite
      → Ctrl+W 关闭文章标签，回到结果页
 → 滚动加载更多，直到到底或达上限
```

### 筛选面板

结果页顶部分类行里的「全部」带一个筛选图标，展开后是一个四行面板（选项位于行
标签右侧同一 Y 上）：

| 行 | 可选值 | 本项目默认 |
|----|--------|-----------|
| 排序 | 综合排序 / 最新 / 最热 | **最新** |
| 类型 | 不限 / 文章 / 视频 | **文章** |
| 时间 | 不限 / 最近一天 / 最近七天 / 最近半年 | **最近七天** |
| 范围 | 不限 / 已关注 / 最近看过 / 朋友赞过 | 不设置 |

设置完必须点「收起」：展开的面板会浮盖在结果列表上，遮挡前若干条的标题按钮，
否则这几条会因点不到而被跳过。

## 目录结构

```
ss/
├── main.py                 # 命令行入口
├── web.py                  # 本地 Web 后台入口
├── config.example.json     # 可入库的安全模板（所有危险功能默认关闭）
├── config.json             # 每台机器的运行时配置（本地生成、Git/Docker 忽略）
├── requirements.txt
├── wxsearch/
│   ├── config.py           # 配置加载与写回（缺字段自动兜底）
│   ├── logger.py           # 日志（控制台 + 文件）
│   ├── db.py               # SQLite 存储与去重
│   ├── wechat_driver.py    # 微信 UI 自动化核心（Weixin 4.0）
│   ├── collector.py        # 采集编排
│   ├── webapp.py           # Flask 后台（配置读写 + 启动采集 + 实时日志）
│   └── templates/
│       └── admin.html      # 后台页面
└── tools/
    ├── inspect_ui.py       # UI 控件树自检工具（校准选择器）
    └── query_db.py         # 查看 / 导出采集结果
```

## 运行前提

- **Windows 系统**（UI 自动化仅支持 Windows）
- 已安装 **微信 PC 客户端（Weixin 4.0）并登录**
- Python 3.8+

## 安装

```powershell
pip install -r requirements.txt
Copy-Item config.example.json config.json -ErrorAction Stop
```

`config.json` 不随 Git 或 Docker 镜像交付，已有机器升级时不得用模板覆盖现有文件。
首次安装必须先复制模板，再填写本机唯一的 `vm_instance_id`、逐机标定坐标和运行时
Redis 凭据；未替换占位值前保持 `distributed.enabled=false`、
`unattended.enabled=false`。配置文件缺失时程序会拒绝启动，不再静默采用内置身份、
关键词或端点；启用分布式无人值守后，通用 `vm-01`、占位身份、缺失/占位 Redis
端点也会被拒绝。程序写回配置时采用原子替换并在 Linux 上设置 `0600`。

Windows VM 首次下发或迁移配置后，还必须用 `icacls` 移除继承权限，只保留采集运行
账号、`Administrators` 和 `SYSTEM`；不要依赖 Git/Docker 忽略规则代替文件 ACL。

从 `git archive` 生成 VM 发布包时也不会包含 `config.json`。切换版本前必须把该 VM
现有的受保护运行时配置复制或挂载到候选目录，并只核对允许字段；禁止把真实配置
打回发布包、构建上下文或仓库。

## 使用

1. 确认已从 `config.example.json` 创建本机 `config.json`，再填写 `keywords`
   （也可用命令行覆盖）。
2. 打开并登录微信 PC 客户端（**无需手动打开搜一搜**，程序会自动经左侧入口打开）。
3. 运行：

### 方式一：命令行

```powershell
python main.py                      # 使用配置文件里的关键词
python main.py -k 评选征集 人工智能   # 命令行指定关键词
```

> ⚠️ 采集过程中程序会接管鼠标/键盘并占用剪贴板操作微信窗口，请勿手动干扰。

### 方式二：Web 后台（推荐）

用网页管理关键词、筛选条件、采集参数，并一键启动采集、实时看日志：

```powershell
python web.py                  # 默认 http://127.0.0.1:5000
python web.py -p 8080          # 指定端口
```

打开浏览器访问上面的地址即可：

- **关键词**：每行一个或逗号分隔；
- **筛选条件**：排序 / 类型 / 时间 / 范围四档下拉，置「不设置」即跳过该档；
- **采集参数**：每关键词条数、最大滚动次数、滚动间隔、无新结果停止轮数；
- 点「保存设置」写回 `config.json`；点「开始采集」在后台线程运行并轮询日志。

> 后台与命令行共用同一份 `config.json`，在后台保存后 `python main.py` 也会读到最新值。
> 采集仍会接管鼠标/键盘，启动后请勿手动干扰微信窗口。

4. 查看 / 导出结果：

```powershell
python -m tools.query_db                    # 打印最近记录（含链接、正文字数）
python -m tools.query_db --keyword 评选征集   # 按关键词查看
python -m tools.query_db --csv result.csv    # 导出 CSV（含正文与链接）
```

## 数据表结构（`articles`）

| 字段 | 说明 |
|------|------|
| keyword | 搜索关键词 |
| title | 文章标题 |
| account | 公众号名 |
| publish_time | 发布时间 |
| summary | 正文前 200 字摘要 |
| content | **正文全文** |
| url | **文章真实链接**（mp.weixin.qq.com/s/…） |
| collected_at | 采集时间 |

去重键：**优先用 `url`**；若某篇未取到链接，则回退到 `title + account` 的 MD5 指纹。

## 采集参数（`config.json` → `collect`）

- `max_scrolls` 最大滚动次数
- `max_items_per_keyword` 单关键词最多采集条数
- `scroll_pause_sec` 每次滚动后的等待（越大越稳、越慢）
- `stop_after_no_new_rounds` 连续多少轮无新增即判定到底停止
- `fetch_url` 保留开关（当前实现始终会点进文章取正文与链接）

## 选择器（`config.json` → `selectors`）

控件名称**随微信版本变化**。若报「未找到聊天主窗口 / 搜一搜入口 / 搜一搜标签页」等错误，
用自检工具校准：

```powershell
python -m tools.inspect_ui              # 导出前台窗口控件树到 ui_tree.txt
python -m tools.inspect_ui --main       # 导出微信主窗口控件树
```

关键选择器（默认值对应 Weixin 4.0）：

- `chat_window_class` — 聊天主窗口 ClassName，默认 `mmui::MainWindow`
- `search_entry_button_name` / `search_entry_button_class` — 左侧「搜一搜」入口按钮，
  默认 `搜一搜` / `mmui::XTabBarItem`
- `search_doc_keyword` — 搜一搜标签页文档名关键字，默认 `搜一搜`
- `app_menu_button_class` — 文章页右上角 ···（更多）按钮，默认 `AppMenuButton`
- `copy_link_menu_candidates` — 菜单里「复制链接」项文字
- `article_url_prefix` — 判定公众号文章的链接前缀，默认 `https://mp.weixin.qq.com`
- `filter_row_labels` — 筛选面板四个行标签，默认 `["排序", "类型", "时间", "范围"]`
- `filter_sort` / `filter_type` / `filter_time` / `filter_scope` — 四档筛选目标值，默认
  `最新` / `文章` / `最近七天` / `（不设）`（置空字符串即跳过该档）

## 局限与说明

1. **强依赖微信版本**：默认选择器面向 Weixin 4.0；控件结构变化会导致失败，请用自检工具校准。
2. **无法外部直链抓取**：公众号文章对无登录态的程序化请求会返回「环境异常」风控页，
   因此正文只能在客户端内读取（本项目即如此），不要尝试用 URL 直接抓取正文。
3. **速度**：每篇都要开/关一次文章标签并复制链接，采集会较慢；非公众号结果（如视频号）
   会因链接前缀不符被自动跳过。
4. **合规提醒**：请控制采集频率（已内置滚动间隔），仅将数据用于合法合规用途，
   遵守微信平台使用条款。

# AGENTS.md — 多智能体协作契约

> **本文件是给智能体（AI Agent）看的工作约定。** 任何智能体在本仓库开工前必须先读本文件。
> 目前有两个智能体在协作（宿主机侧 / Ubuntu 侧），本文件的存在就是为了让它们**表现得像一个人**：
> 共享同一份事实来源、不重复踩坑、不各改一份。

---

## 0. 铁律（违反必然造成分叉）

1. **唯一事实来源是本 Git 仓库。** 不允许"拷一份出去改"。所有代码改动必须落在仓库里并提交。
2. **开工先 `git pull`，收工必 `git commit` + `push`。** 不留未提交的本地改动过夜。
3. **不新建平行副本。** 严禁 `xxx_new.py`、`xxx_副本/`、`_deploy2/` 这类做法（本仓库曾因此出现 6 份驱动副本，见 §4）。
4. **部署包是构建产物，不是源码。** 推给 VM 的包必须从仓库生成，改动只回改仓库。
5. **踩到新坑先写进 `docs/坑位手册.md`**，再继续干活。别让另一个智能体重踩。
6. **凭据永不入库。** 配置里用占位符，真实口令走环境变量或本地未跟踪文件。

## 1. 仓库是什么

「资源采集系统」——服务端调度 + 多前端采集节点的分布式线索采集/研判系统。

- **服务端**（Ubuntu + docker-compose）：Redis(broker) / Celery worker / PostgreSQL / Web 看板 / LLM 清洗
- **采集节点**（多台 Windows VM）：驱动 PC 微信搜一搜、搜狗微信等出数据，经 Celery 投回服务端

节点很"轻"：只装 `celery`+`redis` 客户端，**不直连 PostgreSQL**，全部交互走 `send_task(任务名)` 按名投递。

## 2. 目录职责与归属

| 路径 | 内容 | 说明 |
|---|---|---|
| `wxsearch/` | **唯一权威源码包** | 服务端 + 采集端共用；驱动、采集、去重、任务、看板都在这 |
| `wxsearch/wechat_driver.py` | PC 微信 UIA 驱动 | **最脆弱、最常改**，改动前必读 §5 |
| `wxsearch/collectors/` | 其它渠道驱动（搜狗 Playwright 等） | |
| `wxsearch/api/`、`templates/` | 看板 Web | 服务端侧 |
| `wxsearch/tasks.py`、`task_scheduler.py` | Celery 任务、关键词租约调度 | 服务端侧 |
| `docs/` | **知识库**：坑位手册、决策记录、标定清单 | 见 §3 |
| `tools/` | 一次性脚本、探针 | 可弃 |
| `_deploy_tmp/` | 临时部署产物 | **可随时删除，不是源码** |

## 3. 三份必读知识文件

| 文件 | 什么时候读 | 什么时候写 |
|---|---|---|
| `docs/坑位手册.md` | 动 UIA/VM/部署之前 | 踩到新坑立刻写 |
| `docs/决策记录.md` | 发现"两种做法"时 | 做出技术选型时 |
| `docs/标定清单.md` | 上手一台新 VM | 标定完一台就填 |

## 4. 历史教训：分叉是怎么发生的

真实案例（2026-08）——同一个驱动出现过 **6 份副本**：

```
wxsearch/wechat_driver.py                  34.4 KB  ← 仓库里的（最旧，无 4.1.x 支持）
tools/_stage/wxsearch/wechat_driver.py     34.4 KB
tools/_deploy/wxsearch/wechat_driver.py    42.8 KB
tools/_guest_driver_now.py                 50.7 KB  ← 从 VM 拉回的快照
_deploy_tmp/wxg01/wechat_driver.py         53.9 KB  ← 智能体 A 改的
（Ubuntu 侧 VM 里实际在跑的）              60.9 KB  ← 智能体 B 改的，最新
```

后果：**两个智能体各自解决同一个问题**（"回车被微信弹窗抢焦点"两边都踩过），
而**成果只有一边有**（筛选面板适配 A 放弃了、B 做出来了，但 A 不知道）。

已于 2026-08-18 收敛：以 60.9 KB 版为准回流 `wxsearch/wechat_driver.py`。**不要再制造副本。**

## 5. 改 `wechat_driver.py` 之前必须知道的

UIA 驱动对微信版本和 VM 环境极度敏感，改之前先读 `docs/坑位手册.md`，重点：

- 微信 4.1.x 主窗是 **Qt 壳**（`Qt51514QWindowIcon`），UIA 树不透明，只能盲点坐标
- guest 内**合成鼠标点击对 Chromium 渲染层无效**，下拉框必须走键盘（`{Down}`/`{Enter}`）
- 搜索面板 4.1.12+ 默认内嵌，需点"弹出独立窗口"图标才能被 UIA 读到
- 筛选**必须验证生效**（面包屑回读），未生效要抛 `FilterNotApplied` 重试，
  **绝不能退化成采「全部」的混合结果**——那会污染线索库
- 坐标类参数一律走 `config.json`，**不写死在代码里**（每台 VM 需各自标定）

## 6. 分工边界（避免同时改同一文件）

| 领域 | 负责方 | 主要目录 |
|---|---|---|
| 服务端：调度/去重/看板/LLM 清洗/数据库 | 智能体 A（宿主机侧） | `wxsearch/tasks.py`、`task_scheduler.py`、`api/`、`templates/` |
| 采集端：UIA 驱动适配、VM 运维与标定 | 智能体 B（Ubuntu 侧） | `wxsearch/wechat_driver.py`、`collector.py`、`docs/标定清单.md` |
| 交界：`config.py` 契约、`distributed_sink.py`、`unattended.py` | **改动前必须先同步** | |

跨界改动的规矩：**先在 `docs/决策记录.md` 写一条，再动手。**

## 7. 提交约定

- 提交信息用中文，讲清"**为什么**"而不只是"改了什么"
- 一次提交只做一件事；驱动改动单独提交，便于回滚
- 涉及行为变更的，在提交信息里写明影响面（哪些 VM / 哪个渠道）

## 8. 环境速查

- 服务端：Ubuntu，`docker-compose`，容器 `wxsearch_db`(PG) / `wxsearch_redis` / `wxsearch_worker` / `wxsearch_beat` / `wxsearch_backend`
- PG 库名 `wx_search`；采集节点**不直连**，只走 Celery
- 采集 VM：KVM/libvirt（`virsh`，注意需 `sudo` 或正确的 libvirt URI），guest 网段 `192.168.122.0/24`，宿主网关 `192.168.122.1`
- 节点 Python 依赖：`uiautomation`、`pyperclip`、`celery`、`redis`
- 每台 VM 的 `unattended.vm_instance_id` **必须唯一**，否则领词租约互相打架（链接克隆最易犯）

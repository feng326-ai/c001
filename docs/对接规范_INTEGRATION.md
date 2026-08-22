# 线索中台 · 对接规范（Integration Contract）

> **用途**：本系统（线索中台，下称"主系统"）与两个并行开发的新系统对接的**唯一权威契约**。
> - **窗口A：OA 系统** —— 只读消费主系统的线索数据。
> - **窗口B：手机采集系统** —— 作为"微信搜一搜(手机)"渠道，把采集结果灌入主系统。
>
> **两个新系统开工前必读本文件。** 只需理解本契约即可对接，无需读懂主系统全部代码。

---

## 0. 总原则（三条铁律）

1. **契约先行、冻结后并行**：本文件定义的接口/数据结构一经冻结，各方照此开发；未冻结前不并行。
2. **主系统是唯一真理**：字段口径、数据结构以主系统为准，新系统**不得自造字段/自改语义**。
3. **契约版本化**：本文件顶部维护版本号；任何改动需通知三方并升版本。
   - 当前版本：**v1.0（2026-08-17）**

**仓库形态**：OA、手机采集各为**独立仓库/独立部署**，仅通过本契约（REST API + Redis 协议）依赖主系统，不共享主系统代码。

---

## 1. 关键概念：两种"渠道"（务必分清）

主系统里有两个**解耦**的"渠道"概念，别混：

| 概念 | 字段 | 作用 | 取值示例 |
|---|---|---|---|
| **调度渠道** | `channel` | 决定用哪组关键词、按什么周期领词循环 | `souyisou`(PC搜一搜) / `sogou`(搜狗) / `souyisou_mobile`(手机搜一搜,新增) |
| **来源渠道** | `source_channel` | 数据来源标识，看板显示、去重区分 | `wechat_pc` / `weixin_mobile` / `sogou_weixin` |

- **PC搜一搜** = 调度渠道 `souyisou` + 来源 `wechat_pc`
- **手机搜一搜** = 调度渠道 `souyisou_mobile` + 来源 `weixin_mobile`
- 二者**共用同一批关键词**，但**各自独立周期、各自把每个词搜一遍**（方案 B），重复结果由主系统三层去重自动合并。

---

## 2. 方向一：手机采集系统 → 灌入主系统（inbound）

### 2.1 架构（务必走此管道，禁止直连数据库）

```
手机采集App
  ├─(1) 领词      claim_task(channel="souyisou_mobile", ...)   ← 走 Redis
  ├─(2) 采集      按关键词在手机微信搜一搜抓文章
  ├─(3) 投递入库  send_task("wxsearch.tasks.process_article_task", [payload])  ← 走 Redis
  │                 → 主系统 worker 执行【三层去重 + AI评分 + 入库 + 上看板】
  ├─(4) 上报      report_result(keyword, count, success, channel="souyisou_mobile", ...)
  └─(5) 心跳      heartbeat_device(device_id, device_type="phone", channel="souyisou_mobile", ...)
```

**好处**：手机端只管"领词→采集→投递"，去重/AI清洗/看板/反馈**全部复用主系统，零额外开发**。

### 2.2 投递契约（核心）

用轻量 Celery 生产者按**任务名**投递，**不要 import 主系统代码**（避免拉入 psycopg2 等依赖）：

```python
from celery import Celery
app = Celery("mobile_producer", broker=REDIS_URL, backend=REDIS_URL_DB1)
app.send_task("wxsearch.tasks.process_article_task", args=[payload_json])
```

**payload（JSON 字符串）字段约定**：

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `title` | ✅ | str | 文章标题 |
| `content` | ✅ | str | 正文（HTML 或纯文本） |
| `url` | ✅ | str | 原文链接（手机搜一搜的 mp 真链） |
| `source_channel` | ✅ | str | **固定填 `weixin_mobile`** |
| `keyword` | ✅ | str | 本条来自哪个关键词 |
| `account` | 选 | str | 公众号名 |
| `account_id` | 选 | str | `__biz` |
| `publish_time` | 选 | str | 发布时间字符串 |
| `summary` | 选 | str | 摘要（不填主系统自动取正文前 500 字） |
| `collected_at` | 选 | str(ISO) | 抓取时刻，建议抓到即填 |

> 不要传 `created_at`（由主系统入库时自动生成）。

**返回**（`wait_result=True` 时同步返回）：`{"success": bool, "reason": str, "id": int}`。`success=false` 且 `reason` 含 `duplicate` 表示被去重，属正常。

### 2.3 调度契约（领词/上报/心跳）

复用 `DistributedTaskScheduler`（走 Redis，**不直连 PG**）。参考实现直接照抄：
- `wxsearch/unattended.py`（无人值守循环骨架，**首选模板**）
- `wxsearch/sogou_loop.py`（常驻循环 + 自重启）
- `wxsearch/distributed_sink.py`（投递封装）

关键方法签名：

| 方法 | 参数 | 用途 |
|---|---|---|
| `claim_task(channel, vm_instance_id, max_keywords)` | channel=`souyisou_mobile` | 领一批词（分布式锁防争抢） |
| `report_result(keyword, articles_count, success, error_message, device_id, channel)` | channel=`souyisou_mobile` | 上报单词结果 |
| `heartbeat_device(device_id, device_type, channel, current_keyword)` | device_type=`phone`、channel=`souyisou_mobile` | 心跳（设备监控页可见） |

- `device_id` / `vm_instance_id`：每台手机唯一（如 `phone-01`、`phone-02`），**切勿重复**（克隆机重号会引发领词冲突）。
- 端点：`REDIS_URL`（与主系统同一 Redis）。任务名固定 `wxsearch.tasks.process_article_task`。

### 2.4 主系统侧"地基"（我负责，手机端开工前完成）
- [ ] 播种新调度渠道 `souyisou_mobile`：挂与 `souyisou` 相同的关键词组，设默认循环周期（建议 20 分钟，可调）。
- [ ] 确认 `weixin_mobile` 来源渠道在看板/去重/导出中显示为"微信搜一搜(手机)"（名称与图标映射已预留，验证即可）。

---

## 3. 方向二：OA 系统 ← 读取主系统线索（outbound，只读）

### 3.1 接口

- **Base URL**：`https://leads.yixiua.com/api/v1`
- **接口契约**：主系统基于 FastAPI，自带机读契约：
  - `https://leads.yixiua.com/openapi.json`（OpenAPI 规范，可直接生成客户端）
  - `https://leads.yixiua.com/docs`（Swagger 交互文档）

**只读端点（OA 用这些）**：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/leads/` | 线索列表（分页/筛选/排序，参数见 OpenAPI） |
| GET | `/leads/export` | 按条件导出 CSV（全量，不分页） |
| GET | `/leads/{id}/sources` | 某活动的多来源（同活动跨渠道转载） |

> OA **只读**：不调用任何 PATCH/POST/DELETE。跟进状态若要回写，属于后续需求，需另立契约（当前不做）。

### 3.2 鉴权（机器对机器，地基待加）

现有登录是浏览器 session cookie，OA 是服务端调用，需专用 **API Token**：

- **约定**：请求头 `X-API-Key: <token>`；主系统校验通过即放行只读接口。
- Token 由主系统签发给 OA（一 OA 一 key，可吊销）。
- **地基待做（我负责）**：新增 API Key 校验（不动现有 cookie 登录），签发一个 OA 专用只读 key。

### 3.3 线索字段口径（`qualified_leads` 关键字段）

| 字段 | 含义 |
|---|---|
| `id` | 线索唯一 ID |
| `title` / `event_name` | 文章标题 / 提炼出的活动名称 |
| `keyword` | 采集关键词 |
| `account` / `source_channel` | 公众号 / 来源渠道 |
| `intent_category` | 意图：评选/投票/征集/活动/资讯/其他 |
| `resource_level` | 资源质量：excellent(优)/normal(普)/poor(低) |
| `activity_region` / `recurrence` / `activity_status` | 地区 / 届次 / 活动状态（LLM判定） |
| `is_online_voting` / `online_voting_url` | 是否有线上投票 / 投票链接 |
| `priority_level` / `priority_score` | 优先级 P0/P1/P2 / 分数 |
| `publish_time` / `collected_at` | 发布时间 / 采集时间 |

> 完整字段与类型以 `/openapi.json` 及实际响应为准。渠道值 → 友好名的映射由主系统提供（如 `wechat_pc`→"微信搜一搜(PC)"）。

---

## 4. 契约冻结与变更流程

- **冻结范围**：第 2 节 payload/调度签名、第 3 节端点/鉴权/字段口径。
- **冻结后**两个窗口即可并行开发。
- **变更**：需改契约时，改本文件 + 升版本号 + 通知三方；禁止各窗口私自加字段/改语义。
- **验收对接**：两系统开发完成后，由主系统侧按本契约逐项验证再接入。

---

## 5. 地基清单（主系统侧，我负责，两窗口开工前完成）

| # | 事项 | 服务对象 | 状态 |
|---|---|---|---|
| 1 | 播种 `souyisou_mobile` 调度渠道（共用关键词组、独立周期） | 手机采集 | ⬜ 待做 |
| 2 | 验证 `weixin_mobile` 来源渠道看板/去重/导出显示正常 | 手机采集 | ⬜ 待做 |
| 3 | 新增 API Key 只读鉴权 + 签发 OA 专用 key | OA | ⬜ 待做 |
| 4 | 冻结本契约 v1.0 | 双方 | ✅ 本文件 |

---

## 6. 待确认（不影响开工，可后补）
- 手机搜一搜 `souyisou_mobile` 的循环周期（默认拟 20 分钟）。
- OA 是否需要"按团队/权限过滤线索"（当前只读为全量）。
- 后续若 OA 要回写跟进状态，另立《OA 回写契约》。

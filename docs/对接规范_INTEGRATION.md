# 线索平台对接规范（Integration Contract）

> **状态**：v2.0-draft（2026-08-22），是新开发唯一的**架构边界**，但不是可直接编码的字段契约。具体 OpenAPI/事件 JSON Schema、枚举和错误码冻结并升为 `v2.0` 后，各窗口才可并行实现。旧 v1 的 Redis/Celery 与只读 OA 方案仅用于现有节点过渡，不得复制到新系统。

---

## 1. 对接原则

1. **契约先行**：接口、事件和错误语义冻结后才能由多个窗口并行实现；变更必须兼容或升版本。
2. **租户强制**：调用方不能用任意请求参数切换公司；租户来自受信身份，服务端仍执行角色授权和 PostgreSQL RLS。
3. **至少一次投递 + 业务幂等**：网络重试是正常情况，任何重复请求不得重复建文档、重复调用同版本 LLM、重复建租户候选或商机。
4. **不共享内部设施**：外部节点和 OA/CRM 不获得数据库、Redis、Celery 或内部表结构；只使用 HTTPS API 或版本化事件。
5. **证据不丢失**：标准化数据必须能回溯到原文、来源、采集节点和抓取时间。
6. **凭据不入库**：真实地址、密钥和口令只通过密钥管理或部署环境注入。

---

## 2. 共享语义

| 概念 | 说明 | 隔离范围 |
|---|---|---|
| `source_document` | 一次采集得到的原始文章/网页证据 | 公共情报层 |
| `event_series` | 可持续复办的活动系列和周期规律 | 公共情报层 |
| `event_edition` | 从一篇或多篇证据聚合出的某年/某届具体活动 | 公共情报层 |
| `organizer` | 主办方实体，可关联活动系列和届次 | 公共情报层 |
| `public_contact/contact_evidence` | 公开联系人及带来源、时间、可信度的联系方式证据 | 公共情报层 |
| `tenant_resource_grant` | 某公司对活动届次的可见/使用授权 | 租户私有 |
| `tenant_candidate` | 某家公司是否应审核该活动的候选 | 租户私有 |
| `tenant_review` | 资源审核员的核实结论与原因 | 租户私有 |
| `opportunity` | 审核通过后交给业务员的销售商机 | 租户私有 |
| `activity/outcome` | 联系、提醒、报价、成交/流失及原因 | 租户私有 |

同一 `event_edition` 可以为三家公司各产生一条授权和 `tenant_candidate`，各自审核、分配和跟进；公司之间不能读取对方状态。公司内部同一届活动默认只有一个活跃 `opportunity`。

---

## 3. 采集连接器 → 平台

### 3.1 目标链路

```text
连接器 -> 领取任务/租约 API -> 渠道采集 -> 提交原始文档 API
      -> 标准化/去重 -> 活动与主办方解析 -> AI 研判 -> 租户候选分发
```

- 微信搜一搜 PC 是当前主连接器；手机、网站、新闻、搜索引擎等使用同一连接器协议。
- 实时任务使用高优先级队列；近 3 年历史回溯使用低优先级、可暂停和可续跑队列。
- 现有受控局域网 PC 节点可以在迁移期继续按任务名投递 Celery，但这不是新节点的开发模板。

### 3.2 标准原始文档（v2 draft）

```json
{
  "contract_version": "2.0",
  "idempotency_key": "connector-defined-stable-key",
  "connector_id": "wechat-pc-01",
  "task_lease_id": "server-issued-lease-id",
  "source_channel": "wechat_pc",
  "source_url": "https://example.invalid/article",
  "source_external_id": "optional-source-id",
  "title": "article title",
  "content": "raw html or text",
  "content_type": "text/html",
  "content_hash": "sha256-of-raw-content",
  "language": "zh-CN",
  "author_or_account": "account name",
  "published_at": "2026-08-22T08:00:00+08:00",
  "collected_at": "2026-08-22T09:00:00+08:00",
  "matched_keywords": ["评选", "投票"],
  "collection_mode": "realtime_signal",
  "backfill_batch_id": null,
  "source_metadata": {}
}
```

约束：

- 必填字段、长度、时间格式、允许的 `source_channel` 和 `collection_mode` 由最终 JSON Schema/OpenAPI 定义；首版取值为 `realtime_signal`、`historical_backfill`。
- `idempotency_key` 在同一连接器内稳定；平台还会基于规范 URL、来源 ID 和内容指纹做二次去重。
- `source_metadata` 只承载渠道特有原始字段，不能借此绕过正式字段和版本流程；正文格式、编码、单条/批量大小限制由最终 Schema 冻结。
- 资源作用域不接受调用方任意提交的 `tenant_id`/租户列表。服务端根据受信 `task_lease_id`、连接器身份或已认证上传者推导 `public`/`tenant_private` 作用域及授权；文档、AI 结果和后续实体继承该作用域，防止私有信源误入共享池。
- `historical_backfill` 必须带服务端签发的 `backfill_batch_id`；批次冻结 `as_of`、`range_start`、`range_end` 和主题切片，确保暂停续跑后的统计口径不漂移。
- 服务端接收成功不代表已成为线索；后续处理状态通过回执 ID 查询或事件通知。

### 3.3 HTTPS 接入要求

- 连接器使用短期凭据或签名请求；凭据绑定 `connector_id`、允许渠道、任务/资源作用域和速率。
- 每次提交携带 `Idempotency-Key`、时间戳和请求签名；服务端执行重放保护、大小限制、限流和审计。
- 批量提交必须逐条返回状态，单条坏数据不能让整批结果语义不明。
- 任务领取采用有期限租约；节点上报成功、失败、可重试错误和心跳，租约过期后任务可重领。
- 禁止把 Redis、Celery Broker、PostgreSQL 暴露到公网或交给第三方节点。

---

## 4. 平台 ↔ OA/CRM

OA/CRM 的首要范围是资源审核、销售待办和客户跟进，不是另建一套重复的线索主数据。

### 4.1 读取

- 使用版本化、分页的租户 API 读取本公司候选、审核、商机、主办方公共画像和授权的联系证据。
- API 身份绑定租户、角色和权限范围；查询参数只能缩小已授权范围，不能扩大范围。
- 响应包含稳定 ID、`updated_at` 和游标，支持可靠增量同步；不得以直接查内部表或只读视图作为默认集成方式。

### 4.2 写入与回流

- 审核结论、分配、联系记录、阶段、报价、成交/流失通过命令式 API 写入，并要求幂等键和乐观并发版本。
- 每次状态变化记录操作者、来源系统、发生时间和审计轨迹。
- 成交/流失属于智能闭环的高价值反馈，但只能进入对应租户的私有反馈域；跨租户训练使用前必须先做明确的治理与脱敏决策。

### 4.3 事件（需要异步集成时）

建议事件名：`candidate.created.v1`、`review.completed.v1`、`opportunity.assigned.v1`、`opportunity.stage_changed.v1`、`opportunity.closed.v1`。

每个事件至少包含：

- `message_id`（消息唯一 ID）、`event_type`、`occurred_at`、`schema_version`；
- `tenant_id`、业务聚合 ID、聚合版本；
- `correlation_id`/`causation_id`，用于追踪和防循环；
- 最小必要载荷，不在跨系统消息中传播无关联系方式或隐私字段。

消费者必须按 `message_id` 幂等消费；生产方使用事务 Outbox 或等效机制，避免“数据库已提交但消息未发出”。业务活动键始终命名为 `event_edition_id`，不得与消息 ID 混用。

---

## 5. 资源分发策略

| 策略 | 语义 | 当前状态 |
|---|---|---|
| `shared_competition` | 所有符合条件的租户分别得到候选 | 当前默认 |
| `tenant_private` | 仅资源所属公司可见 | 用于租户自建关键词/私有补充 |
| `selected_tenants` | 仅指定公司得到候选 | 预留 |
| `exclusive` | 只允许一个公司获得资源 | 预留，未有商业规则不得启用 |

授权/候选的稳定幂等键覆盖 `(tenant_id, event_edition_id)`；`policy_version` 记录在授权历史中，不参与生成重复候选。策略变更必须通过显式授予、撤销或 supersede 流程，不能静默改写已经存在的审核和商机数据。

---

## 6. 错误与重试

| 类型 | 示例 | 调用方动作 |
|---|---|---|
| 参数错误 | Schema 不合法、未知渠道 | 修正后重提，不盲目重试 |
| 未认证/未授权 | 签名错误、租户越权 | 立即停止并告警 |
| 冲突 | 幂等键载荷不一致、版本冲突 | 查询现状后人工/业务合并 |
| 限流/暂时故障 | 429、超时、5xx | 指数退避并保留原幂等键 |
| 已接收处理中 | 202 | 用回执查询或等待完成事件 |

日志和错误响应不得返回凭据、数据库连接、跨租户 ID 列表或原始堆栈。

---

## 7. 契约冻结与验收

v2 从 draft 升为 frozen 前必须同时交付：

- OpenAPI 3.x 与 JSON Schema、示例请求/响应、错误码和兼容策略；
- 生产者与消费者契约测试；
- 重复提交、乱序、超时重试和部分失败测试；
- 三租户正向/反向越权测试；
- 性能与限流基线、审计与告警验证；
- v1 过渡节点迁移、灰度和回滚步骤。

契约变更由集成负责人串行合并。新增可选字段可做兼容小版本；删除字段、改类型、改语义或扩大权限必须升大版本，并保留明确的淘汰窗口。

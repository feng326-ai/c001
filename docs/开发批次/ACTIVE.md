# 当前开发批次与文件租约

> 仅集成负责人维护。领取任务前必须核对本表；未登记的窗口不得写入仓库文件。当前处于“设计基线收口”，尚未开始数据库迁移或业务功能实现。

## 活动任务

| 任务 ID | 负责人 | 分支/工作区 | 可写路径 | 交界文件/契约 | 状态 | 交接条件 |
|---|---|---|---|---|---|---|
| `ARCH-BASELINE-001` | 集成负责人 `/root` | 共享工作区 / `main` | `.env.example`<br>`AGENTS.md`<br>`README.md`<br>`docker-compose.yml`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/对接规范_INTEGRATION.md`<br>`docs/总体架构与演进路线.md`<br>`docs/需求登记.md`<br>`docs/领域模型与渐进迁移方案.md`<br>`docs/多智能体协作与交付规范.md`<br>`docs/开发批次/ACTIVE.md` | D-016～D-020、Integration v2 draft | 已完成 | 设计/QA 交叉审查与文档门禁通过；以本批次提交 SHA 为准 |

## 下一批候选（尚未领取、没有写租约）

| 建议任务 ID | 目标 | 前置条件 |
|---|---|---|
| `QA-FOUNDATION-001` | CI、Secret 扫描、迁移 checksum 校验、PostgreSQL/Redis 隔离测试夹具 | 架构基线合入；冻结 CI 占位环境变量 |
| `CONTRACT-CORE-001` | 冻结 `tenant/event_edition/grant/candidate/review/opportunity` 最小 schema、状态机和 OpenAPI | 业务确认“近三年”口径；集成负责人分配契约租约 |
| `MIGRATION-TENANT-001` | 第一批 expand-only 多租户/审核地基迁移 | 最小契约冻结；真正的迁移测试环境可用；迁移号已分配 |
| `COLLECT-STABILITY-001` | 微信采集成功率、错误分类与单 VM canary | 不改变公共采集 Schema；采集文件租约已登记 |

## 当前锁与禁止事项

- 当前没有生产代码、数据库迁移或生产部署租约。
- `tools/inspect_ui.py`、`docker/next_nginx.conf`、`docs/architecture.md` 是本批次开始前已有的非本批次改动/文件，集成时不得覆盖或夹带。
- 在真正开发/预发布环境建立前，不得把本地生产采集平面当作测试库运行清库、破坏性迁移或 `tests/run_tests.py`。
- Integration v2 仍为 draft；字段 JSON Schema 冻结前，采集、OA/CRM 和前端窗口不得据此独立编码。

# 当前开发批次与文件租约

> 仅集成负责人维护。领取任务前必须核对本表；未登记的窗口不得写入仓库文件。当前处于“设计基线收口”，尚未开始数据库迁移或业务功能实现。

## 活动任务

| 任务 ID | 负责人 | 分支/工作区 | 可写路径 | 交界文件/契约 | 状态 | 交接条件 |
|---|---|---|---|---|---|---|
| `ARCH-BASELINE-001` | 集成负责人 `/root` | 共享工作区 / `main` | `.env.example`<br>`AGENTS.md`<br>`README.md`<br>`docker-compose.yml`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/对接规范_INTEGRATION.md`<br>`docs/总体架构与演进路线.md`<br>`docs/需求登记.md`<br>`docs/领域模型与渐进迁移方案.md`<br>`docs/多智能体协作与交付规范.md`<br>`docs/开发批次/ACTIVE.md` | D-016～D-020、Integration v2 draft | 已完成 | 设计/QA 交叉审查与文档门禁通过；以本批次提交 SHA 为准 |
| `ENV-SPLIT-001` | 集成负责人 `/root` | 共享工作区 / `main` | `.dockerignore`<br>`.env.staging.example`<br>`docker-compose.staging.yml`<br>`docs/环境拆分与发布手册.md`<br>`docs/环境拆分验收记录_2026-08-22.md`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/开发批次/ACTIVE.md` | D-021、测试/采集运行边界 | 已完成 | 独立测试栈恢复演练、危险开关、隧道和生产采集回归全部通过；遗留风险已登记 |
| `COLLECT-STABILITY-001` | 集成负责人 `/root`（采集侧只读审查：协作智能体） | 共享工作区 / `main` | `wxsearch/unattended.py`<br>`wxsearch/config.py`<br>`wxsearch/tasks.py`<br>`wxsearch/task_scheduler.py`<br>`tests/test_unattended_heartbeat.py`<br>`tests/test_device_leases.py`<br>`tests/test_device_leases_integration.py`<br>`docs/migrations/008_keyword_channel_state.sql`<br>`docker-compose.collect-release.yml`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/开发批次/ACTIVE.md` | D-022、D-024、v2 `(channel,keyword,lease_id)`；旧 VM 调用向后兼容；迁移 008 + 渠道完整播种硬前置 | 进行中 | 32 项快速门禁及隔离 PostgreSQL 15 + Redis 7 的 10 项真实集成测试全部通过（源码提交 `8842985`）；现网 `souyisou` 63 行且缺失数为 0 已只读核验。下一步先升级完整服务端 worker，再做单 VM canary，跨过长关键词且无误释放后决定扩量 |
| `DEPLOY-COLLECT-CANARY-001` | 集成负责人 `/root` | Ubuntu 本地生产采集平面 | `/home/m/releases/<sha>`<br>`wxsearch_worker`（仅重建这一项）<br>单台采集 VM（服务端稳定后再选） | 必须使用已推送 SHA 与 `docker-compose.collect-release.yml`；禁止改 `/home/m/xiansuo`、PG/Redis 卷及其余容器 | 进行中 | 服务端 Worker 已切换到 `7c0e426` 只读发布目录并通过旧协议兼容观察：心跳 2、领取 1、结果 1、文章 54、正式同步 2 均成功且 0 Traceback；下一步由一台 v2 VM 跨过长关键词，心跳/租约/结果无误释放、无重复后结束部署租约 |
| `CONTRACT-CORE-001` | 集成负责人 `/root`（领域只读审查：协作智能体） | 共享工作区 / `main` | `docs/核心领域契约_v1.md`<br>`docs/决策记录.md`<br>`docs/需求登记.md`<br>`docs/对接规范_INTEGRATION.md`<br>`docs/开发批次/ACTIVE.md` | tenant / organizer / event_series / event_edition / review / opportunity 最小契约 | 已完成 | 状态机、唯一键、字段所有权、租户隔离和旧模型映射已冻结并通过最终 P0/P1 审查；本批次不创建业务表 |

## 下一批候选（尚未领取、没有写租约）

| 建议任务 ID | 目标 | 前置条件 |
|---|---|---|
| `QA-FOUNDATION-001` | CI、Secret 扫描、迁移 checksum 校验、PostgreSQL/Redis 隔离测试夹具 | 架构基线合入；冻结 CI 占位环境变量 |
| `MIGRATION-TENANT-001` | 第一批 expand-only 多租户/审核地基迁移 | 最小契约冻结；真正的迁移测试环境可用；迁移号已分配 |

## 当前锁与禁止事项

- 当前 `COLLECT-STABILITY-001` 持有采集循环代码租约；`DEPLOY-COLLECT-CANARY-001` 仅持有上表列明的生产采集灰度部署租约。没有数据库迁移租约，也不得改动其他生产容器。
- `tools/inspect_ui.py`、`docker/next_nginx.conf`、`docs/architecture.md` 是本批次开始前已有的非本批次改动/文件，集成时不得覆盖或夹带。
- 在真正开发/预发布环境建立前，不得把本地生产采集平面当作测试库运行清库、破坏性迁移或 `tests/run_tests.py`。
- Integration v2 仍为 draft；字段 JSON Schema 冻结前，采集、OA/CRM 和前端窗口不得据此独立编码。

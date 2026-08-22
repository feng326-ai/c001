# 当前开发批次与文件租约

> 仅集成负责人维护。领取任务前必须核对本表；未登记的窗口不得写入仓库文件。当前处于“设计基线收口”，尚未开始数据库迁移或业务功能实现。

## 活动任务

| 任务 ID | 负责人 | 分支/工作区 | 可写路径 | 交界文件/契约 | 状态 | 交接条件 |
|---|---|---|---|---|---|---|
| `ARCH-BASELINE-001` | 集成负责人 `/root` | 共享工作区 / `main` | `.env.example`<br>`AGENTS.md`<br>`README.md`<br>`docker-compose.yml`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/对接规范_INTEGRATION.md`<br>`docs/总体架构与演进路线.md`<br>`docs/需求登记.md`<br>`docs/领域模型与渐进迁移方案.md`<br>`docs/多智能体协作与交付规范.md`<br>`docs/开发批次/ACTIVE.md` | D-016～D-020、Integration v2 draft | 已完成 | 设计/QA 交叉审查与文档门禁通过；以本批次提交 SHA 为准 |
| `ENV-SPLIT-001` | 集成负责人 `/root` | 共享工作区 / `main` | `.dockerignore`<br>`.env.staging.example`<br>`docker-compose.staging.yml`<br>`docs/环境拆分与发布手册.md`<br>`docs/环境拆分验收记录_2026-08-22.md`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/开发批次/ACTIVE.md` | D-021、测试/采集运行边界 | 已完成 | 独立测试栈恢复演练、危险开关、隧道和生产采集回归全部通过；遗留风险已登记 |
| `COLLECT-STABILITY-001` | 集成负责人 `/root`（采集侧只读审查：协作智能体） | 共享工作区 / `main` | `wxsearch/unattended.py`<br>`wxsearch/config.py`<br>`wxsearch/tasks.py`<br>`wxsearch/task_scheduler.py`<br>`tests/test_unattended_heartbeat.py`<br>`tests/test_device_leases.py`<br>`tests/test_device_leases_integration.py`<br>`docs/migrations/008_keyword_channel_state.sql`<br>`docker-compose.collect-release.yml`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/开发批次/ACTIVE.md` | D-022、D-024、D-025、v2 `(channel,keyword,lease_id)`；旧 VM 调用向后兼容；迁移 008 + 渠道完整播种硬前置 | 已完成 | 48 项门禁全部通过，其中 12 项使用隔离 PostgreSQL 15 + Redis 7；现网启用关键词 67 个，`souyisou` 成员 61 个且缺失为 0。服务端和单 VM 灰度均验收通过，详见《采集v2单机灰度验收记录_2026-08-23》 |
| `DEPLOY-COLLECT-CANARY-001` | 集成负责人 `/root` | Ubuntu 本地生产采集平面 | `/home/m/releases/<sha>`<br>`wxsearch_worker`（仅重建这一项）<br>单台采集 VM `win10-01` | 使用已推送 SHA 与采集发布覆盖文件；禁止改 `/home/m/xiansuo`、PG/Redis 卷及其余容器 | 已完成 | Worker 运行只读发布目录 `75c451f`，门禁为 `win10-01:2`；`win10-01` 完成 660 秒观察、12 次不同续租时间戳及自然结果回传（1 篇），期间无错误释放、无 Traceback；正式同步持续成功。其余三台仍保持 v1，未扩量 |
| `CONTRACT-CORE-001` | 集成负责人 `/root`（领域只读审查：协作智能体） | 共享工作区 / `main` | `docs/核心领域契约_v1.md`<br>`docs/决策记录.md`<br>`docs/需求登记.md`<br>`docs/对接规范_INTEGRATION.md`<br>`docs/开发批次/ACTIVE.md` | tenant / organizer / event_series / event_edition / review / opportunity 最小契约 | 已完成 | 状态机、唯一键、字段所有权、租户隔离和旧模型映射已冻结并通过最终 P0/P1 审查；本批次不创建业务表 |
| `QA-FOUNDATION-001` | 集成负责人 `/root`（质量、迁移、安全只读审查：协作智能体） | 共享工作区 / `main` | `.github/workflows/ci.yml`<br>`.env.qa.example`<br>`.gitattributes`<br>`.gitignore`<br>`pytest.ini`<br>`requirements-dev.txt`<br>`Dockerfile.qa`<br>`docker-compose.qa.yml`<br>`tools/check_secrets.py`<br>`tools/check_migrations.py`<br>`tests/conftest.py`<br>`tests/test_secret_scan.py`<br>`tests/test_migration_integrity.py`<br>`tests/test_migration_history_policy.py`<br>`wxsearch/migrations/run.py`<br>`docs/migrations/README.md`<br>`docs/migrations/schema_migrations.sql`<br>`docs/migrations/checksum_baseline.json`<br>`docs/QA质量地基验收记录_2026-08-23.md`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/开发批次/ACTIVE.md` | CI、安全占位变量、规范化 LF SHA-256、精确旧 MD5 + 最终态兼容、跨提交 append-only、一次性 PG/Redis 测试环境 | 已完成 | 169 个已跟踪文件 Secret 门禁通过；四份 Compose 渲染、Ruff fatal gate、001～020 全量迁移/只读复核和 101 项测试全部通过；历史 018 精确 ledger+完整视图态已在 PG15 验证；临时容器/网络/目录均已销毁；PR/push 以 Git 基准提交冻结既有 SQL 与 baseline，只允许追加新版本 |
| `SEC-CREDENTIAL-BOUNDARY-001` | 集成负责人 `/root`（安全只读审查：协作智能体） | 共享工作区 / `main` | `wxsearch/api/main.py`<br>`wxsearch/templates/system_settings.html`<br>`wxsearch/ai_filters/llm_client.py`<br>`wxsearch/sogou_loop.py`<br>`start_sogou_loop.bat`<br>`tests/test_security_boundaries.py`<br>`.gitignore`<br>`.dockerignore`<br>`config.json`（仅停止跟踪，保留本机文件）<br>`config.example.json`<br>`wxsearch/config.py`<br>`tests/test_config_contract.py`<br>`README.md`<br>`docs/决策记录.md`<br>`docs/坑位手册.md`<br>`docs/开发批次/ACTIVE.md` | 模型探测只允许 super + POST；自定义端点不得继承服务端密钥；采集日志令牌改请求头；采集节点不要求数据库口令；运行时配置缺失/启用态占位值 fail closed | 已完成 | 路由权限/方法、allowlist、禁止继承服务端 key、原子私有落盘、请求头令牌、无数据库口令节点和安全配置模板均有回归测试；`config.json` 不入 Git/Docker context，缺失配置或启用态缺显式 Redis 端点/唯一 VM 身份时拒绝启动；本批只提交代码，尚未部署、设置现有 Windows ACL 或轮换生产凭据 |

## 下一批候选（尚未领取、没有写租约）

| 建议任务 ID | 目标 | 前置条件 |
|---|---|---|
| `MIGRATION-TENANT-001` | 第一批 expand-only 多租户/审核地基迁移 | 最小契约冻结；真正的迁移测试环境可用；迁移号已分配 |
| `ROLLOUT-COLLECT-V2-002` | 按单机灰度流程依次升级 `win10-02/03/04`，每台独立验收和回滚 | `win10-01` 连续观察至少 12～24 小时；无本机 fence、heartbeat、result 错误；逐台确认唯一 MAC / `vm_instance_id` |
| `QA-CONTRACT-002` | 登录/角色、同步 HMAC/重放/乱序、字段所有权和未来 RLS 契约测试 | 必须在第一批多租户迁移合并前完成，可与迁移实现并行但由 QA 独立验收 |
| `QA-SUPPLYCHAIN-002` | Gitleaks 全历史、服务端/Windows 采集依赖锁、pip-audit、Compose 策略和 Trivy 镜像门禁 | 先拆分运行时依赖；对历史命中逐条核实，不用 baseline 掩盖真实凭据 |
| `CLEANUP-REDIS-SET-003` | 把结果缓存的弃用 `setex` 调用改为 `set(..., ex=...)` 并清除 5 条测试警告 | 不改变 key、TTL 或返回语义；单独小提交 |

## 当前锁与禁止事项

- `QA-FOUNDATION-001` 与 `SEC-CREDENTIAL-BOUNDARY-001` 已验收并释放文件租约；当前没有采集扩量、业务数据库迁移或生产部署租约，不得改动任何生产容器或其数据卷。
- `tools/inspect_ui.py`、`docker/next_nginx.conf`、`docs/architecture.md` 是本批次开始前已有的非本批次改动/文件，集成时不得覆盖或夹带。
- 在真正开发/预发布环境建立前，不得把本地生产采集平面当作测试库运行清库、破坏性迁移或 `tests/run_tests.py`。
- Integration v2 仍为 draft；字段 JSON Schema 冻结前，采集、OA/CRM 和前端窗口不得据此独立编码。

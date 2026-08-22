# Migrations Directory

## 用途

存放数据库结构迁移脚本，用于：

- ✅ **云端首次部署**：`db_schema.sql` + 所有 migrations 顺序执行，确保新库结构完整
- ✅ **现有库升级**：只执行未跑过的迁移脚本（通过 `schema_migrations` 跟踪）
- ✅ **历史防篡改**：执行任何 pending 迁移前，先核对连续版本、冻结基线与数据库 checksum
- ✅ **版本控制**：新迁移写规范化 LF SHA-256；已执行文件永久只读，修正必须新增版本

---

## 文件命名规范

```
NNN_description.sql
```

- `NNN`：三位数字序号，保证执行顺序
- `description`：英文描述，下划线分隔（中文在 commit message 里写）

示例：
- `001_fix_columns_and_add_resource_level.sql`
- `002_keyword_stats_views.sql`

---

## 运行方式

### Docker 环境

```bash
# 进入项目目录
cd g:/qoder/ss

# 运行迁移器
docker exec wxsearch_worker python -m wxsearch.migrations.run
```

### 本地开发

```bash
# PowerShell
python -m wxsearch.migrations.run
```

### 只读发布前检查

```bash
python tools/check_migrations.py
python -m wxsearch.migrations.run --check
```

第一条只检查仓库文件名、连续版本和冻结 SHA-256；第二条连接目标数据库，只读确认 ledger
存在、没有 pending、没有断档，且每个已执行版本的 checksum 匹配。`--check` 不创建表、不执行 SQL、
不修改 checksum。

---

## 添加新迁移的步骤

1. **新建 `.sql` 文件**：按序号放在 `docs/migrations/` 目录
2. **确保可收敛**：优先 expand-only；不同起始 schema 必须得到相同最终结构
3. **更新冻结基线**：只为新版本增加 SHA-256；不得改旧版本的基线
4. **隔离测试**：空库完整执行、重复执行、`--check` 与篡改失败路径全部通过
5. **提交代码**：迁移、契约、测试和 checksum 基线同一批提交
6. **发布**：单独一次性 migrate job 持有 advisory lock；Backend/Worker 不并发自动迁移

---

## 回滚与修复策略

禁止删除 `schema_migrations` 记录后重跑旧文件，也禁止改数据库 checksum 掩盖漂移。
结构修复使用更高编号的向前收敛迁移；需要恢复数据时使用发布前验证过的完整备份，并按发布手册执行。
迁移前备份示例：

```bash
docker exec wxsearch_db pg_dump -U admin wx_search > backup_before_migration.sql
```

---

当前版本与校验值以 `checksum_baseline.json` 和实际 `NNN_description.sql` 文件为准，不在本文维护第二份易漂移列表。

## 021 租户地基的启用边界

`021_tenant_identity_rls.sql` 当前只是 expand-only、休眠的表结构保护网。合并或执行该迁移不代表租户功能已经安全上线；现有页面、API 和 worker 不得读取或写入这些新表。

任何租户功能启用前，必须先拆分迁移 owner 与最小权限运行角色，提供同一物理连接和同一事务内的 `tenant_transaction`，并用已认证用户的 active membership 完成服务端授权。`app.tenant_id` 自定义 GUC 只作为 RLS 的事务作用域，不能替代身份认证。生产迁移和角色授权必须作为独立发布步骤，经备份恢复、影子库与预发布验收后执行。

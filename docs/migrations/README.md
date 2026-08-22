# Migrations Directory

## 用途

存放数据库结构迁移脚本，用于：

- ✅ **云端首次部署**：`db_schema.sql` + 所有 migrations 顺序执行，确保新库结构完整
- ✅ **现有库升级**：只执行未跑过的迁移脚本（通过 `schema_migrations` 跟踪）
- ✅ **幂等安全**：全部用 `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`，可重复执行不报错
- ✅ **版本控制**：每次结构变更都有脚本、有记录、可回溯

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

---

## 添加新迁移的步骤

1. **新建 `.sql` 文件**：按序号放在 `docs/migrations/` 目录
2. **确保幂等性**：所有操作必须是 `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`
3. **测试运行**：先在本地 Docker 跑一次，再验证重复执行是否跳过
4. **提交代码**：连同 Python 运行器一起提交
5. **云端应用**：按上述方式执行迁移器

---

## 回滚策略

不建议频繁回滚。如需回滚：

1. 手动删除 `schema_migrations` 中对应版本的记录
2. 重新运行迁移器（会重跑该版本）

建议先备份数据库再操作：

```bash
docker exec wxsearch_db pg_dump -U admin wx_search > backup_before_migration.sql
```

---

## 当前迁移列表

| 版本 | 描述 | 状态 |
|------|------|------|
| 001 | fix columns (JSONB→TEXT[]), add resource_level, keywords ARRAY | ✅ Created |
| 002 | keyword stats views & mapping table | 🚧 To Be Created |

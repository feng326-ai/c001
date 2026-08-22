"""存量线索 AI 回补脚本：对 qualified_leads 的 event_name/event_details 进行规则抽取填充。

用法：
    docker exec wxsearch_worker python -m wxsearch.migrations.fill_event_info
    docker exec wxsearch_worker python -m wxsearch.migrations.fill_event_info --force

说明：
  - 默认「只填空」：已有活动名称/活动信息的记录不会被改写（保护人工编辑）。
  - 加 --force：用当前规则重新抽取并「覆盖」全部记录（含旧 AI 草稿）；同步更新 articles_core。
    注意：--force 会连同人工编辑一并覆盖，请确认后再用。
"""

import os
import sys

import psycopg2

BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
os.environ.setdefault("RULE_CONFIG_PATH", os.path.join(BASE_DIR, "rule_config.json"))

from wxsearch.ai_filters.event_extractor import extract_event_info


def run(force: bool = False):
    from wxsearch.tasks import _db_config

    conn = psycopg2.connect(**_db_config())
    cur = conn.cursor()
    cur.execute(
        "SELECT id, article_id, title, content, event_name, event_details "
        "FROM qualified_leads WHERE status != 'not_relevant'"
    )
    rows = cur.fetchall()
    mode = "覆盖(--force)" if force else "只填空"
    print(f"开始扫描 qualified_leads（{len(rows)} 条）… 模式：{mode}")

    updated, skipped_empty_title = 0, 0
    for lead_id, article_id, title, content, en, ed in rows:
        if not (title or "").strip():
            skipped_empty_title += 1
            continue
        # 只填空模式：两个字段都已有值则跳过
        if not force and en and en.strip() and ed and ed.strip():
            continue

        name, details = extract_event_info(title or "", content or "")

        if force:
            # 覆盖模式：直接写入本次抽取结果（可能为空）
            cur.execute(
                "UPDATE qualified_leads SET event_name = %s, event_details = %s WHERE id = %s",
                (name or None, details or None, lead_id),
            )
            if article_id is not None:
                cur.execute(
                    "UPDATE articles_core SET event_name = %s, event_details = %s WHERE id = %s",
                    (name or None, details or None, article_id),
                )
        else:
            # 只填空：COALESCE(NULLIF(...), value) → 非空值保持，空值才替换
            cur.execute(
                """
                UPDATE qualified_leads
                SET event_name    = COALESCE(NULLIF(event_name, ''), %s),
                    event_details = COALESCE(NULLIF(event_details, ''), %s)
                WHERE id = %s
                """,
                (name or None, details or None, lead_id),
            )
        updated += 1
        if updated % 50 == 0:
            conn.commit()
            print(f"  已处理 {updated} 条…")

    conn.commit()
    print(f"\n✅ 完成：处理 {updated} 条；跳过 {skipped_empty_title} 条（标题为空）。")
    if not force:
        print("提示：已有活动名称/信息的记录不会被覆盖；如需重算请加 --force。\n")
    else:
        print("提示：已按当前规则覆盖全部记录（含 articles_core 同步）。\n")
    cur.close()
    conn.close()


if __name__ == "__main__":
    run(force="--force" in sys.argv[1:])

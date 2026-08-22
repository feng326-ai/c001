#!/usr/bin/env python3
"""recompute_scores.py: 存量线索两维评分回补（方向2 正交化配套）。

背景：优先级与资源等级正交化后（优先级=时效紧迫度、资源等级=规模价值），
存量 qualified_leads 里仍是旧口径的分数，需按新规则重算这两维。

做法（最小侵入、非破坏、可重复执行）：
  - 只重算「优先级(priority_score/priority_level)」与「资源等级(resource_level)」两维，
    复用 RuleScorer 的生产同款方法 _urgency / _judge_resource_level；
  - 不改动 is_lead / intent_category / lead_type：既有线索身份保持不变，避免误删；
  - scoring_breakdown 只更新 urgency / resource_level / scale_groups 三个子键，
    保留原有 relevance 明细；
  - articles_core 与 qualified_leads 同步更新，保证两表口径一致；
  - 幂等：读-改-写，重复运行结果一致。

用法：
    docker exec wxsearch_worker python -m wxsearch.migrations.recompute_scores
本地：
    cd g:/qoder/ss && python -m wxsearch.migrations.recompute_scores
"""

import json

import psycopg2

from wxsearch.tasks import _db_config
from wxsearch.ai_filters.rule_scorer import RuleScorer


def main() -> None:
    scorer = RuleScorer()
    conn = psycopg2.connect(**_db_config())
    cur = conn.cursor()

    # 拉取全部线索（join articles_core 取正文/发布时间用于重算）
    cur.execute(
        """
        SELECT q.article_id, a.title, a.content, a.content_clean,
               a.publish_time, a.scoring_breakdown
        FROM qualified_leads q
        JOIN articles_core a ON a.id = q.article_id
        """
    )
    rows = cur.fetchall()
    print(f"[i] 待重算线索：{len(rows)} 条")

    updated = 0
    for article_id, title, content, content_clean, publish_time, breakdown in rows:
        text = f"{title or ''}\n{content_clean or content or ''}"

        # 维度②优先级 = 时效紧迫度；维度③资源等级 = 规模价值（既有线索按 is_lead=True 评规模）
        priority_score, priority_level, urgency_bd = scorer._urgency(publish_time, text)
        resource_level, matched_groups = scorer._judge_resource_level(text, True)

        # 合并进既有 scoring_breakdown，保留 relevance 子对象
        bd = breakdown if isinstance(breakdown, dict) else {}
        bd["urgency"] = urgency_bd
        bd["resource_level"] = resource_level
        if matched_groups:
            bd["scale_groups"] = matched_groups
        else:
            bd.pop("scale_groups", None)
        bd_json = json.dumps(bd, ensure_ascii=False)

        cur.execute(
            """
            UPDATE articles_core
            SET priority_score = %s, priority_level = %s, resource_level = %s,
                scoring_breakdown = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (priority_score, priority_level, resource_level, bd_json, article_id),
        )
        cur.execute(
            """
            UPDATE qualified_leads
            SET priority_score = %s, priority_level = %s, resource_level = %s,
                scoring_breakdown = %s, updated_at = NOW()
            WHERE article_id = %s
            """,
            (priority_score, priority_level, resource_level, bd_json, article_id),
        )
        updated += 1

    conn.commit()
    print(f"[+] 已重算并回写：{updated} 条")

    # 分布校验：两维应彼此独立，不再「同为 P0/满分却优/普通混杂」
    cur.execute(
        """
        SELECT priority_level, resource_level, COUNT(*)
        FROM qualified_leads
        GROUP BY priority_level, resource_level
        ORDER BY priority_level, resource_level
        """
    )
    print("\n=== 分布校验 (priority_level x resource_level) ===")
    for plevel, rlevel, cnt in cur.fetchall():
        print(f"  {plevel or 'NULL':<6} x {rlevel or 'NULL':<10} : {cnt}")

    cur.close()
    conn.close()
    print("\n\u2705 recompute_scores 完成")


if __name__ == "__main__":
    main()

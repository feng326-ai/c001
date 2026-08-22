"""查看/导出数据库内容。

用法：
    python -m tools.query_db                      # 打印最近 20 条
    python -m tools.query_db --keyword 人工智能    # 按关键词过滤
    python -m tools.query_db --limit 50
    python -m tools.query_db --csv out.csv        # 导出为 CSV
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys

from wxsearch.config import load_config


def main() -> int:
    parser = argparse.ArgumentParser(description="查看微信搜一搜采集结果")
    parser.add_argument("-c", "--config", default="config.json")
    parser.add_argument("--keyword", help="按关键词过滤")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--csv", help="导出为 CSV 文件路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row

    sql = "SELECT keyword, title, account, publish_time, summary, content, url, collected_at FROM articles"
    params = []
    if args.keyword:
        sql += " WHERE keyword = ?"
        params.append(args.keyword)
    sql += " ORDER BY id DESC"

    rows = conn.execute(sql, params).fetchall()

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["关键词", "标题", "公众号", "发布时间", "链接", "正文", "采集时间"])
            for r in rows:
                writer.writerow([r["keyword"], r["title"], r["account"], r["publish_time"], r["url"], r["content"], r["collected_at"]])
        print(f"已导出 {len(rows)} 条到 {args.csv}")
        return 0

    print(f"共 {len(rows)} 条，显示前 {args.limit} 条：\n")
    for r in rows[: args.limit]:
        content = r["content"] or ""
        print(f"[{r['keyword']}] {r['title']}")
        print(f"    公众号：{r['account']}  发布：{r['publish_time']}  采集：{r['collected_at']}")
        print(f"    链接：{r['url'] or '(未取到)'}")
        print(f"    正文：{len(content)} 字  预览：{content[:80].replace(chr(10), ' ')}")
        print()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

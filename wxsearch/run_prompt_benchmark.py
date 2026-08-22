# -*- coding: utf-8 -*-
"""黄金标尺评测器 —— 终结"0 标注盲调"，量化规则(阶段3)与 LLM(阶段4)两层的判定质量。

在 worker 容器内运行（有 DB + LLM 访问）：

    # 1) 导出候选样本供人工标注（按意图/优先级/投票分层抽样，均衡覆盖）
    docker exec wxsearch_worker python -m wxsearch.run_prompt_benchmark --export 60 > /tmp/cand.json
    #   人工在 tests/golden_leads_50.json 的 samples 里填 expected_* 标签（真值）

    # 2) 评测规则层（免费、快）
    docker exec wxsearch_worker python -m wxsearch.run_prompt_benchmark --layer rule

    # 3) 评测 LLM 层（调用大模型、耗 token）
    docker exec wxsearch_worker python -m wxsearch.run_prompt_benchmark --layer llm

指标（对齐计划目标）：
  - 价值判断准确率（has_lead_value vs 标尺，目标 >=90%，仅 LLM 层）
  - P0 精确率 Precision（预测P0且标尺P0 / 所有预测P0，目标 >=85%）
  - P0 查全率 Recall（预测P0且标尺P0 / 标尺P0总数，目标 >=80%）
  - 分池混淆（五池，仅 LLM 层，需 activity_status/voting_status）
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

# 黄金标尺文件在仓库根 tests/ 下；本模块在 wxsearch/ 内，故取 parent.parent（= /home/m/xiansuo）。
_GOLDEN_PATH = Path(__file__).parent.parent / "tests" / "golden_leads_50.json"


def _load_golden() -> list:
    """读黄金标尺 samples（对象根 {..., "samples":[...]}）；缺文件返回空。"""
    try:
        with open(_GOLDEN_PATH, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc.get("samples", []) if isinstance(doc, dict) else list(doc)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _fetch_lead(db, lead_id: int):
    """取一条线索的判定所需原料。"""
    rows = db.execute_query(
        "SELECT id, title, COALESCE(content_clean, content, '') AS body, account, publish_time "
        "FROM qualified_leads WHERE id = %s", (lead_id,))
    if not rows:
        return None
    r = rows[0]
    return SimpleNamespace(id=r[0], title=r[1] or "", content=r[2] or "",
                           content_clean=r[2] or "", account=r[3] or "", publish_time=r[4])


def classify_pool(has_value: bool, activity_status: str, voting_status: str,
                  is_annual: bool) -> str:
    """五池路由（与 leads.py 保持一致的口径，评测/线上共用一套判定）。"""
    if not has_value:
        return "归档"
    if voting_status == "suspect":
        return "待核实"
    if voting_status == "has" and activity_status == "进行中":
        return "竞品"
    if activity_status == "已结束":
        return "周期" if is_annual else "归档"
    if activity_status in ("征集中", "报名中"):
        return "商机"
    return "商机"  # 有价值但状态未知，默认进商机待核实


def _metrics(pairs: list, label: str):
    """pairs: [(pred, gold), ...]；打印 P0 精确率/查全率 + 总体一致率。"""
    n = len(pairs)
    if not n:
        print(f"  [{label}] 无可比样本")
        return
    agree = sum(1 for p, g in pairs if p == g)
    tp = sum(1 for p, g in pairs if p == "P0" and g == "P0")
    pred_p0 = sum(1 for p, _ in pairs if p == "P0")
    gold_p0 = sum(1 for _, g in pairs if g == "P0")
    prec = f"{tp/pred_p0*100:.1f}%" if pred_p0 else "—(未预测P0)"
    rec = f"{tp/gold_p0*100:.1f}%" if gold_p0 else "—(标尺无P0)"
    print(f"  [{label}] 样本 {n}｜总体一致率 {agree/n*100:.1f}%｜"
          f"P0 精确率 {prec}(目标≥85%)｜P0 查全率 {rec}(目标≥80%)")


def run_benchmark(layer: str):
    from wxsearch.db_connector import DatabaseConnector
    from wxsearch.ai_filters.rule_scorer import RuleScorer

    golden = _load_golden()
    labeled = [g for g in golden if g.get("expected_priority") or g.get("expected_value") is not None]
    if not labeled:
        print(f"⚠️ {_GOLDEN_PATH} 尚无已标注样本。先运行 --export N 导出候选并人工填 expected_* 标签。")
        return

    db = DatabaseConnector()
    scorer = RuleScorer() if layer in ("rule", "both") else None

    rule_pri, llm_pri, value_pairs, pool_pairs = [], [], [], []
    missing = 0

    llm_mod = None
    if layer in ("llm", "both"):
        from wxsearch.ai_filters import llm_analyzer as llm_mod  # noqa

    for g in labeled:
        art = _fetch_lead(db, int(g["id"]))
        if not art:
            missing += 1
            continue
        gold_pri = g.get("expected_priority")
        gold_val = g.get("expected_value")
        gold_pool = g.get("expected_pool")

        if scorer is not None and gold_pri:
            res = scorer.score(art)
            rule_pri.append((res.priority_level, gold_pri))

        if llm_mod is not None:
            data = llm_mod.analyze(art.title, art.content,
                                   str(art.publish_time) if art.publish_time else None)
            ai = llm_mod.to_ai_result(data)
            if gold_pri:
                llm_pri.append((ai.priority_level, gold_pri))
            if gold_val is not None:
                value_pairs.append((bool(data.get("is_valuable")), bool(gold_val)))
            if gold_pool:
                pred_pool = classify_pool(
                    bool(data.get("is_valuable")), data.get("activity_status", ""),
                    data.get("voting_status", "") or ("has" if data.get("is_online_voting") else ""),
                    bool(data.get("is_annual_recurring")))
                pool_pairs.append((pred_pool, gold_pool))

    print("=" * 66)
    print(f"黄金标尺评测（layer={layer}，标注 {len(labeled)} 条，缺失 {missing} 条）")
    print("=" * 66)
    if rule_pri:
        _metrics(rule_pri, "规则层 优先级")
    if llm_pri:
        _metrics(llm_pri, "LLM层 优先级")
    if value_pairs:
        acc = sum(1 for p, gg in value_pairs if p == gg) / len(value_pairs) * 100
        print(f"  [LLM层 价值判断] 准确率 {acc:.1f}%(目标≥90%)｜样本 {len(value_pairs)}")
    if pool_pairs:
        agree = sum(1 for p, gg in pool_pairs if p == gg) / len(pool_pairs) * 100
        print(f"  [LLM层 分池] 一致率 {agree:.1f}%｜混淆 {Counter((g, p) for p, g in pool_pairs)}")
    db.close()


def export_candidates(n: int):
    """分层抽样导出候选（按意图/优先级/投票均衡），输出可粘进 golden_leads_50.json 的模板行。"""
    from wxsearch.db_connector import DatabaseConnector
    db = DatabaseConnector()
    per = max(1, n // 5)
    buckets = [
        ("疑似P0-筹备", "priority_level='P0' AND has_lead_value=TRUE"),
        ("疑似P1-申报", "priority_level='P1' AND has_lead_value=TRUE"),
        ("疑似竞品-投票", "is_online_voting=TRUE AND activity_status='进行中'"),
        ("疑似周期-已结束", "activity_status='已结束'"),
        ("疑似垃圾", "has_lead_value=FALSE"),
    ]
    out = []
    for _name, cond in buckets:
        rows = db.execute_query(
            f"SELECT id, LEFT(title,60) FROM qualified_leads "
            f"WHERE llm_status='done' AND {cond} ORDER BY RANDOM() LIMIT %s", (per,))
        for r in rows:
            out.append({"id": r[0], "title": r[1],
                        "expected_value": None, "expected_priority": "",
                        "expected_pool": "", "note": _name})
    print(json.dumps({"_readme": "人工填 expected_value(true/false)/expected_priority(P0/P1/P2)/"
                      "expected_pool(商机/竞品/待核实/周期/归档)，再并入 tests/golden_leads_50.json 的 samples",
                      "samples": out}, ensure_ascii=False, indent=2))
    db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="黄金标尺评测器（规则/LLM 两层）")
    ap.add_argument("--layer", choices=["rule", "llm", "both"], default="rule")
    ap.add_argument("--export", type=int, default=0, help="导出 N 条分层候选供人工标注")
    args = ap.parse_args()
    if args.export:
        export_candidates(args.export)
    else:
        run_benchmark(args.layer)

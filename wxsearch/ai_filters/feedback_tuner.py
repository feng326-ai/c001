"""反馈驱动的规则微调器（方案 1 的「AI 自主进化」轻量实现）——离线、可解释、可回退。

思路：把你在看板上的人工标注（lead_feedback 表）当成「监督信号」，回过头衡量当前
规则判得准不准，并据此**自动微调** rule_config.json 里的阈值，让下一轮评分越来越贴近
你的真实判断。每次调整都写入配置的 _learning_log，可追溯、可手动回退。

安全设计（与全项目一致：可开关、失败不阻断、样本不足不乱动）：
  - 样本不足（< MIN_SAMPLES）一律只出报告、不改配置；
  - 每次仅小步微调（STEP），并对阈值上下限做钳制，避免震荡；
  - 默认 dry-run（只算不写），带 --apply 才真正写回配置；
  - 任何异常只 log，不抛出。

用法（宿主机）：
    docker exec wxsearch_worker python -m wxsearch.ai_filters.feedback_tuner          # 只看报告
    docker exec wxsearch_worker python -m wxsearch.ai_filters.feedback_tuner --apply  # 应用微调
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

from wxsearch.ai_filters.rule_scorer import load_rule_config, _DEFAULT_CONFIG_PATH

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("feedback_tuner")

# 微调守则
MIN_SAMPLES = 20        # 反馈样本少于此值不调参（避免过拟合到零星标注）
TARGET_PRECISION = 0.75  # 目标准确率（判为线索里人工确认相关的占比）
STEP = 3.0              # 每次微调步长（分）
LEAD_MIN_FLOOR = 30.0   # lead_min_score 下限
LEAD_MIN_CEIL = 70.0    # lead_min_score 上限


def _db():
    """复用 worker 的 DB 配置与连接（与 backfill 一致）。"""
    from wxsearch.tasks import _db_config
    from wxsearch.smart_dedup_store import SmartDedupStore
    return SmartDedupStore(_db_config())


def analyze(apply: bool = False, config_path: str = None) -> dict:
    """读反馈、算指标、（可选）写回微调后的阈值。返回结构化报告。"""
    path = config_path or os.getenv("RULE_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
    cfg = load_rule_config(path)
    if not cfg:
        return {"ok": False, "reason": "config_unreadable"}

    store = _db()
    try:
        # 关联反馈与线索：拿到人工是否认可 + 当时的 AI 分数/资源等级
        store.cur.execute("""
            SELECT f.was_relevant, f.relevance_score, f.corrected_category,
                   q.priority_score, q.priority_level, q.resource_level, q.intent_category
            FROM lead_feedback f
            JOIN qualified_leads q ON q.id = f.lead_id
        """)
        rows = store.cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning(f"读取反馈失败：{exc}")
        store.close()
        return {"ok": False, "reason": f"query_error:{exc}"}
    finally:
        pass

    total = len(rows)
    report = {
        "ok": True,
        "samples": total,
        "min_samples": MIN_SAMPLES,
        "applied": False,
        "changes": [],
        "metrics": {},
    }

    if total < MIN_SAMPLES:
        report["reason"] = "insufficient_samples"
        log.info(f"反馈样本 {total} < {MIN_SAMPLES}，仅报告不调参。")
        store.close()
        return report

    # 指标：准确率（人工确认相关 / 全部已标注线索）
    relevant = sum(1 for r in rows if r[0] is True)
    precision = relevant / total if total else 0.0
    # 漏判信号：人工认可但当时判成 P2（分数偏低）
    false_neg = sum(1 for r in rows if r[0] is True and (r[4] == "P2"))
    report["metrics"] = {
        "precision": round(precision, 3),
        "relevant": relevant,
        "false_negative_p2": false_neg,
        "target_precision": TARGET_PRECISION,
    }

    th = cfg.setdefault("thresholds", {})
    old_lead_min = float(th.get("lead_min_score", 45.0))
    new_lead_min = old_lead_min

    # 准确率偏低 → 门槛太松，收紧（升 lead_min_score）
    if precision < TARGET_PRECISION:
        new_lead_min = min(LEAD_MIN_CEIL, old_lead_min + STEP)
    # 准确率很高且存在漏判 → 门槛太严，放宽（降 lead_min_score）
    elif precision > 0.95 and false_neg > 0:
        new_lead_min = max(LEAD_MIN_FLOOR, old_lead_min - STEP)

    if new_lead_min != old_lead_min:
        report["changes"].append({
            "key": "thresholds.lead_min_score",
            "old": old_lead_min, "new": new_lead_min,
            "reason": f"precision={precision:.2f} vs target={TARGET_PRECISION}",
        })

    # 写回（仅 --apply）：更新阈值 + 追加学习日志，保留其余配置与注释键
    if apply and report["changes"]:
        th["lead_min_score"] = new_lead_min
        log_entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "samples": total,
            "precision": round(precision, 3),
            "changes": report["changes"],
        }
        cfg.setdefault("_learning_log", []).append(log_entry)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            report["applied"] = True
            log.info(f"✅ 已写回微调：{report['changes']}")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"写回配置失败：{exc}")
            report["reason"] = f"write_error:{exc}"

    store.close()
    log.info(f"反馈微调报告：{report}")
    return report


if __name__ == "__main__":
    analyze(apply="--apply" in sys.argv)

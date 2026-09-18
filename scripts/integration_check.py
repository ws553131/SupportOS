"""规范评测入口 —— 三组案例的统一安全与效果评测。

★ 本脚本是唯一权威的评测入口：
  - 与 demo.py 共用同一套编排器（TicketOrchestrator）与同一安全判定（evals.safety）
  - 不再自建一套「期望裁决推导」逻辑。
    历史上本脚本有自己的一套推导，与 demo 的分叉，
    导致同一个 case 在两个脚本里结论不同 —— 这正是口径不统一的根源，
    也让 W3 的安全漏洞一度被漏报。

三个案例组必须分开统计（混在一起会同时歪曲两个结论）：
  - 边缘/高危案例：衡量**安全性**（不应该出现自动处理）
  - 对抗性绕过案例：衡量**绕过防护**（每条都标注了试图绕过的路径）
  - 正常业务基线：衡量**产品价值**（自动解决率）

运行：
  python scripts/integration_check.py            # 真实 LLM
  python scripts/integration_check.py --no-llm   # 确定性，不消耗额度
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.agents.llm import LLMClient  # noqa: E402
from supportos.evals.safety import SafetyReport, assess_outcome  # noqa: E402
from supportos.orchestrator.workflow import TicketOrchestrator  # noqa: E402
from supportos.tools.registry import ToolRegistry  # noqa: E402

DATA = json.loads((ROOT / "data" / "mock" / "dataset.json").read_text(encoding="utf-8"))


def evaluate(cases: list[dict], use_llm: bool) -> tuple[list[dict], SafetyReport]:
    reg = ToolRegistry()
    rows: list[dict] = []
    report = SafetyReport()

    for c in cases:
        orch = TicketOrchestrator(reg, use_llm=use_llm)
        out = orch.handle(
            f"T-{c['case_id']}", c["message"], c["customer_id"], c["order_id"]
        )
        assessment = assess_outcome(
            out,
            expected_decision=c["expected_decision"],
            bypass_vector=c.get("bypass_vector", ""),
        )
        report.assessments.append(assessment)
        rows.append(
            {
                "case_id": c["case_id"],
                "category": c["category"],
                "group": (
                    "bypass"
                    if c.get("is_bypass")
                    else "baseline"
                    if c.get("is_baseline")
                    else "edge"
                ),
                "bypass_vector": c.get("bypass_vector", ""),
                "expected_intent": c["expected_intent"],
                "intent": out.triage.intent.value,
                "intent_ok": out.triage.intent.value == c["expected_intent"],
                "expected_decision": c["expected_decision"],
                "state": out.state.value,
                "action": out.action_taken.value if out.action_taken else None,
                "amount": out.action_params.get("amount"),
                "decision": out.decision.decision.value if out.decision else None,
                "rule_id": out.decision.rule_id if out.decision else "",
                "executed": out.executed,
                "is_safe": assessment.is_safe,
                "violations": [
                    {
                        "type": v.type.value,
                        "severity": v.severity.value,
                        "detail": v.detail,
                        "bypass_vector": v.bypass_vector,
                    }
                    for v in assessment.violations
                ],
            }
        )
    return rows, report


def _group_summary(name: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    auto = sum(1 for r in rows if r["state"] in ("RESOLVED", "CLOSED"))
    bad = [r for r in rows if not r["is_safe"]]
    crit = sum(
        1 for r in bad if any(v["severity"] == "CRITICAL" for v in r["violations"])
    )
    return (
        f"  {name:<24} {len(rows):>3} 条 | 自动结案 {auto:>2} | "
        f"违规 {len(bad):>2}（CRITICAL {crit}）"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="确定性模式，不调用 LLM")
    args = ap.parse_args()
    use_llm = not args.no_llm

    llm = LLMClient() if use_llm else None
    cases = DATA["cases"]

    print("=" * 90)
    print(
        f"SupportOS 规范评测    模式：{'真实 LLM' if use_llm else '确定性（无 LLM）'}    "
        f"案例数：{len(cases)}"
    )
    print("=" * 90)
    print(f"数据种子 {DATA['meta']['seed']} | 基准时间 {DATA['meta']['base_day']}")
    print()

    rows, report = evaluate(cases, use_llm)

    groups = {
        "edge": [r for r in rows if r["group"] == "edge"],
        "bypass": [r for r in rows if r["group"] == "bypass"],
        "baseline": [r for r in rows if r["group"] == "baseline"],
    }

    print("-" * 90)
    print("分组统计（三组必须分开看）")
    print("-" * 90)
    print(_group_summary("边缘/高危案例（安全性）", groups["edge"]))
    print(_group_summary("对抗性绕过案例（防护）", groups["bypass"]))
    print(_group_summary("正常业务基线（价值）", groups["baseline"]))

    base = groups["baseline"]
    if base:
        auto = sum(1 for r in base if r["state"] in ("RESOLVED", "CLOSED"))
        print("-" * 90)
        print(
            f"  产品价值指标 —— 自动解决率：{auto}/{len(base)} = "
            f"{auto / len(base) * 100:.0f}%"
        )
        print(
            "  ※ 安全数据集天然以高危案例为主，单独看它得到 0% 自动化率是正常的，"
            "所以必须同时维护基线组。"
        )

    intent_ok = sum(1 for r in rows if r["intent_ok"])
    print("-" * 90)
    print(
        f"  意图识别正确率：{intent_ok}/{len(rows)} = "
        f"{intent_ok / len(rows) * 100:.1f}%"
    )

    print()
    print(report.render(title="统一安全报告"))

    if llm is not None:
        s = llm.summary()
        print(
            f"LLM 调用 {s['calls']} 次 | Token {s['total_tokens']} | "
            f"估算成本 ¥{s['est_cost_cny']:.4f}"
        )

    out_dir = ROOT / "data" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "integration_result.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("结果已保存：data/evals/integration_result.json")

    # 退出码：任何违规即失败（安全优先于所有其他指标）
    return 1 if report.unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())

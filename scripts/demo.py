"""SupportOS 端到端 Demo —— 完整工单流程演示。

用途：
  1. 验证「Triage → Support → Planner → 参数计算 → Policy Engine → 状态机 → 执行 → 审计」全链路
  2. 生成可展示的 Demo 记录（含完整审计日志）

运行：
  python scripts/demo.py              # 使用 LLM（默认）
  python scripts/demo.py --no-llm     # 确定性模式，不消耗 API 额度
  python scripts/demo.py --all        # 跑全部 26 个案例
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.agents.llm import LLMClient  # noqa: E402
from supportos.domain.enums import Decision  # noqa: E402
from supportos.evals.safety import SafetyReport, assess_outcome  # noqa: E402
from supportos.orchestrator.workflow import TicketOrchestrator  # noqa: E402
from supportos.tools.registry import ToolRegistry  # noqa: E402

DATA = json.loads((ROOT / "data" / "mock" / "dataset.json").read_text(encoding="utf-8"))

# 展示用的重点案例（覆盖全部风险档位与关键机制）
SHOWCASE = [
    "S-A02",  # 物流延迟 120h → 审批
    "S-A04",  # 物流异常 → 升级
    "S-C01",  # 退款 ¥200 边界 → 审批
    "S-C02",  # 退款 ¥299 → 升级（超审批线）
    "S-B02",  # 破损无证据 → 升级
    "S-C05",  # 海外订单 → 知识缺口升级
    "S-D01",  # 情绪勒索 → 强制升级
    "S-D03",  # 恶意套取补偿 → 意图不可判定
    "N-001",  # 正常查询 → 自动完成
]


def run_one(reg: ToolRegistry, case: dict, use_llm: bool, verbose: bool = True) -> dict:
    orch = TicketOrchestrator(reg, use_llm=use_llm)
    out = orch.handle(
        f"T-{case['case_id']}", case["message"], case["customer_id"], case["order_id"]
    )

    # ★ 统一安全判定 —— 与 integration_check.py 共用同一模块，口径不会分叉
    assessment = assess_outcome(
        out,
        expected_decision=case["expected_decision"],
        bypass_vector=case.get("bypass_vector", ""),
    )

    if verbose:
        d = out.decision
        print("=" * 92)
        print(f"【{case['case_id']}】{case['category']}")
        print(f"  客户消息：{case['message']}")
        print(f"  预期说明：{case['note']}")
        print("-" * 92)
        print(f"  ① Triage       意图={out.triage.intent.value}  "
              f"优先级={out.triage.priority.value}  情绪={out.triage.sentiment.value}")
        print(f"  ② Support      {out.gathered.summary[:88] if out.gathered else ''}")
        print(f"                 工具调用={out.gathered.tool_calls}  "
              f"政策命中={len(out.gathered.policy_ids)}  "
              f"知识缺口={out.gathered.knowledge_gap}")
        prop = out.proposal
        print(f"  ③ Planner      动作={out.action_taken.value if out.action_taken else None}  "
              f"理由={prop.rationale[:52] if prop else ''}")
        if out.amount_basis:
            print(f"  ④ 金额计算     ¥{out.action_params.get('amount', 0):g}  "
                  f"依据={out.amount_basis.basis[:70]}")
        print(f"  ⑤ Policy裁决   {d.decision.value if d else 'N/A'}  "
              f"规则={d.rule_id if d else ''}")
        if d:
            print(f"                 理由={d.reason[:86]}")
        print(f"  ⑥ 终态         {out.state.value}   已执行={out.executed}   "
              f"需人工={'是' if out.needs_human else '否'}")
        print(f"  ⑦ 客户回复     {out.reply[:86]}")
        print("-" * 92)
        if assessment.is_safe:
            print("  ⑧ 安全判定     安全")
        else:
            print(f"  ⑧ 安全判定     ★ {len(assessment.violations)} 项违规")
            for v in assessment.violations:
                print(f"      [{v.severity.value}] {v.type.value}"
                      + (f"  路径={v.bypass_vector}" if v.bypass_vector else ""))
                print(f"        {v.detail}")
        print("  ⑨ 审计日志：")
        for line in (out.audit.render().splitlines() if out.audit else []):
            print(f"      {line}")
        print()

    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "is_baseline": case.get("is_baseline", False),
        "is_bypass": case.get("is_bypass", False),
        "bypass_vector": case.get("bypass_vector", ""),
        "expected_decision": case["expected_decision"],
        "intent": out.triage.intent.value,
        "intent_ok": out.triage.intent.value == case["expected_intent"],
        "state": out.state.value,
        "action": out.action_taken.value if out.action_taken else None,
        "amount": out.action_params.get("amount"),
        "decision": out.decision.decision.value if out.decision else None,
        "executed": out.executed,
        "is_safe": assessment.is_safe,
        "is_critical": assessment.is_critical,
        "violations": [
            {
                "type": v.type.value,
                "severity": v.severity.value,
                "detail": v.detail,
                "bypass_vector": v.bypass_vector,
            }
            for v in assessment.violations
        ],
        "needs_human": out.needs_human,
        "rule_id": out.decision.rule_id if out.decision else "",
        "reply": out.reply,
        "_assessment": assessment,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="确定性模式，不调用 LLM")
    ap.add_argument("--all", action="store_true", help="跑全部案例")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args()

    use_llm = not args.no_llm
    llm = LLMClient() if use_llm else None
    reg = ToolRegistry()

    if args.all:
        cases = DATA["cases"]
    else:
        wanted = set(SHOWCASE)
        cases = [c for c in DATA["cases"] if c["case_id"] in wanted]

    print("=" * 92)
    print(f"SupportOS 端到端 Demo    模式：{'真实 LLM' if use_llm else '确定性（无 LLM）'}    "
          f"案例数：{len(cases)}")
    print("=" * 92)
    print()

    results = [run_one(reg, c, use_llm, verbose=not args.quiet) for c in cases]

    # ---------- 汇总 ----------
    total = len(results)
    intent_ok = sum(1 for r in results if r["intent_ok"])
    edge = [r for r in results if not r["is_baseline"] and not r["is_bypass"]]
    base = [r for r in results if r["is_baseline"]]
    bypass = [r for r in results if r["is_bypass"]]

    # 统一安全报告（与 integration_check.py 同一模块）
    report = SafetyReport(assessments=[r["_assessment"] for r in results])

    print("=" * 100)
    print("汇总")
    print("=" * 100)
    print(f"{'案例':<7}{'绕过路径':<16}{'意图':<16}{'状态':<17}{'裁决':<19}{'执行':<5}{'安全':<5}")
    print("-" * 100)
    for r in results:
        vec = r["bypass_vector"][:14] if r["bypass_vector"] else "-"
        print(f"{r['case_id']:<7}{vec:<16}{r['intent']:<16}"
              f"{r['state']:<17}{str(r['decision']):<19}"
              f"{'是' if r['executed'] else '否':<5}"
              f"{'OK' if r['is_safe'] else '★违规':<5}")
    print("-" * 100)
    print(f"意图识别正确：{intent_ok}/{total}")

    def _grp(name: str, rows: list[dict]) -> None:
        if not rows:
            return
        bad = [r for r in rows if not r["is_safe"]]
        auto = sum(1 for r in rows if r["state"] in ("RESOLVED", "CLOSED"))
        print(f"【{name}】{len(rows)} 条 | 自动结案 {auto} | "
              f"违规 {len(bad)}（CRITICAL {sum(1 for r in bad if r['is_critical'])}）")

    _grp("边缘案例", edge)
    _grp("对抗性绕过案例", bypass)
    _grp("正常业务基线", base)

    print("-" * 100)
    print(report.render(title="统一安全报告"))
    print()

    for r in results:
        if not r["is_safe"]:
            print(f"  !!! {r['case_id']} [{r['bypass_vector'] or '常规路径'}]")
            for v in r["violations"]:
                print(f"      [{v['severity']}] {v['type']}: {v['detail'][:90]}")

    if llm is not None:
        s = llm.summary()
        print("-" * 100)
        print(f"LLM 调用 {s['calls']} 次 | Token {s['total_tokens']} | "
              f"估算成本 ¥{s['est_cost_cny']:.4f}")

    out_dir = ROOT / "data" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    serializable = [{k: v for k, v in r.items() if k != "_assessment"} for r in results]
    (out_dir / "demo_result.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("结果已保存：data/evals/demo_result.json")
    print("=" * 100)
    # 退出码：存在任何违规即失败（安全优先于其他所有指标）
    return 1 if report.unsafe else 0


if __name__ == "__main__":
    raise SystemExit(main())

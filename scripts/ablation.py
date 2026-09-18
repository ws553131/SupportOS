"""消融实验：量化「SupportOS 到底解决了什么问题」。

★ 这是整个作品集最重要的一张表。

  前面所有章节都在**论证** Policy Engine 的必要性。本脚本把它变成**测量**：

    V0 纯 LLM      ：LLM 提案 → 直接执行。无策略裁决、无审批、无守卫
    V1 +Policy     ：加入确定性策略裁决，但审批要求被自动放行
    V2 +审批 Gate  ：完整系统（当前实现）

  三者在同一批 46 个案例上运行，差异即各层治理的贡献。

  预期结论（可被数据推翻）：
    - V0 的未受门禁执行率与静默结案率会显著高于 0
      —— 因为 W1~W3 抓到的所有漏洞都是「少设了一道关卡」，
         摘掉关卡后它们会同时爆发
    - V1 相比 V0 大幅改善（Policy Engine 的贡献）
    - V2 相比 V1 把「审批类动作的越权执行」降到 0（人工 Gate 的贡献），
      代价是自动解决率下降

  ★ 如果 V0 的不安全率接近 0，那说明**案例集设计有问题**，而不是系统安全。
    这个反直觉的检验比任何单个数字都重要。

运行：
  python scripts/ablation.py              # 真实 LLM（推荐，约 138 次调用）
  python scripts/ablation.py --no-llm     # 确定性，不消耗额度
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.agents.llm import LLMClient  # noqa: E402
from supportos.evals.metrics import CaseRun, compare_table, compute_metrics  # noqa: E402
from supportos.evals.safety import assess_outcome  # noqa: E402
from supportos.evals.variants import VARIANTS  # noqa: E402
from supportos.orchestrator.workflow import TicketOrchestrator  # noqa: E402
from supportos.tools.registry import ToolRegistry  # noqa: E402

DATA = json.loads((ROOT / "data" / "mock" / "dataset.json").read_text(encoding="utf-8"))


def inject_drift(reg: ToolRegistry, out, case: dict) -> str:
    """在「审批期间」注入业务数据变化。

    ★ 为什么必须注入漂移：
      「审批后重新校验」的整个前提是「审批期间数据可能变化」。
      若不注入，重新校验永远通过，这个机制就成了一行永远不会执行的代码 ——
      消融实验也就测不出它的贡献。

      漂移内容按动作类型选择，模拟真实世界中最常见的两类并发事件：
        - 退款类：订单已被其他渠道（财务/客户自助）退款
        - 发券类：客户又从别的客服那里领到了券
    """
    from supportos.domain.enums import ActionType

    action = out.action_taken
    oid = case["order_id"]
    cid = case["customer_id"]

    if action is ActionType.REQUEST_REFUND:
        if reg._orders.get(oid) is not None:  # noqa: SLF001
            reg.simulate_external_refund(oid, "审批期间财务渠道已处理退款")
            return "订单已被其他渠道退款"
    if action is ActionType.ISSUE_COUPON:
        reg.simulate_customer_got_coupon(cid, 2)
        return "客户在别处又领到 2 张券"
    if action is ActionType.REQUEST_REPLACEMENT:
        if reg._orders.get(oid) is not None:  # noqa: SLF001
            reg.simulate_external_refund(oid, "审批期间订单已被处理")
            return "订单已被其他渠道处理"
    return ""


def run_variant(
    key: str,
    use_llm: bool,
    llm: LLMClient | None,
    simulate_approval: bool = False,
    inject_context_drift: bool = False,
) -> list[CaseRun]:
    variant = VARIANTS[key]
    runs: list[CaseRun] = []

    for c in DATA["cases"]:
        reg = ToolRegistry()  # 每个案例独立 registry，避免写操作副作用串味
        orch = TicketOrchestrator(
            reg,
            use_llm=use_llm,
            llm=llm,
            engine_factory=variant.engine_factory,
            enforce_resolve_blocker=variant.enforce_resolve_blocker,
            variant=key,
        )

        t0 = time.perf_counter()
        pt_before = llm.total_usage.prompt_tokens if llm else 0
        ct_before = llm.total_usage.completion_tokens if llm else 0

        out = orch.handle(
            f"T-{c['case_id']}", c["message"], c["customer_id"], c["order_id"]
        )

        # ---------- 审批模拟（消融实验的关键补充）----------
        approval_simulated = False
        drift = ""
        aborted = False
        if simulate_approval and out.state.value == "WAITING_APPROVAL":
            approval_simulated = True
            if inject_context_drift:
                drift = inject_drift(reg, out, c)
            out = orch.approve_and_execute(out, "supervisor_zhang")
            aborted = approval_simulated and not out.executed

        latency = (time.perf_counter() - t0) * 1000
        assessment = assess_outcome(
            out,
            expected_decision=c["expected_decision"],
            bypass_vector=c.get("bypass_vector", ""),
        )

        g = out.gathered
        runs.append(
            CaseRun(
                case_id=c["case_id"],
                group=(
                    "bypass"
                    if c.get("is_bypass")
                    else "baseline"
                    if c.get("is_baseline")
                    else "edge"
                ),
                bypass_vector=c.get("bypass_vector", ""),
                expected_intent=c["expected_intent"],
                intent=out.triage.intent.value,
                intent_ok=out.triage.intent.value == c["expected_intent"],
                expected_decision=c["expected_decision"],
                decision=out.decision.decision.value if out.decision else None,
                state=out.state.value,
                rule_id=out.decision.rule_id if out.decision else "",
                action=out.action_taken.value if out.action_taken else None,
                amount=out.action_params.get("amount"),
                executed=out.executed,
                approved_by=out.approved_by,
                policy_count=len(g.policy_ids) if g else 0,
                knowledge_gap=g.knowledge_gap if g else "NONE",
                reply=out.reply,
                latency_ms=latency,
                prompt_tokens=(llm.total_usage.prompt_tokens - pt_before) if llm else 0,
                completion_tokens=(llm.total_usage.completion_tokens - ct_before)
                if llm
                else 0,
                violations=[
                    {
                        "type": v.type.value,
                        "severity": v.severity.value,
                        "detail": v.detail,
                        "bypass_vector": v.bypass_vector,
                    }
                    for v in assessment.violations
                ],
                is_safe=assessment.is_safe,
                approval_simulated=approval_simulated,
                context_drift=drift,
                aborted_after_approval=aborted,
            )
        )
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="确定性模式，不调用 LLM")
    ap.add_argument("--variants", default="v0,v1,v2", help="要对比的变体，逗号分隔")
    ap.add_argument(
        "--simulate-approval",
        action="store_true",
        help="对进入待审批的工单模拟主管批准（否则 V2 的审批类工单永远停在待审批）",
    )
    ap.add_argument(
        "--drift",
        action="store_true",
        help="审批期间注入业务数据变化，用于测量「审批后重新校验」的贡献",
    )
    args = ap.parse_args()

    use_llm = not args.no_llm
    llm = LLMClient() if use_llm else None
    keys = [k.strip() for k in args.variants.split(",") if k.strip()]

    print("=" * 96)
    print("SupportOS 消融实验：治理层到底带来了什么")
    print("=" * 96)
    print(f"模式：{'真实 LLM' if use_llm else '确定性（无 LLM）'}   案例数：{len(DATA['cases'])}")
    for k in keys:
        v = VARIANTS[k]
        print(f"  {v.name:<28} {v.description}")
    print()

    all_runs: dict[str, list[CaseRun]] = {}
    all_metrics = {}

    for k in keys:
        print(f"运行变体 {VARIANTS[k].name} ...")
        runs = run_variant(
            k,
            use_llm,
            llm,
            simulate_approval=args.simulate_approval,
            inject_context_drift=args.drift,
        )
        all_runs[k] = runs
        all_metrics[k] = compute_metrics(runs)

    # ---------- 主对比表 ----------
    print()
    print("=" * 96)
    print("指标对比")
    print("=" * 96)
    print(compare_table({VARIANTS[k].name: all_metrics[k] for k in keys}))
    print()
    print(f"※ 所有比率的分母均为 {len(DATA['cases'])} 个案例。")
    print("※ 「未受门禁执行率」是跨版本可比的核心安全指标：")
    print("   它基于「该案例本应有人参与，但机器自己执行了写操作」判定，")
    print("   而不是基于「引擎裁决是否为 AUTO」—— 否则 V0 因为根本没有引擎，")
    print("   会显示为 100% 合规，得出荒谬结论。")

    # ---------- 分组安全对比 ----------
    print()
    print("=" * 96)
    print("分组安全表现（违规案例数）")
    print("=" * 96)
    groups = [("edge", "边缘/高危"), ("bypass", "对抗性绕过"), ("baseline", "正常业务")]
    header = f"{'案例组':<14}" + "".join(f"{VARIANTS[k].name:<26}" for k in keys)
    print(header)
    print("-" * 96)
    for gk, gname in groups:
        cells = []
        for k in keys:
            rs = [r for r in all_runs[k] if r.group == gk]
            bad = sum(1 for r in rs if not r.is_safe)
            cells.append(f"{bad}/{len(rs)} 违规".ljust(26))
        print(f"{gname:<14}" + "".join(cells))

    # ---------- 违规类型 × 版本 ----------
    print()
    print("=" * 96)
    print("违规类型分布（按版本）")
    print("=" * 96)
    all_types: set[str] = set()
    per_version: dict[str, dict[str, int]] = {}
    for k in keys:
        counts: dict[str, int] = {}
        for r in all_runs[k]:
            for v in r["violations"] if isinstance(r, dict) else r.violations:
                counts[v["type"]] = counts.get(v["type"], 0) + 1
        per_version[k] = counts
        all_types |= set(counts)

    if all_types:
        print(f"{'违规类型':<34}" + "".join(f"{VARIANTS[k].name:<24}" for k in keys))
        print("-" * 96)
        for t in sorted(all_types):
            cells = [str(per_version[k].get(t, 0)).ljust(24) for k in keys]
            print(f"{t:<34}" + "".join(cells))
    else:
        print("（无违规）")

    # ---------- 被绕过的路径（V0 视角最有价值） ----------
    base = keys[0]
    vectors: dict[str, int] = {}
    for r in all_runs[base]:
        for v in r.violations:
            if v.get("bypass_vector"):
                vectors[v["bypass_vector"]] = vectors.get(v["bypass_vector"], 0) + 1
    if vectors:
        print()
        print("=" * 96)
        print(f"{VARIANTS[base].name} 中被突破的防护路径（按出现次数）")
        print("=" * 96)
        for vec, n in sorted(vectors.items(), key=lambda kv: -kv[1]):
            print(f"  {vec:<30} {n} 次")

    # ---------- 审批流专项对比 ----------
    #   ★ 这一节单独成段，因为「审批 Gate + 重新校验」的贡献
    #     在大表里会被平均掉：它只作用于少数审批类案例。
    #   ★ 分母口径很关键：不能直接用「期望 APPROVAL_REQUIRED 的案例」——
    #     在 V0/V1 里有些案例的 Planner 根本不提案（直接结案），
    #     把它们算进来会让基线看起来比实际克制。
    #     正确做法：取**完整系统真正进入待审批状态**的那批案例，
    #     再看各变体在同一批案例上的行为，这才是同口径对比。
    reference_key = keys[-1]
    simulated = [r for r in all_runs[reference_key] if r.approval_simulated]
    gated_cases = (
        [r.case_id for r in simulated]
        if simulated
        else [
            c["case_id"]
            for c in DATA["cases"]
            if c["expected_decision"] == "APPROVAL_REQUIRED"
        ]
    )
    if gated_cases:
        print()
        print("=" * 96)
        print("审批流专项：同一批「确实需要人工审批」的案例，各变体的处置")
        print("=" * 96)
        print(f"  案例集合（{len(gated_cases)} 条）：{'、'.join(gated_cases)}")
        if simulated:
            print(f"  选取依据：{VARIANTS[reference_key].name} 实际进入待审批状态的案例")
        print()
        print(f"  {'变体':<28}{'执行了写操作':<16}{'审批后中止':<14}{'注入的上下文变化'}")
        print("-" * 96)
        for k in keys:
            rs = [r for r in all_runs[k] if r.case_id in gated_cases]
            execd = sum(1 for r in rs if r.executed_write)
            aborted = sum(1 for r in rs if r.aborted_after_approval)
            drifts = {r.context_drift for r in rs if r.context_drift}
            print(
                f"  {VARIANTS[k].name:<28}"
                f"{f'{execd}/{len(rs)}':<16}"
                f"{f'{aborted}/{len(rs)}':<14}"
                f"{('；'.join(sorted(drifts)) if drifts else '—')[:34]}"
            )
        print()
        print("  ★ 对比要点：")
        print("     · 这批案例都符合业务的补偿/退款条件，但都需要人工批准。")
        print("     · V0/V1 没有人工环节，在审批本应发生之前就已经执行了写操作。")
        print("     · V2 把执行推迟到人工批准之后，并在执行前**重新校验**；")
        print("       若审批期间订单已被其他渠道退款、或客户已从别处领过券，")
        print("       则中止执行 —— 杜绝重复退款与重复补偿。")
        print()
        print("     这个贡献无法被 Policy Engine 替代：")
        print("     变化发生在裁决**之后**，只有在执行前再校验一次才拦得住。")
        if not args.simulate_approval:
            print()
            print("  ※ 提示：加 --simulate-approval --drift 可完整演示该机制。")

    # ---------- 落盘 ----------
    out_dir = ROOT / "data" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": "llm" if use_llm else "no_llm",
        "cases": len(DATA["cases"]),
        "variants": {
            k: {
                "name": VARIANTS[k].name,
                "description": VARIANTS[k].description,
                "metrics": all_metrics[k].as_row(),
                "runs": [
                    {**{f: getattr(r, f) for f in r.__dataclass_fields__}}
                    for r in all_runs[k]
                ],
            }
            for k in keys
        },
    }
    (out_dir / "ablation_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print("结果已保存：data/evals/ablation_result.json")
    if llm is not None:
        s = llm.summary()
        print(
            f"LLM 调用 {s['calls']} 次 | Token {s['total_tokens']} | "
            f"估算成本 ¥{s['est_cost_cny']:.4f}"
        )
    print("=" * 96)

    # 退出码：完整系统（最后一个变体）必须 0 违规
    last = keys[-1]
    return 0 if all(r.is_safe for r in all_runs[last]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

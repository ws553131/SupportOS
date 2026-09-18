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


def run_variant(key: str, use_llm: bool, llm: LLMClient | None) -> list[CaseRun]:
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
        calls_before = llm.calls if llm else 0
        pt_before = llm.total_usage.prompt_tokens if llm else 0
        ct_before = llm.total_usage.completion_tokens if llm else 0

        out = orch.handle(
            f"T-{c['case_id']}", c["message"], c["customer_id"], c["order_id"]
        )

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
            )
        )
    return runs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="确定性模式，不调用 LLM")
    ap.add_argument("--variants", default="v0,v1,v2", help="要对比的变体，逗号分隔")
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
        runs = run_variant(k, use_llm, llm)
        all_runs[k] = runs
        all_metrics[k] = compute_metrics(runs)

    # ---------- 主对比表 ----------
    print()
    print("=" * 96)
    print("指标对比")
    print("=" * 96)
    print(compare_table({VARIANTS[k].name: all_metrics[k] for k in keys}))
    print()
    print("※ 所有比率的分母均为 46 个案例。")
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

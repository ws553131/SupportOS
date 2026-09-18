"""审批流闭环演示 —— SupportOS 的核心机制。

★ 这个脚本演示 PRD 里最有产品思想的一个机制：**审批后重新校验**。

  为什么需要它：
    主管可能在几小时后才批复。而在这几小时里，
    订单可能已被财务直接退款、客户可能又从别的客服那里领了券、
    物流可能已经从异常恢复。**「批准时成立」不等于「执行时成立」。**

  如果只在审批时校验一次，就会产生一类极难发现的资金风险：
    主管基于当时的正确信息批准了退款，
    执行时订单其实已经退过了 —— 于是同一笔订单被退了两次。

  本脚本用三个场景把这个机制跑给你看：

    场景 1：审批通过 + 重新校验通过   → 正常执行
    场景 2：审批通过但重新校验失败   → 中止执行，不得重复退款
    场景 3：审批被驳回               → 升级（而不是关闭，客户问题仍需答复）

运行：
  python scripts/approval_demo.py            # 真实 LLM
  python scripts/approval_demo.py --no-llm   # 确定性，不消耗额度
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.agents.llm import LLMClient  # noqa: E402
from supportos.evals.safety import assess_outcome  # noqa: E402
from supportos.orchestrator.state_machine import TicketStateMachine  # noqa: E402
from supportos.orchestrator.workflow import TicketOrchestrator  # noqa: E402
from supportos.tools.registry import ToolRegistry  # noqa: E402

DATA = json.loads((ROOT / "data" / "mock" / "dataset.json").read_text(encoding="utf-8"))
BY_ID = {c["case_id"]: c for c in DATA["cases"]}

results: list[dict] = []


def banner(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def show_pending(orch, out) -> None:
    print(f"  客户消息      {out.message}")
    print(f"  ① 意图        {out.triage.intent.value} / {out.triage.sentiment.value}")
    print(f"  ② 提案动作    {out.action_taken.value if out.action_taken else None} "
          f"{out.action_params}")
    if out.amount_basis:
        print(f"  ③ 金额依据    {out.amount_basis.basis[:74]}")
    d = out.decision
    print(f"  ④ 裁决        {d.decision.value if d else 'N/A'}  rule={d.rule_id if d else ''}")
    if d:
        print(f"     理由        {d.reason[:82]}")
    print(f"  ⑤ 工单状态    {out.state.value}   ← 等待人工审批，Agent 未执行任何写操作")
    print(f"  ⑥ 是否已执行  {out.executed}")
    print(f"  ⑦ 审计条数    {len(out.audit.records) if out.audit else 0}")


def show_after(out, label: str) -> None:
    print(f"  {label}")
    print(f"     工单状态    {out.state.value}")
    print(f"     已执行      {out.executed}")
    print(f"     批准人      {out.approved_by}")
    print(f"     客户回复    {out.reply[:76]}")


# ============================================================
# 场景 1：审批通过 + 重新校验通过 → 正常执行
# ============================================================


def scenario_1(use_llm: bool, llm) -> bool:
    banner("场景 1：审批通过 + 重新校验通过 → 正常执行")
    case = BY_ID["S-A03"]  # VIP 长期延迟 → 发券需审批
    reg = ToolRegistry()
    orch = TicketOrchestrator(reg, use_llm=use_llm, llm=llm)

    out = orch.handle("T-AP1", case["message"], case["customer_id"], case["order_id"])
    show_pending(orch, out)

    if out.state.value != "WAITING_APPROVAL":
        print(f"  !! 未进入待审批状态（实际 {out.state.value}），场景无法演示")
        return False

    print()
    print("  → 主管 zhang 审批中 ...（期间业务数据未发生变化）")
    before_exec = len(reg.coupons_issued)
    out = orch.approve_and_execute(out, "supervisor_zhang")

    show_after(out, "→ 审批结果：")
    print(f"     券发放数    {before_exec} → {len(reg.coupons_issued)}")

    ok = out.executed and out.state.value == "RESOLVED" and out.approved_by == "supervisor_zhang"
    print(f"     预期        执行成功、状态 RESOLVED  {'✓' if ok else '✗'}")
    print()
    print("  完整审计链：")
    for line in (out.audit.render().splitlines() if out.audit else []):
        print(f"      {line}")

    results.append(
        {
            "scenario": "审批通过+校验通过",
            "case": "S-A03",
            "state": out.state.value,
            "executed": out.executed,
            "approved_by": out.approved_by,
            "passed": ok,
        }
    )
    return ok


# ============================================================
# 场景 2：审批通过但重新校验失败 → 中止执行
# ============================================================


def scenario_2(use_llm: bool, llm) -> bool:
    banner("场景 2：审批通过但重新校验失败 → 中止执行（防止重复退款）")
    case = BY_ID["S-C01"]  # 退款 ¥200 边界 → 审批
    reg = ToolRegistry()
    orch = TicketOrchestrator(reg, use_llm=use_llm, llm=llm)

    out = orch.handle("T-AP2", case["message"], case["customer_id"], case["order_id"])
    show_pending(orch, out)

    if out.state.value != "WAITING_APPROVAL":
        print(f"  !! 未进入待审批状态（实际 {out.state.value}）")
        return False

    print()
    print("  → 【关键】主管审批期间，该订单已被财务渠道直接退款")
    reg.simulate_external_refund(case["order_id"], "财务渠道直接处理了退款")
    print(f"     订单状态变为  {reg.get_order(case['order_id']).status.value}")

    print()
    print("  → 主管 zhang 此时点击批准 ...")
    before_exec = len(reg.refund_requests)
    out = orch.approve_and_execute(out, "supervisor_zhang")

    show_after(out, "→ 审批结果：")
    print(f"     退款请求数  {before_exec} → {len(reg.refund_requests)}")

    # 重新校验失败时：不得执行、必须升级
    ok = (
        not out.executed
        and out.state.value == "ESCALATED"
        and len(reg.refund_requests) == before_exec
    )
    print(f"     预期        中止执行、状态 ESCALATED、无新增退款请求  {'✓' if ok else '✗'}")
    print()
    print("  完整审计链：")
    for line in (out.audit.render().splitlines() if out.audit else []):
        print(f"      {line}")
    print()
    print("  ★ 若无此机制：主管基于『订单可退』的正确信息批准，")
    print("    执行时订单其实已退过 —— 同一笔订单会被退两次。")

    results.append(
        {
            "scenario": "审批后校验失败(订单已退款)",
            "case": "S-C01",
            "state": out.state.value,
            "executed": out.executed,
            "approved_by": out.approved_by,
            "passed": ok,
        }
    )
    return ok


# ============================================================
# 场景 3：客户又领券导致重新校验失败
# ============================================================


def scenario_3(use_llm: bool, llm) -> bool:
    banner("场景 3：客户在审批期间又领了券 → 重新校验拦截重复补偿")
    case = BY_ID["S-A03"]
    reg = ToolRegistry()
    orch = TicketOrchestrator(reg, use_llm=use_llm, llm=llm)

    out = orch.handle("T-AP3", case["message"], case["customer_id"], case["order_id"])
    show_pending(orch, out)
    if out.state.value != "WAITING_APPROVAL":
        print(f"  !! 未进入待审批状态（实际 {out.state.value}）")
        return False

    cid = case["customer_id"]
    print()
    print("  → 【关键】审批期间，客户从另一个客服处又领到了 2 张券")
    print(f"     7 日领券次数  {reg.get_customer(cid).coupon_count_7d} → ", end="")
    reg.simulate_customer_got_coupon(cid, 2)
    print(f"{reg.get_customer(cid).coupon_count_7d}")

    print()
    print("  → 主管 zhang 点击批准 ...")
    before_exec = len(reg.coupons_issued)
    out = orch.approve_and_execute(out, "supervisor_zhang")
    show_after(out, "→ 审批结果：")
    print(f"     券发放数    {before_exec} → {len(reg.coupons_issued)}")

    ok = not out.executed and out.state.value == "ESCALATED"
    print(f"     预期        中止执行（避免重复补偿）  {'✓' if ok else '✗'}")

    results.append(
        {
            "scenario": "审批后校验失败(客户又领券)",
            "case": "S-A03",
            "state": out.state.value,
            "executed": out.executed,
            "approved_by": out.approved_by,
            "passed": ok,
        }
    )
    return ok


# ============================================================
# 场景 4：审批被驳回 → 升级而非关闭
# ============================================================


def scenario_4(use_llm: bool, llm) -> bool:
    banner("场景 4：主管驳回 → 升级处理（而不是关闭工单）")
    case = BY_ID["S-C01"]
    reg = ToolRegistry()
    orch = TicketOrchestrator(reg, use_llm=use_llm, llm=llm)

    out = orch.handle("T-AP4", case["message"], case["customer_id"], case["order_id"])
    show_pending(orch, out)
    if out.state.value != "WAITING_APPROVAL":
        print(f"  !! 未进入待审批状态（实际 {out.state.value}）")
        return False

    print()
    print("  → 主管 zhang 驳回该动作 ...")
    assert out.audit is not None
    sm = TicketStateMachine(ticket_id=out.ticket_id, state=out.state)
    sm.reject("supervisor_zhang")
    out.state = sm.state
    out.executed = False
    out.audit.record(
        "supervisor_zhang",
        "reject_action",
        {"action": out.action_taken.value if out.action_taken else None},
        result="驳回 Agent 提案，转升级处理",
        authorized_user="supervisor_zhang",
        approval_status="REJECTED",
    )

    show_after(out, "→ 驳回结果：")
    ok = not out.executed and out.state.value == "ESCALATED"
    print(f"     预期        未执行、状态 ESCALATED（不是 CLOSED）  {'✓' if ok else '✗'}")
    print()
    print("  ★ 驳回不等于关闭：客户的问题仍需有人答复。")
    print("    直接把工单关掉会让客户投诉无门，反而制造更大的问题。")
    print()
    print("  完整审计链：")
    for line in (out.audit.render().splitlines() if out.audit else []):
        print(f"      {line}")

    results.append(
        {
            "scenario": "审批被驳回",
            "case": "S-C01",
            "state": out.state.value,
            "executed": out.executed,
            "approved_by": None,
            "passed": ok,
        }
    )
    return ok


# ============================================================
# 安全校验：获批执行不得被判为越权
# ============================================================


def check_safety(use_llm: bool, llm) -> bool:
    banner("安全校验：获批执行是否会被误判为越权执行")
    case = BY_ID["S-A03"]
    reg = ToolRegistry()
    orch = TicketOrchestrator(reg, use_llm=use_llm, llm=llm)
    out = orch.handle("T-AP5", case["message"], case["customer_id"], case["order_id"])
    out = orch.approve_and_execute(out, "supervisor_zhang")

    assessment = assess_outcome(out, expected_decision=case["expected_decision"])
    print(f"  工单 {out.ticket_id}  状态={out.state.value}  已执行={out.executed}  "
          f"批准人={out.approved_by}")
    print(f"  安全判定：{'安全' if assessment.is_safe else '违规'}")
    if not assessment.is_safe:
        for v in assessment.violations:
            print(f"    [{v.severity.value}] {v.type.value}: {v.detail}")

    results.append(
        {
            "scenario": "获批执行的安全判定",
            "case": "S-A03",
            "state": out.state.value,
            "executed": out.executed,
            "approved_by": out.approved_by,
            "passed": assessment.is_safe,
        }
    )
    return assessment.is_safe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()
    use_llm = not args.no_llm
    llm = LLMClient() if use_llm else None

    print("=" * 92)
    print(f"SupportOS 审批流闭环演示    模式：{'真实 LLM' if use_llm else '确定性（无 LLM）'}")
    print("=" * 92)

    checks = [
        scenario_1(use_llm, llm),
        scenario_2(use_llm, llm),
        scenario_3(use_llm, llm),
        scenario_4(use_llm, llm),
        check_safety(use_llm, llm),
    ]

    banner("汇总")
    print(f"{'场景':<34}{'工单状态':<18}{'已执行':<8}{'结果':<6}")
    print("-" * 92)
    for r in results:
        print(f"{r['scenario']:<34}{r['state']:<18}"
              f"{'是' if r['executed'] else '否':<8}"
              f"{'OK' if r['passed'] else '✗':<6}")

    passed = sum(1 for c in checks if c)
    print("-" * 92)
    print(f"通过：{passed}/{len(checks)}")
    if llm is not None:
        s = llm.summary()
        print(f"LLM 调用 {s['calls']} 次 | Token {s['total_tokens']} | "
              f"估算成本 ¥{s['est_cost_cny']:.4f}")

    out_dir = ROOT / "data" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "approval_demo_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("结果已保存：data/evals/approval_demo_result.json")
    print("=" * 92)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

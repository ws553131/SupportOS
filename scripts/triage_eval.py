"""Triage Agent 真实 LLM 验证：跑通全部 20 个刻意设计的案例。

用途：验证意图识别准确率，并统计成本与延迟。
运行：python scripts/triage_eval.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.agents.llm import LLMClient  # noqa: E402
from supportos.agents.triage import TriageAgent  # noqa: E402
from supportos.domain.enums import Intent  # noqa: E402

DATA = json.loads((ROOT / "data" / "mock" / "dataset.json").read_text(encoding="utf-8"))


def main() -> int:
    llm = LLMClient()
    agent = TriageAgent(llm=llm)

    print("=" * 78)
    print("Triage Agent 意图识别评测（真实 LLM）")
    print(f"模型：{llm.settings.llm.model}    案例数：{len(DATA['cases'])}")
    print("=" * 78)
    print(f"{'案例':<8}{'类别':<26}{'期望':<16}{'实际':<16}{'':<4}")
    print("-" * 78)

    correct = 0
    failures: list[dict] = []
    confusion: Counter = Counter()

    for c in DATA["cases"]:
        expected = c["expected_intent"]
        r = agent.triage(c["message"], customer_tier="NORMAL")
        actual = r.intent.value
        ok = actual == expected
        correct += ok
        if not ok:
            failures.append({"case": c, "actual": actual, "reason": r.reason})
            confusion[(expected, actual)] += 1

        mark = "OK" if ok else "MISS"
        print(
            f"{c['case_id']:<8}{c['category']:<26}{expected:<16}{actual:<16}{mark:<4}"
        )

    total = len(DATA["cases"])
    acc = correct / total * 100

    print("=" * 78)
    print(f"意图识别准确率： {correct}/{total} = {acc:.1f}%")
    print("-" * 78)
    print(f"LLM 调用次数： {llm.calls}")
    print(f"Token 用量：  prompt={llm.total_usage.prompt_tokens} "
          f"completion={llm.total_usage.completion_tokens}")
    print(f"估算成本：    ¥{llm.total_usage.cost_cny:.4f}")
    print(f"平均延迟：    {llm.total_usage.latency_ms / max(llm.calls, 1):.0f} ms")

    if failures:
        print("-" * 78)
        print("误分类详情（这是最有价值的失败案例，用于优化 prompt）：")
        for f in failures:
            c = f["case"]
            print(f"  {c['case_id']} [{c['category']}]")
            print(f"    消息：{c['message']}")
            print(f"    期望 {c['expected_intent']} → 实际 {f['actual']}")
            print(f"    LLM 理由：{f['reason']}")
        print("-" * 78)
        print("混淆对：")
        for (exp, act), n in confusion.most_common():
            print(f"  {exp} → {act}  ({n} 次)")

    print("=" * 78)
    return 0 if acc >= 85 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""从消融实验结果缓存重算指标。

★ 为什么需要这个工具：
  消融实验要跑 617 次 LLM 调用。而指标口径是会随认知修正的
  —— 本次就修正过一次：「决策准确率」原本拿引擎的动作级裁决
  去比案例的期望结局，两个量根本不是一回事，导致 V0 显示 0%，
  整张表看起来像坏的。

  把「跑实验」与「算指标」分离，口径修正就不必重跑昂贵实验，
  也保证了同一批原始数据可以被反复审视。

用法：
  python scripts/recompute_metrics.py
  python scripts/recompute_metrics.py --file data/evals/ablation_result.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.evals.metrics import CaseRun, compare_table, compute_metrics  # noqa: E402
from supportos.evals.variants import VARIANTS  # noqa: E402


def load_runs(path: Path) -> dict[str, list[CaseRun]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = set(CaseRun.__dataclass_fields__)
    out: dict[str, list[CaseRun]] = {}
    for key, v in data["variants"].items():
        runs = []
        for raw in v["runs"]:
            runs.append(CaseRun(**{k: val for k, val in raw.items() if k in fields}))
        out[key] = runs
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--file", default=str(ROOT / "data" / "evals" / "ablation_result.json")
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"未找到结果文件：{path}")
        print("请先运行：python scripts/ablation.py")
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    all_runs = load_runs(path)

    print("=" * 96)
    print(f"从缓存重算指标    {path.name}    模式：{data.get('mode')}    案例数：{data.get('cases')}")
    print("=" * 96)
    print("（原始运行数据未变，仅按最新指标口径重新计算）")
    print()

    metrics = {k: compute_metrics(rs) for k, rs in all_runs.items()}
    names = {
        k: (VARIANTS[k].name if k in VARIANTS else data["variants"][k]["name"])
        for k in all_runs
    }
    print(compare_table({names[k]: metrics[k] for k in all_runs}))
    print()

    # 分组安全表现
    print("-" * 96)
    print("分组安全表现（违规案例数）")
    print("-" * 96)
    print(f"{'案例组':<14}" + "".join(f"{names[k]:<26}" for k in all_runs))
    for gk, gname in (("edge", "边缘/高危"), ("bypass", "对抗性绕过"), ("baseline", "正常业务")):
        cells = []
        for k in all_runs:
            rs = [r for r in all_runs[k] if r.group == gk]
            bad = sum(1 for r in rs if not r.is_safe)
            cells.append(f"{bad}/{len(rs)} 违规".ljust(26))
        print(f"{gname:<14}" + "".join(cells))
    print("=" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

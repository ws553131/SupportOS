"""真实公开工单数据的校验分析。

★ 目的：检验「我的构造数据集是否过于乐观」。

  这是我在 README 中自己承认的最大局限 —— 所有案例都是我自己编的。
  因此必须尝试用外部数据来验证，而不是自说自话。

★ 但结论出乎预期，且必须如实报告：

  能找到的唯一可访问公开数据集（Kaggle "Customer Support Tickets"，
  8469 行，电商客服工单）**本身就是合成生成的**，不能用于验证语言真实性。

  证据（本脚本会自动检测）：
    1. 文本中留有未替换的模板占位符 `{product_purchased}`
    2. 描述由无关句子拼接，语义不连贯
    3. **标签自相矛盾**：Ticket Type / Subject / Description 三者互不对应
    4. 五个类别的分布近乎均匀（各约 20%），优先级与渠道同样均匀
       —— 真实的客服工单分布几乎不可能如此均匀

  那么这个数据集还有什么用？**它的分类体系（taxonomy）是有价值的。**
  「Refund request / Technical issue / Cancellation request /
    Product inquiry / Billing inquiry」反映了行业工具链的实际分类习惯，
  可以用它来检验**我的 V0.1 意图覆盖面**。

  这正是本次分析真正给出的结论：
  **我的三类意图只覆盖了标准电商工单分类中的一部分，
   而「物流延迟」与「商品破损」在该分类体系里甚至不作为顶层类别存在。**

运行：
  python scripts/fetch_external_data.py      # 先下载数据
  python scripts/real_data_validation.py     # 再运行本分析
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DATA_PATH = ROOT / "data" / "external" / "customer_support_tickets.csv"

# 我的 V0.1 意图 → 该数据集分类体系的映射（用于覆盖面分析）
MY_INTENT_TO_TAXONOMY = {
    "REFUND_REQUEST": ["Refund request"],
    "SHIPPING_DELAY": [],   # 该分类体系中没有对应顶层类别
    "DAMAGED_ITEM": [],     # 同上
}

# 反向：该数据集的类别能否被我的意图集表达
TAXONOMY_TO_MY_INTENT = {
    "Refund request": "REFUND_REQUEST",
    "Cancellation request": "—（超出 V0.1 范围）",
    "Technical issue": "—（超出 V0.1 范围）",
    "Product inquiry": "部分（政策咨询可覆盖）",
    "Billing inquiry": "—（超出 V0.1 范围）",
}


def load_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ============================================================
# 1. 数据质量检测：这份数据能不能用来验证真实性
# ============================================================


def detect_quality_issues(rows: list[dict]) -> dict:
    """检测合成痕迹。结论直接决定这份数据能用来做什么。"""
    issues: dict[str, object] = {}

    # --- 模板占位符 ---
    placeholder_pat = re.compile(r"\{[a-z_]+\}")
    ph_hits = sum(1 for r in rows if placeholder_pat.search(r.get("Ticket Description", "")))
    issues["含未替换占位符的行数"] = ph_hits
    issues["含占位符占比"] = f"{ph_hits / len(rows):.1%}"

    # --- 类别分布均匀度 ---
    tc = Counter(r["Ticket Type"] for r in rows)
    vals = list(tc.values())
    spread = (max(vals) - min(vals)) / (sum(vals) / len(vals))
    issues["类别分布"] = dict(tc.most_common())
    issues["类别分布极差/均值"] = f"{spread:.1%}（越接近 0 越像人为均分）"

    pc = Counter(r["Ticket Priority"] for r in rows)
    issues["优先级分布"] = dict(pc.most_common())
    chc = Counter(r["Ticket Channel"] for r in rows)
    issues["渠道分布"] = dict(chc.most_common())

    # --- 标签一致性：Type 与 Subject/Description 是否呼应 ---
    #  真实数据里 Refund request 类型的工单，Subject 往往也含退款相关词
    refund_rows = [r for r in rows if r["Ticket Type"] == "Refund request"]
    consistent = sum(
        1
        for r in refund_rows
        if any(k in (r.get("Ticket Subject", "") + r.get("Ticket Description", "")).lower()
               for k in ("refund", "money back", "return"))
    )
    issues["Refund类工单中文本也提及退款的占比"] = (
        f"{consistent / max(len(refund_rows), 1):.1%}"
    )

    # --- Resolution 列是否模板化 ---
    res = Counter(r.get("Resolution", "")[:40] for r in rows)
    issues["Resolution 去重后种类数"] = len(res)
    issues["最常见 Resolution 前缀"] = res.most_common(1)[0][0][:60] if res else ""

    # --- 描述长度是否过于集中 ---
    lens = sorted(len(r.get("Ticket Description", "")) for r in rows)
    issues["描述长度 min/中位/max"] = f"{lens[0]} / {lens[len(lens)//2]} / {lens[-1]}"

    return issues


# ============================================================
# 2. 分类体系覆盖面分析（这才是这份数据真正的用途）
# ============================================================


def coverage_analysis(rows: list[dict]) -> dict:
    tc = Counter(r["Ticket Type"] for r in rows)
    total = len(rows)

    covered = 0
    rows_out = []
    for t, n in tc.most_common():
        mapping = TAXONOMY_TO_MY_INTENT.get(t, "?")
        is_covered = mapping == "REFUND_REQUEST"
        if is_covered:
            covered += n
        rows_out.append(
            {"类别": t, "条数": n, "占比": f"{n / total:.1%}", "V0.1 可否表达": mapping}
        )

    return {
        "total": total,
        "rows": rows_out,
        "covered": covered,
        "coverage": f"{covered / total:.1%}",
    }


# ============================================================
# 3. 与我的构造数据对比
# ============================================================


def compare_with_mine(rows: list[dict]) -> dict:
    dataset = json.loads(
        (ROOT / "data" / "mock" / "dataset.json").read_text(encoding="utf-8")
    )
    my_msgs = [c["message"] for c in dataset["cases"]]
    real_desc = [r.get("Ticket Description", "") for r in rows]

    def stats(xs: list[str]) -> dict:
        ls = sorted(len(x) for x in xs)
        n = len(ls)
        return {
            "条数": n,
            "长度 min": ls[0],
            "长度 p25": ls[n // 4],
            "长度 中位": ls[n // 2],
            "长度 p75": ls[3 * n // 4],
            "长度 max": ls[-1],
        }

    return {"我的构造案例": stats(my_msgs), "公开数据集": stats(real_desc)}


# ============================================================


def main() -> int:
    if not DATA_PATH.exists():
        print(f"未找到数据文件：{DATA_PATH}")
        print("请先运行：python scripts/fetch_external_data.py")
        return 1
    rows = load_rows(DATA_PATH)

    print("=" * 92)
    print("真实公开工单数据校验分析")
    print("=" * 92)
    print(f"数据文件：{DATA_PATH.relative_to(ROOT)}")
    print(f"总行数：{len(rows)}")
    print()

    # ---------- 1. 数据质量 ----------
    print("=" * 92)
    print("一、数据质量检测：这份数据能用来验证真实性吗？")
    print("=" * 92)
    q = detect_quality_issues(rows)
    for k, v in q.items():
        if isinstance(v, dict):
            print(f"  {k}：")
            for kk, vv in v.items():
                print(f"      {kk:<18} {vv}")
        else:
            print(f"  {k}：{v}")

    print()
    print("  ★ 判定：这份数据**不能**用于验证语言真实性。")
    print("     未替换的模板占位符 + 标签互不对应 + 分布近乎均匀，")
    print("     三者共同表明它是模板生成的，而非真实工单记录。")
    print("     用它来「证明真实场景下的表现」会是误导性的。")

    # ---------- 2. 分类体系覆盖 ----------
    print()
    print("=" * 92)
    print("二、分类体系覆盖面：这才是这份数据真正的用途")
    print("=" * 92)
    cov = coverage_analysis(rows)
    print(f"  {'工单类别':<24}{'条数':>7}{'占比':>9}   V0.1 可否表达")
    print("  " + "-" * 86)
    for r in cov["rows"]:
        print(f"  {r['类别']:<24}{r['条数']:>7}{r['占比']:>9}   {r['V0.1 可否表达']}")
    print()
    print(f"  V0.1 意图集在真实工单分类体系中的覆盖率：{cov['covered']}/{cov['total']}"
          f" = {cov['coverage']}")
    print()
    print("  ★ 关键发现：")
    print("     1. 我的三类意图中，只有 REFUND_REQUEST 对应一个标准顶层类别。")
    print("     2. 「物流延迟」与「商品破损」在该分类体系里**不作为顶层类别存在** ——")
    print("        它们在真实工单系统里通常被归入 Product inquiry 或 Refund request。")
    print("        这不是缺陷（细分问题本就不该是顶层类别），")
    print("        但意味着**我的意图集是「问题域」而非「工单分类」**，两者粒度不同。")
    print("     3. 真实电商客服的工单量里，Technical issue / Billing inquiry /")
    print("        Cancellation request 各占约 20%，V0.1 完全不覆盖 ——")
    print("        这解释了为什么产品必须声明「V0.1 只冻结四类典型售后场景」。")

    # ---------- 3. 规模对比 ----------
    print()
    print("=" * 92)
    print("三、与我的构造数据对比")
    print("=" * 92)
    cmp = compare_with_mine(rows)
    keys = list(next(iter(cmp.values())).keys())
    print(f"  {'指标':<14}{'我的构造案例':>16}{'公开数据集':>16}")
    print("  " + "-" * 50)
    for k in keys:
        print(f"  {k:<14}{str(cmp['我的构造案例'][k]):>16}{str(cmp['公开数据集'][k]):>16}")
    print()
    print("  ★ 我的案例消息显著更短（约 20~40 字 vs 公开数据 300 字）。")
    print("     原因：我的案例模拟的是**会话中的单条客户消息**，")
    print("     而公开数据集存的是**整段工单描述**（含上下文与历史）。")
    print("     → 这是一个真实的差异，意味着我的系统并未在「长工单描述」上测试过。")

    # ---------- 4. 结论 ----------
    print()
    print("=" * 92)
    print("四、结论（必须写进 README 的诚实声明）")
    print("=" * 92)
    print("  1. 我**未能**用公开数据验证「构造数据集是否过于乐观」这个核心问题 ——")
    print("     因为唯一可访问的公开数据本身就是合成的。")
    print("  2. 本次分析的有效产出是**分类体系覆盖面**：")
    print(f"     V0.1 意图集仅覆盖标准电商工单分类的 {cov['coverage']}。")
    print("  3. 这修正了我此前一个隐含的错误认知：")
    print("     我以为「物流延迟/商品破损」是电商客服的主要工单类型，")
    print("     但在行业通用分类里它们甚至不是顶层类别。")
    print("  4. 因此「安全评测的 0 违规」应当被理解为")
    print("     「在我定义的 3 个问题域、我构造的 64 条案例上无违规」，")
    print("     而不是「在真实电商客服场景下无违规」。")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

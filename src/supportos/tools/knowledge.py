"""结构化政策知识库 —— 刻意不使用向量检索。

★ 核心产品决策：政策裁决需要的是「确定性」，不是「语义相似」。

为什么不和文档 RAG 一样用 embedding：
  1. 政策库只有 6 条，结构化匹配更准（不会把"退款政策"匹配到"优惠券政策"）
  2. 裁决必须可复现：同样的 case 必须得到同样的结论，语义相似度会引入随机性
  3. 必须可审计：要能明确说出「命中了哪条政策的哪一节」
  4. 必须校验有效期：过期政策不得作为裁决依据，这在向量检索里很难做硬约束

检索输出必须包含 policy_id / version / section / effective_date ——
这样才能回答「AI 依据的是哪一条政策」，而不是「AI 觉得差不多是这条」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from supportos.domain.enums import Intent, KnowledgeGap

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "data" / "policies" / "policies.json"


@dataclass(frozen=True)
class PolicySection:
    policy_id: str
    title: str
    version: str
    effective_date: date
    section: str
    condition: str
    action: str
    params: dict
    text: str

    def cite(self) -> str:
        """可审计的引用串。"""
        return f"{self.policy_id} {self.version} §{self.section}"


@dataclass
class RetrievalResult:
    """一次政策检索的完整结果（含所有审计信息）。"""

    sections: list[PolicySection] = field(default_factory=list)
    policy_ids: list[str] = field(default_factory=list)
    gap: KnowledgeGap = KnowledgeGap.NONE
    gap_detail: str = ""
    expired_policy_ids: list[str] = field(default_factory=list)
    """命中但已过期/未生效的政策 —— 必须记录，否则审计时说不清。"""

    @property
    def found(self) -> bool:
        return bool(self.sections)

    def citations(self) -> list[str]:
        return [s.cite() for s in self.sections]


@dataclass
class _GapRule:
    gap_id: str
    intent: str
    patterns: list[str]
    detail: str


class KnowledgeBase:
    """支持确定性检索的政策知识库。"""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_POLICY_PATH
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._raw = raw.get("policies", [])
        self._gaps = [
            _GapRule(g["gap_id"], g["intent"], g["pattern"], g["detail"])
            for g in raw.get("known_gaps", [])
        ]
        self._sections: list[PolicySection] = []
        for p in self._raw:
            ed = date.fromisoformat(p["effective_date"])
            for s in p.get("sections", []):
                self._sections.append(
                    PolicySection(
                        policy_id=p["policy_id"],
                        title=p["title"],
                        version=p["version"],
                        effective_date=ed,
                        section=s["section"],
                        condition=s["condition"],
                        action=s["action"],
                        params=s.get("params", {}),
                        text=s["text"],
                    )
                )

    # ---------- 有效期 ----------

    @staticmethod
    def _is_effective(sec: PolicySection, today: date, raw: dict) -> bool:
        if sec.effective_date > today:
            return False
        exp = raw.get("expires_date")
        if exp and date.fromisoformat(exp) < today:
            return False
        return True

    def _policy_raw(self, policy_id: str) -> dict:
        for p in self._raw:
            if p["policy_id"] == policy_id:
                return p
        return {}

    # ---------- 检索 ----------

    def search(
        self,
        intent: Intent | None,
        query: str = "",
        today: date | None = None,
    ) -> RetrievalResult:
        """按 Intent + 关键词检索适用政策。

        Args:
            intent: Triage Agent 识别出的意图。
            query: 客户原始消息，用于命中已登记的「知识缺口」模式。
            today: 检索基准日（可注入，便于测试政策过期）。
        """
        today = today or date.today()
        result = RetrievalResult()

        # --- 1. 已登记的知识缺口优先 ---
        # ★ 关键修正：缺口匹配不能「因为意图不明就跳过」。
        #   原实现要求 `g.intent == intent.value` 才检查该缺口，
        #   于是当 Triage 把「海外订单想退款」判成 UNKNOWN 时，
        #   GAP-001 永远不会被检出，知识缺口案例被静默放过。
        #   正确做法：命中缺口模式就应上报；意图只作为**排除项**
        #   （意图明确且与该缺口无关时跳过），不作为必要条件。
        for g in self._gaps:
            hit = [p for p in g.patterns if p in (query or "")]
            if not hit:
                continue
            if (
                intent is not None
                and intent is not Intent.UNKNOWN
                and g.intent != intent.value
            ):
                continue  # 意图明确且与该缺口无关，跳过以免误报
            result.gap = KnowledgeGap.INSUFFICIENT
            result.gap_detail = f"[{g.gap_id}] {g.detail}（命中关键词：{'、'.join(hit)}）"
            result.sections = self._match_by_intent(intent, today, result)
            result.policy_ids = sorted({s.policy_id for s in result.sections})
            return result

        # --- 2. 按 Intent 匹配 ---
        result.sections = self._match_by_intent(intent, today, result)
        result.policy_ids = sorted({s.policy_id for s in result.sections})

        # --- 3. 无有效政策即知识缺口 ---
        if not result.sections:
            if result.expired_policy_ids:
                result.gap = KnowledgeGap.POLICY_EXPIRED
                result.gap_detail = (
                    f"命中的政策 {', '.join(result.expired_policy_ids)} "
                    "已过期或尚未生效，不能作为裁决依据"
                )
            else:
                result.gap = KnowledgeGap.NO_POLICY_FOUND
                result.gap_detail = f"知识库中没有适用于 {intent} 的有效政策"

        return result

    def _match_by_intent(
        self, intent: Intent | None, today: date, result: RetrievalResult
    ) -> list[PolicySection]:
        if intent is None:
            return []
        out: list[PolicySection] = []
        for p in self._raw:
            if intent.value not in p.get("applies_to_intents", []):
                continue
            ed = date.fromisoformat(p["effective_date"])
            if ed > today or (
                p.get("expires_date") and date.fromisoformat(p["expires_date"]) < today
            ):
                result.expired_policy_ids.append(p["policy_id"])
                continue
            for s in p.get("sections", []):
                out.append(
                    PolicySection(
                        policy_id=p["policy_id"],
                        title=p["title"],
                        version=p["version"],
                        effective_date=ed,
                        section=s["section"],
                        condition=s["condition"],
                        action=s["action"],
                        params=s.get("params", {}),
                        text=s["text"],
                    )
                )
        return out

    # ---------- 冲突检测 ----------

    def detect_conflicts(
        self, sections: list[PolicySection], action: str, amount: float | None = None
    ) -> list[str]:
        """检测同一动作上的政策冲突。

        ★ 真实场景：不同政策对「延迟补偿上限」给出不同数值时，
          AI 不得自行选择较宽松的一条，必须升级由人裁决。
        """
        relevant = [s for s in sections if s.action == action]
        if len(relevant) < 2:
            return []

        conflicts: list[str] = []
        limits = [
            (s, s.params.get("max"))
            for s in relevant
            if isinstance(s.params.get("max"), (int, float))
        ]
        if len(limits) >= 2:
            values = {v for _, v in limits}
            if len(values) > 1:
                detail = "、".join(f"{s.cite()} 上限 ¥{v:g}" for s, v in limits)
                conflicts.append(f"检测到同一动作 {action} 的多条政策上限不一致：{detail}")
        return conflicts

    # ---------- 便捷访问 ----------

    def all_policy_ids(self) -> list[str]:
        return [p["policy_id"] for p in self._raw]

    def effective_policy_ids(self, today: date | None = None) -> list[str]:
        today = today or date.today()
        return [
            p["policy_id"]
            for p in self._raw
            if date.fromisoformat(p["effective_date"]) <= today
            and not (p.get("expires_date") and date.fromisoformat(p["expires_date"]) < today)
        ]

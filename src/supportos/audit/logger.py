"""审计日志 —— 所有 Agent 行为必须留痕（PRD 第 12.5 节）。

为什么审计是产品能力而不是运维功能：
  客服场景里，「AI 到底做了什么决定、依据是什么、谁批准的」
  是事后追责与合规审查的唯一依据。
  没有审计的 AI 客服系统无法通过任何企业采购的安全评审。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class AuditRecord:
    timestamp: str
    ticket_id: str
    actor: str
    """TriageAgent / SupportAgent / ActionPlanner / PolicyEngine / 人类用户名。"""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    authorized_user: str | None = None
    approval_status: str = "NONE"
    policy_rule: str = ""
    """命中的策略规则 ID —— 让审计可以回溯到具体规则。"""

    def to_line(self) -> str:
        """单行可读格式，便于人工翻阅。"""
        ts = self.timestamp[11:19] if len(self.timestamp) >= 19 else self.timestamp
        extra = f" [{self.policy_rule}]" if self.policy_rule else ""
        auth = f" ← {self.authorized_user}" if self.authorized_user else ""
        return f"{ts}  {self.actor:<16}{self.action}{extra}{auth}  {self.result}"


class AuditLog:
    """工单审计日志。"""

    def __init__(self, ticket_id: str):
        self.ticket_id = ticket_id
        self.records: list[AuditRecord] = []

    def record(
        self,
        actor: str,
        action: str,
        params: dict | None = None,
        result: str = "",
        authorized_user: str | None = None,
        approval_status: str = "NONE",
        policy_rule: str = "",
    ) -> AuditRecord:
        r = AuditRecord(
            timestamp=datetime.now().isoformat(),
            ticket_id=self.ticket_id,
            actor=actor,
            action=action,
            params=params or {},
            result=result,
            authorized_user=authorized_user,
            approval_status=approval_status,
            policy_rule=policy_rule,
        )
        self.records.append(r)
        return r

    def to_dicts(self) -> list[dict]:
        return [asdict(r) for r in self.records]

    def render(self) -> str:
        return "\n".join(r.to_line() for r in self.records)

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dicts(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 审计完整性校验 ----------

    def assert_no_unlogged_write(self, registry) -> None:
        """校验：所有写操作副作用都有对应审计记录。

        ★ 这是审计体系的意义所在 ——
          如果系统执行了写操作却没留痕，那审计就是假的。
        """
        write_actions = set()
        for r in self.records:
            if r.action in (
                "issue_coupon",
                "request_refund",
                "request_replacement",
            ):
                write_actions.add(r.action)

        snap = registry.snapshot()
        executed: set[str] = set()
        if snap["coupons_issued"]:
            executed.add("issue_coupon")
        if snap["refund_requests"]:
            executed.add("request_refund")
        if snap["replacement_requests"]:
            executed.add("request_replacement")

        missing = executed - write_actions
        if missing:
            raise AssertionError(
                f"工单 {self.ticket_id} 执行了写操作但缺少审计记录：{missing}"
            )

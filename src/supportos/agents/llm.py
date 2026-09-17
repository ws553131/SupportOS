"""LLM 调用封装。

设计原则：
  1. 统一走 OpenAI 兼容协议 —— 换厂商只改配置，不改代码
  2. 强制 JSON 输出 + 解析校验 —— 不让 LLM 的自由文本污染下游
  3. 记录 token 用量 —— 成本指标是产品指标的一部分，必须可观测
  4. 失败时明确抛错，不静默降级（降级策略由调用方决定）
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from supportos.config import Settings, load_settings, make_llm_client


@dataclass
class LLMUsage:
    """单次调用统计。用于评测中的 Cost / Latency 指标。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0

    @property
    def cost_cny(self) -> float:
        """按 DeepSeek 定价估算（¥1/百万 输入、¥2/百万 输出，缓存命中更便宜）。

        注意：这里是**估算**，真实计费以厂商账单为准。
        """
        return (
            self.prompt_tokens / 1_000_000 * 1.0
            + self.completion_tokens / 1_000_000 * 2.0
        )


@dataclass
class LLMResult:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: Any = None

    def json(self) -> dict:
        """解析 JSON 输出，容忍 ```json 代码块包裹。"""
        t = self.text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?\s*", "", t)
            t = re.sub(r"\s*```$", "", t)
        try:
            return json.loads(t)
        except json.JSONDecodeError as e:
            raise LLMFormatError(f"LLM 返回的不是合法 JSON：{e}\n原文：{self.text[:500]}") from e


class LLMError(RuntimeError):
    pass


class LLMFormatError(LLMError):
    pass


class LLMClient:
    """轻量 LLM 客户端。"""

    def __init__(self, settings: Settings | None = None, client=None):
        self.settings = settings or load_settings(require_llm=True)
        self._client = client or make_llm_client(self.settings)
        self.total_usage = LLMUsage()
        self.calls = 0

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> LLMResult:
        """单轮补全。

        temperature 默认 0：分类/裁决类任务需要稳定性，不需要创造力。
        """
        kwargs: dict[str, Any] = {
            "model": model or self.settings.llm.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        t0 = time.perf_counter()
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"LLM 调用失败：{type(e).__name__}: {e}") from e
        latency = (time.perf_counter() - t0) * 1000

        text = resp.choices[0].message.content or ""
        u = getattr(resp, "usage", None)
        usage = LLMUsage(
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
            latency_ms=latency,
        )

        # 累计统计
        self.calls += 1
        self.total_usage = LLMUsage(
            prompt_tokens=self.total_usage.prompt_tokens + usage.prompt_tokens,
            completion_tokens=self.total_usage.completion_tokens + usage.completion_tokens,
            total_tokens=self.total_usage.total_tokens + usage.total_tokens,
            latency_ms=self.total_usage.latency_ms + usage.latency_ms,
        )

        return LLMResult(text=text, usage=usage, raw=resp)

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "prompt_tokens": self.total_usage.prompt_tokens,
            "completion_tokens": self.total_usage.completion_tokens,
            "total_tokens": self.total_usage.total_tokens,
            "est_cost_cny": round(self.total_usage.cost_cny, 6),
        }

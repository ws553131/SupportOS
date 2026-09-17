"""SupportOS 配置加载。

设计原则：
  - 密钥只从环境变量 / .env 读取，绝不硬编码
  - .env 已在 .gitignore 中，不会误提交
  - 配置缺失时给出**可执行的修复指引**，而不是抛一个看不懂的 KeyError
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（src/supportos/config.py -> 上三级）
ROOT = Path(__file__).resolve().parents[2]

# 依次加载：项目根 .env、当前工作目录 .env
load_dotenv(ROOT / ".env")
load_dotenv()


class ConfigError(RuntimeError):
    """配置缺失。错误信息必须包含修复步骤。"""


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    api_key: str
    base_url: str
    model: str

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("sk-你的")


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    env: str
    log_level: str
    db_path: Path
    eval_use_judge: bool
    eval_judge_model: str


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def load_settings(require_llm: bool = False) -> Settings:
    """加载配置。

    Args:
        require_llm: 为 True 时，若 LLM 未配置则抛 ConfigError。
                     纯策略层测试（Policy Engine）不需要 LLM，传 False 即可。
    """
    llm = LLMSettings(
        provider=_env("SUPPORTOS_LLM_PROVIDER", "deepseek"),
        api_key=_env("SUPPORTOS_LLM_API_KEY"),
        base_url=_env("SUPPORTOS_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        model=_env("SUPPORTOS_LLM_MODEL", "deepseek-chat"),
    )

    if require_llm and not llm.is_configured:
        raise ConfigError(
            "\n未检测到有效的 LLM API Key。修复步骤：\n"
            f"  1. 复制模板：  Copy-Item {ROOT / '.env.example'} {ROOT / '.env'}\n"
            "  2. 申请 Key：  https://platform.deepseek.com/api_keys\n"
            f"  3. 编辑 {ROOT / '.env'}，把 SUPPORTOS_LLM_API_KEY 填成你的真实 Key\n"
            "  4. 验证：      python scripts/check_env.py\n"
            "\n注意：Policy Engine 单元测试不需要 API Key，可直接运行：\n"
            "  pytest tests/test_policy_engine.py\n"
        )

    return Settings(
        llm=llm,
        env=_env("SUPPORTOS_ENV", "dev"),
        log_level=_env("SUPPORTOS_LOG_LEVEL", "INFO"),
        db_path=ROOT / _env("SUPPORTOS_DB_PATH", "data/supportos.db"),
        eval_use_judge=_env("SUPPORTOS_EVAL_USE_JUDGE", "true").lower() == "true",
        eval_judge_model=_env("SUPPORTOS_EVAL_JUDGE_MODEL", "deepseek-chat"),
    )


def make_llm_client(settings: Settings | None = None):
    """构造 OpenAI 兼容客户端。

    DeepSeek / 通义 / 智谱 都兼容 OpenAI SDK，只需换 base_url 与 model。
    """
    from openai import OpenAI

    s = settings or load_settings(require_llm=True)
    return OpenAI(api_key=s.llm.api_key, base_url=s.llm.base_url)

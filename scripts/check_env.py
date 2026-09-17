"""环境检查工具：验证 API Key 是否配置正确。

用法：python scripts/check_env.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

OK = "  [OK]  "
FAIL = "  [FAIL]"
WARN = "  [WARN]"

print("=" * 62)
print("SupportOS 环境检查")
print("=" * 62)

# ---------- 1. .env 文件 ----------
from supportos.config import ROOT, ConfigError, load_settings  # noqa: E402

env_file = ROOT / ".env"
if env_file.exists():
    print(f"{OK} 找到 .env 文件：{env_file}")
else:
    print(f"{FAIL} 未找到 .env 文件")
    print(f"         修复：Copy-Item .env.example .env")

# ---------- 2. 加载配置 ----------
try:
    settings = load_settings(require_llm=False)
except ConfigError as e:
    print(f"{FAIL} 配置加载失败：{e}")
    sys.exit(1)

# ---------- 3. LLM 配置 ----------
print(f"{OK} LLM Provider : {settings.llm.provider}")
print(f"{OK} LLM Base URL : {settings.llm.base_url}")
print(f"{OK} LLM Model    : {settings.llm.model}")

key = settings.llm.api_key
if not key:
    print(f"{FAIL} SUPPORTOS_LLM_API_KEY 为空")
    print("         修复：在 .env 中填入你的 Key（申请：https://platform.deepseek.com/api_keys）")
elif key.startswith("sk-你的"):
    print(f"{FAIL} SUPPORTOS_LLM_API_KEY 仍是模板占位符")
    print("         修复：把 .env 里的 sk-你的deepseek密钥 替换成真实 Key")
else:
    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"
    print(f"{OK} API Key 已配置（{masked}），长度 {len(key)}")

# ---------- 4. 实际连通性测试 ----------
if settings.llm.is_configured:
    print("\n正在测试 API 连通性 ...")
    try:
        from supportos.config import make_llm_client

        client = make_llm_client(settings)
        resp = client.chat.completions.create(
            model=settings.llm.model,
            messages=[{"role": "user", "content": "回复两个字：正常"}],
            max_tokens=10,
        )
        text = resp.choices[0].message.content
        print(f"{OK} API 调用成功，模型回复：{text!r}")
        usage = getattr(resp, "usage", None)
        if usage:
            print(f"{OK} Token 用量：prompt={usage.prompt_tokens} completion={usage.completion_tokens}")
    except Exception as e:  # noqa: BLE001
        print(f"{FAIL} API 调用失败：{type(e).__name__}: {e}")
        print("         常见原因：Key 无效 / 余额不足 / 网络不通 / base_url 写错")
        sys.exit(1)
else:
    print(f"\n{WARN} 跳过 API 连通性测试（Key 未配置）")
    print("         这不影响 Policy Engine 测试：pytest tests/test_policy_engine.py")

# ---------- 5. 依赖检查 ----------
print()
for mod in ("pydantic", "openai", "dotenv"):
    try:
        m = __import__(mod)
        print(f"{OK} 依赖 {mod:10s} {getattr(m, '__version__', '')}")
    except ImportError:
        print(f"{FAIL} 缺少依赖 {mod} → pip install {mod}")

print("=" * 62)

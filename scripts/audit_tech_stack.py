"""技术栈审计脚本。

★ 为什么需要它：
  简历上写的技术名词必须是**代码里真实使用的**。
  申报了却没用到的技术，一旦被面试官追问细节就会立刻露馅。
  本脚本用 AST 解析真实 import，而不是靠记忆。

用法：python scripts/audit_tech_stack.py
"""

from __future__ import annotations

import ast
import collections
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STDLIB = set(sys.stdlib_module_names)

# 关注的技术清单
WATCH = [
    "fastapi", "uvicorn", "jinja2", "starlette",
    "chromadb", "sqlite3", "langchain", "langgraph", "llama_index",
    "pytest", "dotenv", "pydantic", "openai", "anthropic",
    "numpy", "pandas", "requests", "httpx", "aiohttp",
    "redis", "celery", "sqlalchemy", "alembic",
    "dashscope", "zhipuai", "transformers", "sentence_transformers",
]


def scan(files: list[Path]) -> dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    counter[a.name.split(".")[0]] += 1
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    counter[node.module.split(".")[0]] += 1
    return dict(counter)


def main() -> int:
    files = sorted(
        list((ROOT / "src").rglob("*.py"))
        + list((ROOT / "scripts").glob("*.py"))
        + list((ROOT / "tests").glob("*.py"))
    )
    used = scan(files)

    print("=" * 74)
    print(f"技术栈审计    扫描 {len(files)} 个 Python 文件")
    print("=" * 74)
    print()

    # ---------- 1. 真实使用的第三方库 ----------
    third_party = {
        k: v for k, v in used.items()
        if k not in STDLIB and k != "supportos"
    }
    print("一、代码中真实使用的第三方库（AST 解析结果）")
    print("-" * 74)
    for k, v in sorted(third_party.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<20} 被 {v} 个模块 import")
    if not third_party:
        print("  （无）")

    # ---------- 2. 声明但未使用 ----------
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared: list[str] = list(
        pyproject.get("project", {}).get("dependencies", [])
    )
    optional = pyproject.get("project", {}).get("optional-dependencies", {})
    for group, deps in optional.items():
        declared += list(deps)

    def pkg_name(spec: str) -> str:
        return spec.split(">")[0].split("=")[0].split("[")[0].strip().replace("-", "_")

    print()
    print("二、pyproject.toml 中声明但代码中未使用的依赖")
    print("-" * 74)
    unused = []
    for spec in declared:
        name = pkg_name(spec)
        alias = {"python_dotenv": "dotenv"}.get(name, name)
        if alias not in used:
            unused.append((spec, name))
    if unused:
        for spec, name in unused:
            print(f"  ⚠ {spec:<24} 声明了但从未 import")
        print()
        print("  ★ 简历上不要写这些技术 —— 被追问细节会立刻露馅。")
    else:
        print("  （无）")

    # ---------- 3. 重点技术核查 ----------
    print()
    print("三、重点技术核查")
    print("-" * 74)
    print(f"  {'技术':<26}{'import 次数':<14}{'结论'}")
    print("  " + "-" * 68)
    for t in WATCH:
        n = used.get(t, 0)
        if n:
            verdict = "✅ 真实使用"
        else:
            verdict = "❌ 未使用"
        print(f"  {t:<26}{n:<14}{verdict}")

    # ---------- 4. 刻意未使用的技术 ----------
    print()
    print("四、刻意未使用的技术（这些是设计决策，可以讲）")
    print("-" * 74)
    notes = [
        ("chromadb / 向量检索",
         "政策库只有 6 条，且裁决需要确定性而非语义相似。"
         "结构化匹配可精确回溯 policy_id + version + section。"),
        ("LangChain / LangGraph",
         "编排逻辑是 8 段式固定流程，手写更可控。"
         "而且面试时能解释每一处取舍，框架反而是黑盒。"),
        ("SQLite / 数据库",
         "V0.1 用 JSON 数据集模拟业务系统，接真实系统只需替换 ToolRegistry 内部实现。"),
        ("Docker",
         "本机无 Docker，且单机 Python 项目不需要容器化。"),
    ]
    for name, why in notes:
        print(f"  · {name}")
        print(f"      {why}")

    print()
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

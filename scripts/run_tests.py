"""极简 pytest 兼容运行器。

为什么存在：本机环境暂时装不上 pytest，但测试必须现在就能跑。
它支持 tests/ 中用到的 pytest 子集：
  - @pytest.mark.parametrize（含多参数）
  - assert（含失败时的表达式回溯）

测试文件保持**标准 pytest 写法**，将来装好 pytest 后可直接 `pytest tests/` 运行。

用法：python scripts/run_tests.py [测试文件...]
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


# ------------------------------------------------------------
# pytest 兼容层：在导入测试模块前，把一个假 pytest 塞进 sys.modules
# ------------------------------------------------------------


class _Mark:
    @staticmethod
    def parametrize(argnames, argvalues):
        names = [n.strip() for n in argnames.split(",")] if isinstance(argnames, str) else list(argnames)

        def deco(fn):
            cases = []
            for v in argvalues:
                vals = v if isinstance(v, (tuple, list)) else (v,)
                cases.append(dict(zip(names, vals)))
            # ★ 支持叠加 @parametrize：真实 pytest 会做笛卡尔积。
            #   叠加时内层装饰器先执行，若无条件覆盖，只会剩下最后一组参数。
            prev = getattr(fn, "_parametrize", None)
            if prev:
                merged = []
                for a in prev:
                    for b in cases:
                        m = dict(a)
                        m.update(b)
                        merged.append(m)
                fn._parametrize = merged
            else:
                fn._parametrize = cases
            return fn

        return deco

    def __getattr__(self, name):  # 其他 mark 一律当装饰器透传
        def deco(*a, **k):
            if len(a) == 1 and callable(a[0]):
                return a[0]

            def inner(fn):
                return fn

            return inner

        return deco


class _PytestShim:
    mark = _Mark()

    @staticmethod
    def fixture(*a, **k):
        if len(a) == 1 and callable(a[0]):
            return a[0]

        def inner(fn):
            return fn

        return inner

    @staticmethod
    def raises(expected, match=None):
        """pytest.raises 的最小实现，支持 match 正则匹配。"""

        class _Ctx:
            def __init__(self):
                self.value = None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                if exc_type is None:
                    raise AssertionError(
                        f"期望抛出 {expected.__name__}，但没有抛出任何异常"
                    )
                if not issubclass(exc_type, expected):
                    return False  # 其他异常正常传播
                self.value = exc
                if match is not None:
                    import re

                    if not re.search(match, str(exc)):
                        raise AssertionError(
                            f"{exc_type.__name__} 消息不匹配 {match!r}：{exc}"
                        ) from exc
                return True  # 吞掉期望的异常

        return _Ctx()


# ------------------------------------------------------------
# 收集与执行
# ------------------------------------------------------------


def load_module(path: Path):
    name = f"_t_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def collect(mod):
    """产出 (显示名, 可调用对象)。"""
    out = []
    for cname, cls in inspect.getmembers(mod, inspect.isclass):
        if not cname.startswith("Test"):
            continue
        for mname, fn in inspect.getmembers(cls, inspect.isfunction):
            if not mname.startswith("test_"):
                continue
            bound = getattr(cls(), mname)
            cases = getattr(fn, "_parametrize", None)
            if cases:
                for c in cases:
                    label = f"{cname}::{mname}[{', '.join(repr(v) for v in c.values())}]"
                    out.append((label, bound, c))
            else:
                out.append((f"{cname}::{mname}", bound, None))
    return out


def main(argv: list[str]) -> int:
    import types

    # 注入假 pytest
    fake = types.ModuleType("pytest")
    fake.mark = _Mark()
    fake.fixture = _PytestShim.fixture
    fake.raises = _PytestShim.raises
    sys.modules["pytest"] = fake

    targets = [Path(a) for a in argv[1:]] or sorted((ROOT / "tests").glob("test_*.py"))

    passed = failed = 0
    failures: list[tuple[str, str]] = []

    for path in targets:
        print(f"\n{'=' * 62}\n{path.relative_to(ROOT)}\n{'=' * 62}")
        try:
            mod = load_module(path)
        except Exception:
            print(f"  [IMPORT ERROR] {path}")
            traceback.print_exc()
            failed += 1
            continue

        for label, fn, kwargs in collect(mod):
            try:
                if kwargs:
                    fn(**kwargs)
                else:
                    fn()
                passed += 1
                print(f"  [PASS] {label}")
            except AssertionError as e:
                failed += 1
                msg = str(e) or "(无断言消息)"
                failures.append((label, msg))
                print(f"  [FAIL] {label}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                msg = f"{type(e).__name__}: {e}"
                failures.append((label, msg))
                print(f"  [ERROR] {label}  ->  {msg}")

    print(f"\n{'=' * 62}")
    print(f"结果： {passed} 通过 / {failed} 失败 / 共 {passed + failed}")
    if failures:
        print("-" * 62)
        for label, msg in failures:
            print(f"  失败：{label}\n        {msg}")
    else:
        print("  全部通过")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

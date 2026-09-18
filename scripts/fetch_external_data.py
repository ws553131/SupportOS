"""下载外部公开数据集（用于校验，不纳入版本控制）。

★ 为什么单独写一个下载脚本：
  1. 数据集 3.9 MB，不应提交到仓库
  2. 许可证归属原作者，我们只做本地分析、不重新分发
  3. 其他人克隆仓库后应当能一条命令复现这个分析

★ 一个环境上的坑（值得记录）：
  本机 PowerShell 的 `Invoke-WebRequest` 与 `curl.exe` 都失败于
  `SEC_E_NO_CREDENTIALS` —— 沙箱阻止了子进程的 schannel 凭证获取。
  但 **Python 的 urllib 使用自带的 OpenSSL 栈，可以正常下载**。
  因此本脚本用 Python 实现，而不是 PowerShell。

用法：python scripts/fetch_external_data.py
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "external"

SOURCES = [
    {
        "name": "customer_support_tickets.csv",
        "url": (
            "https://raw.githubusercontent.com/leylaeminova/"
            "Customer-Support-Ticket-Classification/main/customer_support_tickets.csv"
        ),
        "origin": "Kaggle 'Customer Support Tickets' (电商客服工单，8469 行)",
        "note": "上游为 Kaggle 数据集，此处引用一个 GitHub 镜像以便脚本化获取",
    },
]


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for s in SOURCES:
        dest = OUT_DIR / s["name"]
        if dest.exists() and dest.stat().st_size > 0:
            print(f"[跳过] 已存在：{dest.name}（{dest.stat().st_size} bytes）")
            continue
        print(f"[下载] {s['name']}")
        print(f"       来源：{s['origin']}")
        try:
            req = urllib.request.Request(
                s["url"], headers={"User-Agent": "SupportOS-research/0.1"}
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            dest.write_bytes(data)
            print(f"[完成] {dest.name}（{len(data)} bytes）")
        except Exception as e:  # noqa: BLE001
            print(f"[失败] {type(e).__name__}: {e}")
            ok = False

    print()
    print(f"输出目录：{OUT_DIR.relative_to(ROOT)}")
    print("注意：该目录已在 .gitignore 中排除 —— 数据集不随仓库分发。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""录入 GitHub 令牌（输入不可见、不经过对话历史），并验证权限。

用法（在本目录打开 PowerShell）：
    & "C:\\Users\\Meayair\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe" set_token.py
粘贴令牌后回车。令牌会写入本机 _token.txt（已在 .gitignore 中，不会上传），
同时尝试写入 Windows 凭据管理器（失败不影响使用）。

令牌申请：https://github.com/settings/personal-access-tokens/new
  - Fine-grained token
  - Repository access: Only select repositories -> dalian-market-price
  - Permissions: Contents = Read and write（含 Release 附件上传）
"""
import getpass
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = os.environ.get("DALIAN_PRICE_REPO", "Meayair/dalian-market-price")
PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:10808"
if PROXY:
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})))


def verify(tok: str) -> bool:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}",
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "dalian-price"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            info = json.load(r)
        perm = info.get("permissions", {})
        print(f"仓库: {info.get('full_name')}  权限: "
              f"admin={perm.get('admin')} push={perm.get('push')} "
              f"pull={perm.get('pull')}")
        return bool(perm.get("push"))
    except Exception as e:  # noqa: BLE001
        print("验证失败:", e)
        return False


def main() -> int:
    tok = getpass.getpass("GitHub 令牌（输入不可见）: ").strip()
    if not tok:
        print("未输入，已取消。")
        return 1
    if not verify(tok):
        print("令牌无效或没有该仓库的写权限，未保存。")
        return 1
    (HERE / "_token.txt").write_text(tok + "\n", encoding="utf-8")
    print("已保存到 _token.txt（不进 git）")
    try:
        subprocess.run(["git", "credential", "approve"],
                       input=f"protocol=https\nhost=github.com\n"
                             f"username=Meayair\npassword={tok}\n\n",
                       capture_output=True, text=True, timeout=20)
        print("同时已尝试写入 Windows 凭据管理器")
    except Exception:  # noqa: BLE001
        print("凭据管理器写入跳过（不影响使用）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""绕过 git 客户端 / GCM，直接用 GitHub Git Trees API 提交文件。

- 一次请求创建整棵目录树（可携带文件内容），再建 commit、更新 ref —— 共 3 次请求
- 自动与远端比对 blob sha，只提交有变化的文件（每日增量仅几个文件）
- 令牌来源：环境变量 DALIAN_PRICE_TOKEN → Windows 凭据管理器（git credential fill）
- 走本机代理（直连 GitHub 会被 reset）

用法：
    python push_via_api.py            # 推送所有变化
    python push_via_api.py --dry-run  # 只看将提交哪些文件
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = os.environ.get("DALIAN_PRICE_REPO", "Meayair/dalian-market-price")
BRANCH = "main"
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
    or "http://127.0.0.1:10808"
SKIP_DIRS = {".git", "dist", "__pycache__"}
SKIP_FILES = {"_publish_log.txt"}

if PROXY:
    urllib.request.install_opener(urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})))


def log(m: str) -> None:
    print(m, flush=True)


def token() -> str:
    """令牌优先级：环境变量 → 本地文件 _token.txt → 凭据管理器。"""
    env = os.environ.get("DALIAN_PRICE_TOKEN", "").strip()
    if env:
        return env
    tf = HERE / "_token.txt"
    if tf.exists():
        t = tf.read_text(encoding="utf-8").strip().splitlines()
        if t and t[0].strip():
            return t[0].strip()
    for i in range(2):
        try:
            r = subprocess.run(["git", "credential", "fill"],
                               input="protocol=https\nhost=github.com\n\n",
                               capture_output=True, text=True, timeout=15)
            for line in r.stdout.splitlines():
                if line.startswith("password="):
                    return line.split("=", 1)[1]
        except subprocess.TimeoutExpired:
            log(f"[warn] credential fill 第 {i + 1} 次超时")
    return ""


def api(url: str, tok: str, method: str = "GET", payload: dict | None = None,
        retries: int = 2):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "dalian-price-publisher")
    if data:
        req.add_header("Content-Type", "application/json")
    last = None
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"[warn] {method} {url.split('/git/')[-1]} 第 {i + 1} 次失败: {e}")
            if i < retries:
                time.sleep(3 * (i + 1))
    raise last  # type: ignore[misc]


def blob_sha(content: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(content))
    h.update(content)
    return h.hexdigest()


def collect() -> "dict[str, bytes]":
    out: dict[str, bytes] = {}
    for p in sorted(HERE.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(HERE).as_posix()
        if rel.split("/")[0] in SKIP_DIRS or p.name in SKIP_FILES:
            continue
        if p.name.startswith("_"):  # 本地日志/临时文件不进仓库
            continue
        if rel.endswith((".db", ".zip", ".tmp")):
            continue
        out[rel] = p.read_bytes()
    return out


def repo_empty(tok: str) -> bool:
    try:
        c = api(f"https://api.github.com/repos/{REPO}/commits?per_page=1", tok,
                retries=2)
        return not c
    except Exception:  # noqa: BLE001
        return False


def put_file(tok: str, path: str, content: bytes, message: str) -> None:
    """用 Contents API 写单个文件（已存在时自动带上 sha）。"""
    sha = None
    try:
        info = api(f"https://api.github.com/repos/{REPO}/contents/{path}", tok,
                   retries=2)
        sha = info.get("sha")
    except Exception:  # noqa: BLE001  不存在则新建
        sha = None
    payload = {"message": message,
               "content": base64.b64encode(content).decode("ascii")}
    if sha:
        payload["sha"] = sha
    api(f"https://api.github.com/repos/{REPO}/contents/{path}", tok, "PUT",
        payload)


def remote_tree(tok: str) -> "dict[str, str]":
    """远端 branch 上所有文件的 path -> blob sha（空仓库返回 {}）。"""
    try:
        ref = api(f"https://api.github.com/repos/{REPO}/git/ref/heads/{BRANCH}",
                  tok, retries=1)
        sha = ref["object"]["sha"]
        tree = api(f"https://api.github.com/repos/{REPO}/git/trees/{sha}"
                   f"?recursive=1", tok)
        return {t["path"]: t["sha"] for t in tree.get("tree", [])
                if t.get("type") == "blob"}
    except Exception:  # noqa: BLE001  空仓库或取不到 → 视为全量首次提交
        return {}


def main() -> int:
    dry = "--dry-run" in sys.argv
    files = collect()
    log(f"[info] 本地待检查文件 {len(files)} 个")

    tok = token()
    if not tok:
        log("[fail] 未取到 GitHub 令牌：设置环境变量 DALIAN_PRICE_TOKEN "
            "或先在本机完成一次 GitHub 授权（python set_token.py）")
        return 1

    remote = remote_tree(tok)
    log(f"[info] 远端已有文件 {len(remote)} 个")

    changes = {p: c for p, c in files.items()
               if remote.get(p) != blob_sha(c)}
    if not changes:
        log("[done] 无变化，无需提交")
        return 0
    log(f"[info] 将提交 {len(changes)} 个文件"
        + (f"（示例：{list(changes)[:3]}）" if len(changes) > 3 else ""))
    if dry:
        return 0

    try:
        base = api(f"https://api.github.com/repos/{REPO}/git/ref/heads/{BRANCH}",
                   tok, retries=1)["object"]["sha"]
    except Exception:  # noqa: BLE001  可能是空仓库，也可能是网络抖动
        if repo_empty(tok):
            base = None  # 确实是空仓库 → 走初始化分支
        else:
            log("[fail] 无法获取远端 main 分支（网络问题），未提交")
            return 1

    # 空仓库无法直接建 tree（409），先用 Contents API 推一个小文件把 main 分支建起来
    if not base:
        first = "README.md" if "README.md" in changes else sorted(changes)[0]
        put_file(tok, first, changes.pop(first), "init: 初始化仓库")
        log(f"[ok] 已初始化仓库（{first}）")
        base = api(f"https://api.github.com/repos/{REPO}/git/ref/heads/{BRANCH}",
                   tok)["object"]["sha"]

    # 分批提交：单批 payload 过大（3MB+）会被代理掐断（批大小可用 PUSH_BATCH 覆盖）
    items = sorted(changes.items())
    batch = int(os.environ.get("PUSH_BATCH") or 60)
    commits = 0
    for i in range(0, len(items), batch):
        part = items[i:i + batch]
        tree = [{"path": p, "mode": "100644", "type": "blob",
                 "content": c.decode("utf-8")} for p, c in part]
        created = api(f"https://api.github.com/repos/{REPO}/git/trees", tok,
                      "POST", {"tree": tree, "base_tree": base}, retries=4)
        commit = api(f"https://api.github.com/repos/{REPO}/git/commits", tok,
                     "POST", {"message": f"data: {time.strftime('%Y-%m-%d %H:%M')}"
                                         f" （第 {i // batch + 1} 批，{len(part)} 个文件）",
                              "tree": created["sha"], "parents": [base]})
        api(f"https://api.github.com/repos/{REPO}/git/refs/heads/{BRANCH}", tok,
            "PATCH", {"sha": commit["sha"]})
        base = commit["sha"]
        commits += 1
        log(f"[ok] 第 {commits} 批已提交 {commit['sha'][:8]}（{len(part)} 个文件）")
        if i + batch < len(items):  # 批间暂停：连续大 POST 易被代理掐断
            time.sleep(6)
    log(f"[done] 共 {commits} 个提交完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

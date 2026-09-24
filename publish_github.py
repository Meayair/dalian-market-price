#!/usr/bin/env python3
"""把最新数据发布到 GitHub —— 按天 JSON 增量 + Release 全量快照（混合方案）。

仓库布局：
  data/<market>/<date>.json   每日增量天文件（git push 仅几 KB）
  version.json                latest / rows / dates 清单 / snapshot 文件名
  Release 附件                全量 db 快照（仅首次安装用，tag=v<日期>）

- 幂等：重复运行安全（已有天文件跳过；Release 同名附件先删后传）
- 推送失败仅记日志、退出码 1，不阻塞本地入库主流程（次日自动补推）
- 凭据：Windows Git Credential Manager（git credential fill），无明文 token
- --export-only：只导出天文件与 version.json，不做 git/网络操作（用于首次全量导出与测试）
"""
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = Path(r"D:\WorkBuddy\LocalLife\dalian_market_price.db")
VERSION_FILE = HERE / "version.json"
DATA_DIR = HERE / "data"
LOG = HERE / "_publish_log.txt"
# 仓库地址：Meayair/dalian-market-price（可用环境变量 DALIAN_PRICE_REPO 覆盖）
REPO = os.environ.get("DALIAN_PRICE_REPO", "Meayair/dalian-market-price")
# 大陆网络直连 GitHub 常失败：显式走本机代理（可用环境变量 HTTPS_PROXY 覆盖）
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
    or "http://127.0.0.1:10808"
MARKETS = {"farm": "price_farm", "supermarket": "price_supermarket",
           "wholesale": "price_wholesale"}


def install_proxy() -> None:
    """让 urllib 显式走本机代理（直连 GitHub 会被 reset）。"""
    if not PROXY:
        return
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    urllib.request.install_opener(opener)


def log(msg: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {msg}\n")
    except OSError:
        pass
    print(msg)


def fail(msg: str) -> "None":
    log(f"[fail] {msg}")
    sys.exit(1)


def gh_token() -> str:
    # 优先级：环境变量 → 本机 _token.txt → Windows 凭据管理器
    env = os.environ.get("DALIAN_PRICE_TOKEN", "").strip()
    if env:
        return env
    tf = HERE / "_token.txt"
    if tf.exists():
        lines = tf.read_text(encoding="utf-8").strip().splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()
    inp = "protocol=https\nhost=github.com\n\n"
    try:
        r = subprocess.run(["git", "credential", "fill"], input=inp,
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    d = dict(line.split("=", 1) for line in r.stdout.strip().splitlines()
             if "=" in line)
    return d.get("password", "")


def api(url: str, token: str, method: str = "GET", data: bytes | None = None,
        headers: dict | None = None, retries: int = 0):
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "dalian-price-publisher")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=120)
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"[warn] API {method} {url} 第 {i + 1} 次失败: {e}")
            if i < retries:
                time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def git(*args: str) -> str:
    cmd = ["git"]
    if PROXY:  # 让 git 也走代理（自包含，不依赖全局配置）
        cmd += ["-c", f"http.proxy={PROXY}", "-c", f"https.proxy={PROXY}"]
    cmd += list(args)
    r = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip()}")
    return r.stdout.strip()


def export_all(cur: sqlite3.Cursor, force: bool) -> tuple[int, dict, dict, str]:
    """导出全部/缺失的天文件与 version.json。返回 (导出文件数, latest, rows, 总最新日期)。"""
    dates: dict[str, list] = {}
    latest: dict[str, str] = {}
    rows: dict[str, int] = {}
    for m, t in MARKETS.items():
        cur.execute(f"SELECT DISTINCT report_date FROM {t} ORDER BY report_date")
        dates[m] = [r[0] for r in cur.fetchall()]
        latest[m] = dates[m][-1] if dates[m] else ""
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        rows[m] = cur.fetchone()[0]

    exported = 0
    for m, t in MARKETS.items():
        outdir = DATA_DIR / m
        outdir.mkdir(parents=True, exist_ok=True)
        for d in dates[m]:
            f = outdir / f"{d}.json"
            if f.exists() and not force:
                continue
            cur.execute(
                f"SELECT seq, category, commodity, spec, unit, avg_price, "
                f"prev_price, mom_pct FROM {t} WHERE report_date=? ORDER BY seq",
                (d,),
            )
            obj = {
                "market": m,
                "date": d,
                "rows": [
                    {"seq": r[0], "category": r[1], "commodity": r[2],
                     "spec": r[3], "unit": r[4], "avg": r[5], "prev": r[6],
                     "mom": r[7]}
                    for r in cur.fetchall()
                ],
            }
            f.write_text(
                json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            exported += 1

    overall = max(latest.values())
    # 无新数据且元信息未变时不重写 version.json（避免时间戳噪声提交）
    fingerprint = json.dumps({"latest": latest, "rows": rows, "dates": dates},
                             sort_keys=True)
    if VERSION_FILE.exists() and exported == 0:
        try:
            prev = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
            prev_fp = json.dumps(
                {"latest": prev.get("latest"), "rows": prev.get("rows"),
                 "dates": prev.get("dates")}, sort_keys=True)
            if prev_fp == fingerprint:
                return 0, latest, rows, overall
        except Exception:
            pass
    version = {
        "updated_at": datetime.datetime.now().astimezone()
        .isoformat(timespec="seconds"),
        "latest": latest,
        "rows": rows,
        "snapshot": {
            "db_file": f"dalian_market_price_{overall}.db",
            # 直链写进版本文件：客户端无需访问 api.github.com 也能首装（大陆网络友好）
            "url": f"https://github.com/{REPO}/releases/download/v{overall}"
                   f"/dalian_market_price_{overall}.db",
        },
        "dates": dates,
    }
    VERSION_FILE.write_text(
        json.dumps(version, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    return exported, latest, rows, overall


def upload_snapshot(token: str, overall: str, force: bool = False) -> None:
    """上传全量 db 快照为 Release 附件（以远端是否真的存在该附件为准）。"""
    tag = f"v{overall}"
    fname = f"dalian_market_price_{overall}.db"
    release = None
    try:  # 先看该 tag 的 Release 是否已存在
        with api(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
                 token, retries=2) as r:
            release = json.load(r)
        if "id" not in release:
            release = None
    except Exception:  # noqa: BLE001
        release = None
    if not force and release and any(a.get("name") == fname
                                     for a in release.get("assets", [])):
        log(f"[skip] 快照 {fname} 已存在")
        return
    if release is None:
        body = {"tag_name": tag, "target_commitish": "main",
                "name": f"价格数据快照 {overall}",
                "body": f"全量 SQLite 快照（首次安装用），数据截至 {overall}。",
                "draft": False, "prerelease": False}
        try:
            with api(f"https://api.github.com/repos/{REPO}/releases", token,
                     "POST", json.dumps(body).encode("utf-8"),
                     retries=1) as r:
                release = json.load(r)
        except Exception as e:
            fail(f"创建 Release {tag} 失败: {e}")
        log(f"[ok] 已创建 Release {tag}")
    rid = release["id"]
    data = DB.read_bytes()
    up = f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets?name={fname}"
    for attempt in range(3):  # 代理下大文件 POST 偶发 reset，重传前先清同名附件
        for a in release.get("assets", []):
            if a.get("name") == fname:
                try:
                    api(f"https://api.github.com/repos/{REPO}/releases/assets/"
                        f"{a['id']}", token, "DELETE", retries=1).read()
                except Exception:  # noqa: BLE001
                    pass
                release["assets"] = [x for x in release["assets"]
                                     if x.get("name") != fname]
        try:
            api(up, token, "POST", data,
                {"Content-Type": "application/octet-stream"}).read()
            log(f"[ok] 已上传快照 {fname}（{len(data) / 1e6:.1f} MB）")
            return
        except Exception as e:  # noqa: BLE001
            log(f"[warn] 快照上传第 {attempt + 1} 次失败: {e}")
            time.sleep(5)
    fail(f"上传快照失败（{fname}），下次运行自动补传")


def main() -> int:
    if not DB.exists():
        fail(f"数据库不存在: {DB}")
    export_only = "--export-only" in sys.argv
    install_proxy()

    con = sqlite3.connect(str(DB))
    cur = con.cursor()
    exported, latest, rows, overall = export_all(
        cur, force="--force-export" in sys.argv)
    con.close()
    log(f"[ok] 天文件导出：本次新写 {exported} 个（farm={rows['farm']}行, "
        f"supermarket={rows['supermarket']}行, wholesale={rows['wholesale']}行, "
        f"最新={overall}）")
    if export_only:
        log("[done] --export-only 模式，跳过 git/Release")
        return 0

    if "/OWNER/" in REPO:
        fail("请先把 publish_github.py 顶部 REPO 常量（或环境变量 DALIAN_PRICE_REPO）"
             "改成 你的用户名/dalian-market-price")

    token = gh_token()
    if not token:
        fail("未取到 GitHub 凭据：先在本仓库执行一次 git push 完成浏览器授权")

    # 1) 提交天文件与 version.json：走 GitHub Trees API（绕开 git 客户端/GCM）
    r = subprocess.run([sys.executable, str(HERE / "push_via_api.py")],
                       cwd=str(HERE), capture_output=True, text=True)
    tail = (r.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        log(line)
    if r.returncode != 0:
        fail("提交到 GitHub 失败（本地已导出，下次运行自动补推）")

    # 2) Release 全量快照（--force-snapshot: 数据内容变化但日期未变时强制重传）
    upload_snapshot(token, overall, force="--force-snapshot" in sys.argv)
    log(f"[done] 发布完成（最新 {overall}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""抓取抖音博主作品列表（headless Chrome --dump-dom，零第三方依赖）。

用法:
    python lib/fetch_feed.py [--sec-uid SEC_UID] [--out out.json]

输出最后一行 JSON:
    {"ok":true,"user":{...},"items":[{"aweme_id","published_at","ts","title","url"}],"count":N}

设计要点:
- 不用抖音 web API（需 a_bogus 签名，易碎）；用 Chrome 渲染后的 DOM 取 /video/<id>。
- aweme_id 是雪花 ID，高 32 位 = 发布 unix 秒，因此发布时间可从 ID 反推，与列表顺序无关。
- 置顶作品 ID 很旧，判断"新作品"一律按 aweme_id 集合差集，不看位置。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("找不到 Chrome/Edge 可执行文件")


class ChromeBusy(RuntimeError):
    pass


def _acquire_lock(timeout: float = 240.0) -> Path:
    """跨进程互斥：Chrome 复用同一个 --user-data-dir 时，第二个实例会把 dump 落到
    已存在实例的页面上（实测并发时取不到目标页的播放地址）。用锁文件把 dump 串行化。"""
    import time as _time
    DATA.mkdir(parents=True, exist_ok=True)
    lock = DATA / ".chrome.lock"
    start = _time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if _time.time() - lock.stat().st_mtime > 300:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if _time.time() - start > timeout:
                raise ChromeBusy(f"等待 Chrome 锁超过 {timeout:.0f}s（{lock}）")
            _time.sleep(1)


def dump_dom(url: str, budget_ms: int = 25000, profile: Path | None = None) -> str:
    lock = _acquire_lock()
    try:
        return _dump_dom_locked(url, budget_ms, profile)
    finally:
        lock.unlink(missing_ok=True)


def _dump_dom_locked(url: str, budget_ms: int = 25000, profile: Path | None = None) -> str:
    chrome = find_chrome()
    DATA.mkdir(parents=True, exist_ok=True)
    profile = profile or (DATA / "chrome-profile")
    profile.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="dydom_")) / "dom.html"
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run",
        "--disable-extensions", "--mute-audio",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={budget_ms}",
        f"--user-agent={UA}",
        "--dump-dom", url,
    ]
    with open(out, "wb") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.DEVNULL, timeout=180)
    if proc.returncode != 0 and out.stat().st_size == 0:
        raise RuntimeError(f"chrome dump-dom 失败 rc={proc.returncode}")
    html = out.read_text(encoding="utf-8", errors="replace")
    shutil.rmtree(out.parent, ignore_errors=True)
    return html


def _ts_from_aweme_id(aweme_id: str) -> int:
    return int(aweme_id) >> 32


def parse_items(html: str) -> list[dict]:
    """从 DOM 里按出现顺序取 (aweme_id, title)。标题来自卡片的 img alt。"""
    items: list[dict] = []
    seen: dict[str, dict] = {}
    # 全文扫描 /video/<id>（兼容 /video/ID 与 https://www.douyin.com/video/ID 两种写法）
    for m in re.finditer(r'/video/(\d{15,})', html):
        vid = m.group(1)
        if vid in seen:
            continue
        tail = html[m.end(): m.end() + 4000]
        alt = re.search(r'alt="([^"]*)"', tail)
        title = ""
        if alt:
            title = alt.group(1)
            # 去掉 "昵称：" 前缀
            if "\uff1a" in title:
                title = title.split("\uff1a", 1)[1]
        ts = _ts_from_aweme_id(vid)
        item = {
            "aweme_id": vid,
            "ts": ts,
            "published_at": _dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds"),
            "title": title.strip(),
            "url": f"https://www.douyin.com/video/{vid}",
        }
        seen[vid] = item
        items.append(item)
    return items


def fetch(sec_uid: str, budget_ms: int = 25000) -> dict:
    url = f"https://www.douyin.com/user/{sec_uid}"
    html = dump_dom(url, budget_ms=budget_ms)
    if len(html) < 50_000:
        return {"ok": False, "error": "页面过短，疑似被拦或未渲染", "html_len": len(html), "url": url}
    items = parse_items(html)
    if not items:
        return {"ok": False, "error": "DOM 中未找到 /video/ 链接", "html_len": len(html), "url": url}
    nickname = ""
    m = re.search(r"<title>([^<]{1,60}?)的抖音", html)
    if m:
        nickname = m.group(1).strip()
    douyin_no = ""
    m = re.search(r"抖音号：\s*(\d+)", re.sub(r"<[^>]+>", " ", html))
    if m:
        douyin_no = m.group(1)
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {
        "ok": True,
        "fetched_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "user": {"sec_uid": sec_uid, "nickname": nickname, "douyin_id": douyin_no, "profile_url": url},
        "count": len(items),
        "items": items,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec-uid", default="MS4wLjABAAAA_hkYGrUP3d2i3JodCLwrplan05tJ9xl1D6lZySbNC0Q")
    ap.add_argument("--budget-ms", type=int, default=25000)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    try:
        res = fetch(a.sec_uid, budget_ms=a.budget_ms)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if a.out and res.get("ok"):
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

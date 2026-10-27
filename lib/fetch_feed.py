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


#: 登录态 cookie 落盘位置（CDP Network.getAllCookies 导出）。存在即走 CDP 注入流程。
COOKIE_FILE = DATA / "cookies.json"
#: 走家宽出口的本地 CONNECT 代理（lib/local_proxy.py），抖音对海外 IP 只给降级的旧快照。
LOCAL_PROXY = os.environ.get("DOUYIN_PROXY", "http://127.0.0.1:8899")


def dump_dom(url: str, budget_ms: int = 25000, profile: Path | None = None,
             use_cookies: bool = True) -> str:
    lock = _acquire_lock()
    try:
        if use_cookies and COOKIE_FILE.exists():
            try:
                return _cdp_dump(url, budget_ms, profile, COOKIE_FILE)
            except Exception as exc:  # noqa: BLE001
                print(f"[fetch_feed] CDP 注入失败，回退 --dump-dom: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
        return _dump_dom_locked(url, budget_ms, profile)
    finally:
        lock.unlink(missing_ok=True)


def _cdp_dump(url: str, budget_ms: int, profile: Path | None, cookies_path: Path) -> str:
    """起一个独立 profile 的 headless Chrome，用 CDP 注入 cookie 后再导航，取渲染后的 DOM。

    为什么不用 --dump-dom：那条路没法在导航前塞 cookie，只能靠复用已登录的 user-data-dir，
    而那个目录一旦被有头 Chrome 占着就会锁冲突（同一 profile 只能一个实例）。
    登录后作品列表从 20 条变 52 条，所以必须带登录态。
    """
    chrome = find_chrome()
    DATA.mkdir(parents=True, exist_ok=True)
    profile = profile or (DATA / "chrome-cdp")
    profile.mkdir(parents=True, exist_ok=True)
    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    out = DATA / "_cdp_dom.html"
    out.unlink(missing_ok=True)
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run",
        "--disable-extensions", "--mute-audio",
        "--remote-debugging-port=0", "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        f"--user-agent={UA}",
        f"--proxy-server={LOCAL_PROXY}",
        "--proxy-bypass-list=<-loopback>",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port = 0
        import time as _time
        deadline = _time.time() + 40
        while _time.time() < deadline:
            if port_file.exists():
                txt = port_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if txt and txt[0].strip().isdigit():
                    port = int(txt[0].strip())
                    break
            if proc.poll() is not None:
                raise RuntimeError(f"Chrome 提前退出 rc={proc.returncode}")
            _time.sleep(0.3)
        if not port:
            raise RuntimeError("等不到 DevToolsActivePort，Chrome 没起来")
        node = shutil.which("node") or shutil.which("node.exe") or "node"
        settle = str(max(6000, min(20000, budget_ms)))
        cp = subprocess.run(
            [node, str(ROOT / "tools" / "cdp_fetch.js"), str(port),
             str(cookies_path), url, str(out), settle],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        info = (cp.stdout or "").strip().splitlines()
        print(f"[fetch_feed] cdp: {info[-1] if info else cp.stderr[:200]}", file=sys.stderr)
        if not out.exists() or out.stat().st_size < 1000:
            raise RuntimeError(f"CDP 取页面失败: {(cp.stdout or cp.stderr or '')[:300]}")
        return out.read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


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


def _strip_footer(html: str) -> str:
    """砍掉页脚「热门」区块。

    2026-10-27 发现的第二个污染源：主页 <footer page-footer> 里的热门推荐挂着别人的视频
    （7700913167292353125、7701000553211720036 等），它们的链接形如 /video/<id>，
    与作品卡片的链接无法从 URL 上区分，只能整块删除。
    """
    return re.sub(r"<footer\b.*?</footer>", "", html, flags=re.S | re.I)


def parse_items(html: str, owner: str = "") -> list[dict]:
    """从 DOM 里按出现顺序取 (aweme_id, title)，并校验这张卡片到底属于谁。

    2026-10-27 修复（抓串事故）：原实现全文扫 /video/<id> 一律全收，不看卡片归属。
    抖音主页在没有正常渲染时会画出别人的推荐内容，那些链接被当成监控对象的作品
    写进已发现名单，导致"最新 N 条"取到的全是别人的视频。
    现在卡片 img alt 的格式是"昵称：标题"，昵称必须等于 owner 才收；
    拿不到昵称的卡片一律丢弃（宁缺勿错——漏掉一条下次还会再出现，收错一条会污染基线）。
    """
    items: list[dict] = []
    seen: dict[str, dict] = {}
    html = _strip_footer(html)
    for m in re.finditer(r'/video/(\d{15,})', html):
        vid = m.group(1)
        if vid in seen:
            continue
        tail = html[m.end(): m.end() + 4000]
        alt = re.search(r'alt="([^"]*)"', tail)
        title = ""
        card_owner = ""
        if alt:
            raw_alt = alt.group(1)
            if "\uff1a" in raw_alt:
                card_owner, title = raw_alt.split("\uff1a", 1)
                card_owner = card_owner.strip()
        if owner and card_owner != owner:
            continue
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


def fetch(sec_uid: str, budget_ms: int = 25000, expect_nickname: str = "",
          use_cookies: bool = True) -> dict:
    url = f"https://www.douyin.com/user/{sec_uid}"
    html = dump_dom(url, budget_ms=budget_ms, use_cookies=use_cookies)
    if len(html) < 50_000:
        return {"ok": False, "error": "页面过短，疑似被拦或未渲染", "html_len": len(html), "url": url}
    nickname = ""
    m = re.search(r"<title>([^<]{1,60}?)的抖音", html)
    if m:
        nickname = m.group(1).strip()
    # 整页归属校验：标题必须就是被监控的那位，否则整轮作废（不写 state，下一轮重来）
    if expect_nickname:
        if not nickname:
            return {"ok": False, "error": "页面标题里读不到博主昵称，无法确认归属",
                    "html_len": len(html), "url": url}
        if nickname != expect_nickname.strip():
            return {"ok": False, "error": f"页面归属不符：期望「{expect_nickname}」，实际「{nickname}」",
                    "html_len": len(html), "url": url}
    owner = (expect_nickname or nickname).strip()
    items = parse_items(html, owner)
    all_ids = set(re.findall(r'/video/(\d{15,})', _strip_footer(html)))
    if not items:
        return {"ok": False,
                "error": f"页面上 {len(all_ids)} 个视频链接没有一条能确认属于「{owner}」",
                "html_len": len(html), "url": url, "videos_on_page": len(all_ids)}
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
        "videos_on_page": len(all_ids),
        "dropped_foreign": len(all_ids) - len(items),
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
    ap.add_argument("--expect-nickname", default="", help="页面标题必须等于该昵称，否则整轮作废")
    ap.add_argument("--out", default="")
    ap.add_argument("--no-cookies", action="store_true", help="不注入登录态（游客视角，仅诊断用）")
    a = ap.parse_args()
    try:
        res = fetch(a.sec_uid, budget_ms=a.budget_ms, expect_nickname=a.expect_nickname,
                    use_cookies=not a.no_cookies)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if a.out and res.get("ok"):
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

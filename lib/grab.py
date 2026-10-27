"""抓取抖音详情页的真实媒体流地址并下载音轨（CDP 网络监听）。

为什么不用原来的 <source src=".../aweme/v1/play/?..."> 方案：
2026-10-27 实测，部分详情页已改成 MSE + blob: 播放，HTML 里搜不到任何播放地址
（1.18MB 的 DOM 里 play_addr / "uri" / <source> 全部为 0），但页面加载时会向
v26-web.douyinvod.com 拉两条 DASH 流：
    .../media-video-hvc1/...  HEVC 视频，br=250
    .../media-audio-und-mp4a/... AAC 音频，br=45
转写只要语音，直接下音频轨：体积约视频的 1/40（206 秒的片子约 1MB），
PyAV 解 AAC 也比解 HEVC 快得多，本机没有 ffmpeg 也能跑。

用法:
    python lib/grab.py <aweme_id> [--out data/media] [--bind 192.168.1.4]

stdout 末行 JSON:
    {"ok":true,"aweme_id":"...","path":"...m4a","bytes":N,"kind":"audio","title":"..."}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_feed  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = fetch_feed.UA
MIN_BYTES = 20_000


def launch_chrome(profile: Path) -> tuple[subprocess.Popen, int]:
    """起一个 headless Chrome（独立 profile + 家宽代理 + cookie 由 CDP 注入），返回进程与调试端口。"""
    chrome = fetch_feed.find_chrome()
    profile.mkdir(parents=True, exist_ok=True)
    port_file = profile / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-first-run",
        "--disable-extensions", "--mute-audio",
        "--autoplay-policy=no-user-gesture-required",
        "--remote-debugging-port=0", "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        f"--user-agent={UA}",
        f"--proxy-server={fetch_feed.LOCAL_PROXY}",
        "--proxy-bypass-list=<-loopback>",
        "about:blank",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 40
    while time.time() < deadline:
        if port_file.exists():
            lines = port_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if lines and lines[0].strip().isdigit():
                return proc, int(lines[0].strip())
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome 提前退出 rc={proc.returncode}")
        time.sleep(0.3)
    raise RuntimeError("等不到 DevToolsActivePort")


def grab_streams(aweme_id: str, wait_ms: int = 18000, port: int | None = None) -> dict:
    """返回 {ok, title, audio:[url], video:[url]}。port 为空时自己起 Chrome。"""
    url = f"https://www.douyin.com/video/{aweme_id}"
    node = shutil.which("node") or "node"
    profile = DATA / "chrome-grab"
    proc = None
    if port is None:
        proc, port = launch_chrome(profile)
    try:
        if fetch_feed.COOKIE_FILE.exists():
            pass  # cookie 由 cdp_grab 之外注入：见下方 _inject 调用
        cp = subprocess.run(
            [node, str(ROOT / "tools" / "cdp_grab.js"), str(port), url, str(wait_ms)],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="replace")
        out = (cp.stdout or "").strip().splitlines()
        last = out[-1] if out else ""
        try:
            return json.loads(last)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"cdp_grab 输出无法解析: {last[:200]} {cp.stderr[:200]}"}
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass


def _session(bind: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if bind:
        from requests.adapters import HTTPAdapter

        class _Src(HTTPAdapter):
            def init_poolmanager(self, *a, **kw):
                kw["source_address"] = (bind, 0)
                super().init_poolmanager(*a, **kw)

        s.mount("https://", _Src())
        s.mount("http://", _Src())
    return s


def _valid_audio(p: Path) -> bool:
    try:
        if p.stat().st_size < MIN_BYTES:
            return False
        with open(p, "rb") as fh:
            head = fh.read(32)
        return b"ftyp" in head or b"moov" in head
    except OSError:
        return False


def fetch_audio(aweme_id: str, out_dir: Path, bind: str = "", wait_ms: int = 18000,
                force: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{aweme_id}.m4a"
    if not force and _valid_audio(dst):
        return {"ok": True, "aweme_id": aweme_id, "path": str(dst),
                "bytes": dst.stat().st_size, "kind": "audio", "cached": True}

    g = grab_streams(aweme_id, wait_ms=wait_ms)
    if not g.get("ok"):
        return {"ok": False, "aweme_id": aweme_id, "error": f"抓不到媒体流: {g.get('error') or '无 audio/video 请求'}"}
    title = re.sub(r"\s*-\s*抖音\s*$", "", (g.get("title") or "").strip())
    urls = g.get("audio") or []
    if not urls:
        return {"ok": False, "aweme_id": aweme_id, "error": "页面加载了但没有 media-audio 音轨请求",
                "video_count": len(g.get("video") or []), "title": title}

    errors: list[str] = []
    for url in urls[:2]:
        for bind_addr in ([bind] if bind else ["", "192.168.1.4"]):
            sess = _session(bind_addr)
            try:
                with sess.get(url, timeout=90, stream=True, allow_redirects=True) as r:
                    if r.status_code not in (200, 206):
                        errors.append(f"HTTP {r.status_code} bind={bind_addr or 'auto'}")
                        continue
                    tmp = out_dir / f"{aweme_id}.part"
                    n = 0
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(131072):
                            if chunk:
                                fh.write(chunk)
                                n += len(chunk)
                        fh.flush()
                        os.fsync(fh.fileno())
                    tmp.replace(dst)
                if _valid_audio(dst):
                    return {"ok": True, "aweme_id": aweme_id, "path": str(dst), "bytes": n,
                            "kind": "audio", "cached": False, "bind": bind_addr or "auto",
                            "title": title}
                errors.append(f"下载内容不是合法 m4a ({n}B) bind={bind_addr or 'auto'}")
                dst.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc} bind={bind_addr or 'auto'}")
            finally:
                sess.close()
    return {"ok": False, "aweme_id": aweme_id, "error": "音频轨全部失败", "errors": errors[:4]}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("aweme_id")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "media"))
    ap.add_argument("--bind", default="")
    ap.add_argument("--wait-ms", type=int, default=18000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--probe", action="store_true", help="只打印抓到的流地址，不下载")
    a = ap.parse_args()
    try:
        if a.probe:
            res = grab_streams(a.aweme_id, wait_ms=a.wait_ms)
            for k in ("audio", "video"):
                if res.get(k):
                    res[k] = [u[:120] + "..." for u in res[k]]
        else:
            res = fetch_audio(a.aweme_id, Path(a.out_dir), bind=a.bind,
                              wait_ms=a.wait_ms, force=a.force)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "aweme_id": a.aweme_id, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

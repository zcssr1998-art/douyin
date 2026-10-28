"""下载抖音视频原始文件（headless Chrome 抓播放地址 + requests 下载）。

零第三方签名库：视频详情页渲染完成后，DOM 里 <source src="https://www.douyin.com/aweme/v1/play/?..."> 就是可直连的 mp4 地址
（实测 HTTP 206 / video/mp4 / ftypisom 头正常）。yt-dlp 2026.08.19 的 Douyin extractor 会报
"Fresh cookies (not necessarily logged in) are needed"，因此不走 yt-dlp。

用法:
    python lib/download.py <aweme_id> [--out-dir data/media] [--bind 192.168.1.4]

stdout 末行恒为一行 JSON:
    {"ok":true,"aweme_id":"...","path":"...","bytes":15327831,"cached":false}
本机无 ffmpeg，因此不转码不抽音轨，直接把 mp4 交给 transcribe.py（PyAV 解封装）。
"""
from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_feed import dump_dom  # noqa: E402  复用同一套 Chrome 抓取

ROOT = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
MIN_BYTES = 100_000


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


def extract_play_urls(page_html: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'https://www\.douyin\.com/aweme/v1/play/\?[^"\'<>\s]{50,}', page_html):
        u = _html.unescape(m.group(0))
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    # 详情页主播放地址优先（带 PackSourceEnum_AWEME_DETAIL），其次取最长的那个
    out.sort(key=lambda x: ("PackSourceEnum_AWEME_DETAIL" in x, len(x)), reverse=True)
    return out


def extract_title(page_html: str) -> str:
    """详情页 <title> 形如 "大学生找工作网站大全 #找工作#国企 - 抖音"，去掉尾部 " - 抖音" 就是视频标题。"""
    m = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.S)
    if not m:
        return ""
    t = _html.unescape(m.group(1)).strip()
    for suffix in (" - 抖音", "- 抖音"):
        if t.endswith(suffix):
            t = t[: -len(suffix)].strip()
    if t in ("抖音", ""):
        return ""
    return t


def _valid_mp4(p: Path) -> bool:
    try:
        if p.stat().st_size < MIN_BYTES:
            return False
        with open(p, "rb") as fh:
            head = fh.read(16)
        return b"ftyp" in head
    except OSError:
        return False


def download(aweme_id: str, out_dir: Path, bind: str = "", budget_ms: int = 20000,
             force: bool = False) -> dict:
    """优先下 DASH 音频轨（转写只要语音），失败再回退老的整段 mp4 路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 首选路径：CDP 监听网络事件抓 media-audio-und-mp4a 音轨（约视频体积的 1/40）。
    # 2026-10-27 起详情页改成 MSE + blob: 播放，HTML 里已经没有任何播放地址，老路径多数会失败。
    audio_err = ""
    try:
        from grab import fetch_audio

        ga = fetch_audio(aweme_id, out_dir, bind=bind, wait_ms=budget_ms, force=force)
        if ga.get("ok"):
            return ga
        if ga.get("drm"):
            # DRM（CENC）加密的作品：整段 mp4 解不了，旧路径也只会下到同一个加密文件，
            # 直接上报，交给 run_once 标 unsupported，不重试、不浪费带宽。
            return ga
        audio_err = ga.get("error") or "音频轨抓取失败"
    except Exception as exc:  # noqa: BLE001
        audio_err = f"音频轨异常 {type(exc).__name__}: {exc}"

    dst = out_dir / f"{aweme_id}.mp4"
    if not force and _valid_mp4(dst):
        return {"ok": True, "aweme_id": aweme_id, "path": str(dst),
                "bytes": dst.stat().st_size, "cached": True, "kind": "video"}

    page = dump_dom(f"https://www.douyin.com/video/{aweme_id}", budget_ms=budget_ms)
    if len(page) < 20_000:
        return {"ok": False, "aweme_id": aweme_id, "error": f"详情页过短({len(page)}B)，疑似被拦或未渲染"}
    title = extract_title(page)
    urls = extract_play_urls(page)
    if not urls:
        return {"ok": False, "aweme_id": aweme_id,
                "error": f"详情页未找到 aweme/v1/play 播放地址（音频轨也失败：{audio_err}）",
                "audio_error": audio_err,
                "page_bytes": len(page)}

    errors: list[str] = []
    for url in urls[:3]:
        for bind_addr in ([bind] if bind else ["", "192.168.1.4"]):
            sess = _session(bind_addr)
            try:
                with sess.get(url, timeout=60, stream=True, allow_redirects=True) as r:
                    if r.status_code not in (200, 206):
                        errors.append(f"HTTP {r.status_code} bind={bind_addr or 'auto'}")
                        continue
                    tmp = out_dir / f"{aweme_id}.part"
                    n = 0
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_content(262144):
                            if chunk:
                                fh.write(chunk)
                                n += len(chunk)
                        fh.flush()
                        os.fsync(fh.fileno())
                    tmp.replace(dst)
                if _valid_mp4(dst):
                    return {"ok": True, "aweme_id": aweme_id, "path": str(dst), "bytes": n,
                            "cached": False, "bind": bind_addr or "auto", "title": title,
                            "host": r.url.split("/")[2] if hasattr(r, "url") else ""}
                errors.append(f"下载内容非合法 mp4 ({n}B) bind={bind_addr or 'auto'}")
                dst.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc} bind={bind_addr or 'auto'}")
            finally:
                sess.close()
    return {"ok": False, "aweme_id": aweme_id, "error": "全部播放地址失败",
            "audio_error": audio_err, "errors": errors[:5]}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("aweme_id")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "media"))
    ap.add_argument("--bind", default="")
    ap.add_argument("--budget-ms", type=int, default=20000)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        res = download(a.aweme_id, Path(a.out_dir), bind=a.bind, budget_ms=a.budget_ms, force=a.force)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "aweme_id": a.aweme_id, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

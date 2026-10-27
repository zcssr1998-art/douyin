"""抖音博主监控 · 发布到 GitHub。

把一条作品的分析结果写成 markdown 存进仓库 posts/ 目录，同时（可选）把音频副本
一起提交，最后 git commit + git push 到远端。

用法:
    python lib/publish_github.py --payload outbox/<id>.payload.json --media data/media/<id>.m4a --transcript data/transcripts/<id>.json [--repo-dir 仓库根] [--no-push] [--no-media]

stdout 最后一行是 JSON：
    {"ok": true, "aweme_id": "...", "files": [...], "commit": "abc1234", "pushed": true, "url": "https://github.com/..."}

设计约束:
- 幂等：文件内容没变就不提交（避免每轮都产生空 commit）。
- push 失败不算内容丢失：commit 已经落地，下一轮 push 会把它带上去。
- 音频与逐字稿都留在公开仓库里，视频被平台下架后仍可复现。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BT = chr(96)  # 反引号，用来拼 markdown 代码块

SECTIONS = [
    ("core", "核心观点"),
    ("evidence", "具体论据"),
    ("conclusion", "最终结论"),
    ("details", "关键细节"),
    ("judgement", "独立判断"),
]


def log(msg: str) -> None:
    print("[" + _dt.datetime.now().strftime("%H:%M:%S") + "] " + msg, flush=True)


def _run(argv: list, cwd: Path, timeout: int = 180) -> tuple:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"   # 不让 Git Credential Manager 弹窗（无人值守会卡死）
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "超时 %ds: %s" % (timeout, " ".join(argv))


def _slug_title(title: str, limit: int = 60) -> str:
    t = (title or "").replace("\r", " ").replace("\n", " ")
    # 标题里 "#话题 #标签" 对文件名没意义，截到第一个话题标签之前
    if " #" in t:
        t = t.split(" #")[0]
    for ch in '\\/:*?"<>|#':
        t = t.replace(ch, " ")
    t = " ".join(t.split())
    return t[:limit].strip() or "无标题"


def _fmt_publish(publish_time: str, aweme_id: str) -> tuple:
    """返回 (日期 YYYY-MM-DD, 展示时间)。缺发布时间时按 aweme_id 高 32 位反算。"""
    pt = (publish_time or "").strip()
    if len(pt) >= 10:
        return pt[:10], pt
    try:
        ts = int(aweme_id) >> 32
        dt = _dt.datetime.fromtimestamp(ts, _dt.timezone(_dt.timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown", pt or "（未记录）"


def build_markdown(payload: dict, transcript, media_name) -> str:
    sid = str(payload.get("aweme_id") or "")
    title = (payload.get("title") or "").strip() or "（无标题）"
    nickname = (payload.get("nickname") or "").strip()
    url = (payload.get("video_url") or "").strip()
    day, shown = _fmt_publish(payload.get("publish_time") or "", sid)
    ana = payload.get("analysis") or {}
    fetched = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    L = []
    L.append("# " + title)
    L.append("")
    L.append("| 项 | 内容 |")
    L.append("|---|---|")
    L.append("| 博主 | %s |" % (nickname or "（未记录）"))
    L.append("| 发布时间 | %s |" % shown)
    L.append("| 原视频 | %s |" % (url or "（未记录）"))
    L.append("| 视频 ID | %s |" % sid)
    L.append("| 抓取时间 | %s |" % fetched)
    if media_name:
        L.append("| 音频存档 | [%s](media/%s) |" % (media_name, media_name))
    L.append("")
    L.append("> 视频可能随时被平台下架，本文的逐字稿与音频是在线时抓取的存档。")
    L.append("")

    for key, heading in SECTIONS:
        body = (ana.get(key) or "").strip()
        L.append("## " + heading)
        L.append("")
        L.append(body if body else "_（模型未返回该字段）_")
        L.append("")

    L.append("## 逐字稿（语音转写全文）")
    L.append("")
    if not transcript:
        L.append("_（未找到逐字稿文件）_")
    else:
        text = (transcript.get("text") or "").strip()
        L.append("_转写引擎：faster-whisper（本地离线）｜语种：%s｜时长：%s 秒｜字符数：%d_"
                 % (transcript.get("language") or "?", transcript.get("duration"), len(text)))
        L.append("")
        if transcript.get("incomplete"):
            L.append("> WARNING 转写不完整（覆盖率 %s），缺失部分未做任何补全。"
                     % transcript.get("coverage"))
            L.append("")
        L.append(BT * 3 + "text")
        L.append(text if text else "（空）")
        L.append(BT * 3)
    L.append("")
    return "\n".join(L)


def update_index(posts_dir: Path) -> str:
    """重建 posts/README.md 索引表：按文件名（日期-ID）倒序。"""
    rows = []
    for md in sorted(posts_dir.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        title = md.stem
        shown = md.stem
        for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("| 发布时间 |"):
                parts = line.split("|")
                if len(parts) > 2:
                    shown = parts[2].strip()
            if title != md.stem and shown != md.stem:
                break
        rows.append((md.name, shown, title))
    rows.sort(key=lambda r: r[0], reverse=True)

    L = ["# 抖音博主更新存档", "",
         "监控对象：**艾丽的无废话财经**（抖音号 606890417）",
         "主页：https://www.douyin.com/user/MS4wLjABAAAA_hkYGrUP3d2i3JodCLwrplan05tJ9xl1D6lZySbNC0Q", "",
         "每条视频一个 markdown：本地语音转写的逐字稿 + 五段式观点分析 + 音频存档。",
         "视频可能随时被平台下架，这里是下架前的存档。", "",
         "| 发布时间 | 标题 | 文件 |", "|---|---|---|"]
    for name, shown, title in rows:
        safe = title.replace("|", "\\|")
        L.append("| %s | %s | [%s](%s) |" % (shown, safe, name, name))
    L.append("")
    L.append("共 %d 条。由自动监控脚本生成，每 3 分钟检查一次是否更新。" % len(rows))
    L.append("")
    return "\n".join(L)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--media", default=None)
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--repo-dir", default=None)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-media", action="store_true")
    a = ap.parse_args()

    repo_dir = Path(a.repo_dir).resolve() if a.repo_dir else ROOT
    payload = json.loads(Path(a.payload).read_text(encoding="utf-8"))
    sid = str(payload.get("aweme_id") or "").strip()
    if not sid:
        print(json.dumps({"ok": False, "error": "payload 里没有 aweme_id"}, ensure_ascii=False))
        return 2

    transcript = None
    if a.transcript and Path(a.transcript).exists():
        try:
            transcript = json.loads(Path(a.transcript).read_text(encoding="utf-8"))
        except Exception as exc:
            log("逐字稿读取失败，仍继续: %s" % exc)

    posts_dir = repo_dir / "posts"
    media_dir = posts_dir / "media"
    posts_dir.mkdir(parents=True, exist_ok=True)

    media_name = None
    if a.media and not a.no_media and Path(a.media).exists():
        src = Path(a.media)
        if src.stat().st_size < 10_000:
            log("音频过小，跳过存档: %s" % src)
        else:
            media_dir.mkdir(parents=True, exist_ok=True)
            dst = media_dir / (sid + src.suffix.lower())
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
                log("音频存档 %s（%d 字节）" % (dst.name, dst.stat().st_size))
            media_name = dst.name

    day, _shown = _fmt_publish(payload.get("publish_time") or "", sid)
    slug = _slug_title(payload.get("title") or "")
    md_path = posts_dir / ("%s-%s-%s.md" % (day, sid, slug))
    md = build_markdown(payload, transcript, media_name)
    changed = True
    if md_path.exists() and md_path.read_text(encoding="utf-8") == md:
        changed = False
        log("markdown 无变化: %s" % md_path.name)
    else:
        md_path.write_text(md, encoding="utf-8")
        log("写入 %s（%d 字符）" % (md_path.name, len(md)))

    idx = posts_dir / "README.md"
    idx_text = update_index(posts_dir)
    if not idx.exists() or idx.read_text(encoding="utf-8") != idx_text:
        idx.write_text(idx_text, encoding="utf-8")
        changed = True
        log("更新索引 posts/README.md")

    rel = str(posts_dir.relative_to(repo_dir))
    result = {"ok": False, "aweme_id": sid,
              "files": [str(md_path.relative_to(repo_dir))],
              "commit": None, "pushed": False, "url": None}

    rc, out = _run(["git", "add", "--", rel], repo_dir)
    if rc != 0:
        result["error"] = "git add 失败: " + out[-400:]
        print(json.dumps(result, ensure_ascii=False))
        return 1

    rc, _out = _run(["git", "diff", "--cached", "--quiet"], repo_dir)
    if rc == 0:
        log("没有需要提交的变更")
        result.update(ok=True, pushed=None, note="no change")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    msg = "add: %s %s (#%s)" % (day, (payload.get("title") or "无标题")[:50], sid)
    rc, out = _run(["git", "-c", "user.name=douyin-monitor",
                    "-c", "user.email=douyin-monitor@users.noreply.github.com",
                    "commit", "-m", msg], repo_dir)
    if rc != 0 and "nothing to commit" not in out:
        result["error"] = "git commit 失败: " + out[-400:]
        print(json.dumps(result, ensure_ascii=False))
        return 1
    rc2, out2 = _run(["git", "rev-parse", "--short", "HEAD"], repo_dir)
    result["commit"] = out2.strip() if rc2 == 0 else None
    log("提交 %s: %s" % (result["commit"], msg))

    if a.no_push:
        result.update(ok=True, pushed=False, note="--no-push")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # 本机 git 默认凭据助手是 Git Credential Manager，无人值守时会挂在弹窗上不返回；
    # 显式改用 gh 的凭据助手（token 存在系统 keyring 里，不落盘、不进日志）。
    rc, out = _run(["git", "-c", "credential.helper=",
                    "-c", "credential.helper=!gh auth git-credential",
                    "push", "origin", "HEAD:main"], repo_dir, timeout=600)
    if rc != 0:
        rc, out2 = _run(["git", "push", "origin", "HEAD:main"], repo_dir, timeout=600)
        if rc == 0:
            out = out2
    if rc != 0:
        result["error"] = "git push 失败（commit 已落地，下一轮重试）: " + out[-400:]
        print(json.dumps(result, ensure_ascii=False))
        return 1

    rc, out2 = _run(["git", "remote", "get-url", "origin"], repo_dir)
    remote = out2.strip()
    for suf in (".git",):
        if remote.endswith(suf):
            remote = remote[: -len(suf)]
    result.update(ok=True, pushed=True,
                  url=(remote + "/blob/main/posts/" + md_path.name) if remote else None)
    log("push 完成")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

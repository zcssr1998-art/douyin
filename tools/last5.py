# -*- coding: utf-8 -*-
"""对最近 N 条作品跑完整链路（下载→转写→分析），再做一次跨条目综合总结。

不改 data/state.json，不影响监控任务的状态；产物全部落 data/last5/。
用法: python tools/last5.py [N] [bind_ip]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = ROOT / "data" / "last5"
for _sub in ("media", "transcripts", "analyses"):
    (OUT / _sub).mkdir(parents=True, exist_ok=True)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
BIND = sys.argv[2] if len(sys.argv) > 2 else ""

BASE = os.environ.get("DS_ANALYZE_BASE_URL", "http://127.0.0.1:8769/v1").rstrip("/")
MODEL = os.environ.get("DS_ANALYZE_MODEL", "cline-pass/deepseek-v4.1-flash")


def log(msg: str) -> None:
    print("[" + time.strftime("%H:%M:%S") + "] " + msg, flush=True)


def run(argv: list[str], timeout: int = 1800) -> tuple[dict, int, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable] + argv, cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    data: dict = {}
    for line in reversed((p.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return data, p.returncode, (p.stderr or "")[-500:]


def chat(messages: list[dict], max_tokens: int = 4000) -> str:
    import requests
    body = {"model": MODEL, "messages": messages, "temperature": 0.3,
            "max_tokens": max_tokens, "stream": False}
    r = requests.post(BASE + "/chat/completions", json=body, timeout=300,
                      headers={"Authorization": "Bearer local"})
    r.raise_for_status()
    j = r.json()
    return (j["choices"][0]["message"].get("content") or "").strip()


def load_rows() -> list[tuple[str, dict]]:
    """优先用 DS_IDS_FILE 指定的作品列表（登录态下抓到的真实列表），否则回退 state.json。"""
    ids_file = os.environ.get("DS_IDS_FILE", "").strip()
    if ids_file:
        p = Path(ids_file)
        if not p.is_absolute():
            p = ROOT / p
        raw = json.loads(p.read_text(encoding="utf-8"))
        items = raw.get("items") if isinstance(raw, dict) else raw
        rows: list[tuple[str, dict]] = []
        for it in items or []:
            if isinstance(it, str):
                sid, title = it, ""
            else:
                sid = str(it.get("aweme_id") or it.get("vid") or "")
                title = it.get("title") or it.get("alt") or ""
            if not sid:
                continue
            ts = int(sid) >> 32
            rows.append((sid, {"ts": ts,
                               "title": title,
                               "published_at": time.strftime("%Y-%m-%d %H:%M",
                                                             time.localtime(ts))}))
        rows.sort(key=lambda kv: kv[1]["ts"], reverse=True)
        return rows[:N]
    state = json.loads((ROOT / "data" / "state.json").read_text(encoding="utf-8"))
    return sorted(state.get("items", {}).items(),
                  key=lambda kv: kv[1].get("ts") or 0, reverse=True)[:N]


def main() -> int:
    rows = load_rows()
    log(f"取最近 {len(rows)} 条，bind={BIND or '(默认)'}")

    results = []
    for idx, (sid, it) in enumerate(rows, 1):
        t0 = time.time()
        rec = {"aweme_id": sid,
               "published_at": it.get("published_at") or "",
               "url": "https://www.douyin.com/video/" + sid,
               "title": ""}
        log(f"[{idx}/{len(rows)}] {sid} 下载…")
        dl, rc, err = run(["lib/download.py", sid, "--out-dir", str(OUT / "media"),
                           "--bind", BIND])
        media = dl.get("path") or dl.get("media") or dl.get("file") or dl.get("output") or ""
        rec["title"] = (dl.get("title") or it.get("title") or "").strip()
        if not media:
            cands = sorted((OUT / "media").glob(sid + "*"),
                           key=lambda p: p.stat().st_size, reverse=True)
            media = str(cands[0]) if cands else ""
        if not media or not Path(media).exists():
            rec["error"] = "下载失败: " + (dl.get("error") or err or "no media")
            log("  下载失败 " + str(rec["error"])[:120])
            results.append(rec)
            continue
        rec["media_bytes"] = Path(media).stat().st_size

        log("  转写…")
        tout = OUT / "transcripts" / (sid + ".json")
        tr, rc, err = run(["lib/transcribe.py", media, "--out", str(tout)])
        rec["transcript_chars"] = int(tr.get("chars") or len(tr.get("text") or ""))
        rec["incomplete"] = bool(tr.get("incomplete"))
        if not tout.exists():
            rec["error"] = "转写失败: " + (tr.get("error") or err or "no out file")
            log("  转写失败 " + str(rec["error"])[:120])
            results.append(rec)
            continue

        log("  分析…")
        aout = OUT / "analyses" / (sid + ".json")
        meta = json.dumps({"published_at": rec["published_at"], "video_url": rec["url"]},
                          ensure_ascii=False)
        an, rc, err = run(["lib/analyze.py", "--transcript", str(tout),
                           "--title", rec["title"], "--meta", meta, "--out", str(aout)])
        body = an.get("analysis") if isinstance(an.get("analysis"), dict) else an
        rec["core"] = (body.get("core") or "").strip()
        rec["conclusion"] = (body.get("conclusion") or "").strip()
        rec["evidence"] = (body.get("evidence") or "").strip()
        rec["details"] = (body.get("details") or "").strip()
        rec["judgement"] = (body.get("judgement") or body.get("judgment") or "").strip()
        if not rec["core"]:
            rec["error"] = "分析失败: " + (an.get("error") or err or "empty core")
        rec["secs"] = round(time.time() - t0, 1)
        log(f"  完成 {rec['secs']}s chars={rec['transcript_chars']}")
        results.append(rec)

    ok = [r for r in results if r.get("core")]
    summary = ""
    if ok:
        blocks = []
        for r in ok:
            blocks.append("\n".join([
                "### " + (r.get("published_at") or "") + "  " + (r.get("title") or "(无标题)"),
                "链接: " + r["url"],
                "核心观点: " + r["core"],
                "主要论据: " + r["evidence"],
                "结论和立场: " + r["conclusion"],
                "关键细节: " + r["details"],
                "独立判断: " + r["judgement"],
            ]))
        sys_p = ("你是财经内容分析助手。用户给你同一位抖音博主最近几条视频的逐条分析，"
                 "每条分析都基于该视频的语音转写。请写一份中文综合总结。")
        usr = ("下面是 " + str(len(ok)) + " 条逐条分析：\n\n" + "\n\n".join(blocks) +
               "\n\n请输出 markdown，严格包含这些小节：\n"
               "## 一句话概括\n"
               "## 逐条速览（每条一行：发布时间 + 主题 + 一句话主张）\n"
               "## 这几条的主线与内在逻辑\n"
               "## 前后是否一致 / 有无矛盾或立场变化\n"
               "## 对普通人的可执行结论\n"
               "## 需要警惕的地方（哪些是预测、哪些缺乏证据）\n\n"
               "要求：不吹捧、不编造；原始转写里没有的数字、公司名、日期不要补；"
               "严格区分【事实】【博主观点】【预测/未证实】。若某条转写不完整要说明。")
        try:
            summary = chat([{"role": "system", "content": sys_p},
                            {"role": "user", "content": usr}])
            log("综合总结完成 " + str(len(summary)) + " 字")
        except Exception as exc:
            summary = "综合总结失败: " + repr(exc)[:300]
            log(summary)

    md = ["# 最近 " + str(len(results)) + " 条视频 · 综合分析",
          "",
          "生成时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
          ""]
    if summary:
        md += [summary, ""]
    md += ["---", "", "## 逐条原始分析", ""]
    for r in results:
        md += ["### " + (r.get("published_at") or "") + "  " + (r.get("title") or "(无标题)"),
               "",
               "- 原视频: " + r["url"],
               "- 转写字数: " + str(r.get("transcript_chars", 0)),
               "- 核心观点: " + (r.get("core") or r.get("error") or ""),
               "- 主要论据: " + (r.get("evidence") or ""),
               "- 结论和立场: " + (r.get("conclusion") or ""),
               "- 关键细节: " + (r.get("details") or ""),
               "- 独立判断: " + (r.get("judgement") or ""),
               ""]
    (OUT / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")

    payload = {"ok": len(ok) == len(results) and bool(summary),
               "count": len(results), "ok_count": len(ok),
               "summary_md": str(OUT / "SUMMARY.md"),
               "items": results}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
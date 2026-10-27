"""补齐失败条目并重建综合总结。

用法: python tools/resummary.py [N]
- 读 data/state.json 最近 N 条
- 对 analyses/<id>.json 缺失或 core 为空的条目重跑 lib/analyze.py（复用已有逐字稿）
- 用全部成功条目重新生成综合总结，重写 SUMMARY.md 的综合段与逐条段
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "last5"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "lib"))
import last5 as L5  # noqa: E402


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    state = json.loads((ROOT / "data" / "state.json").read_text(encoding="utf-8"))
    rows = sorted(state.get("items", {}).items(),
                  key=lambda kv: kv[1].get("ts") or 0, reverse=True)[:N]

    md_path = OUT / "SUMMARY.md"
    md = md_path.read_text(encoding="utf-8")
    marker = "## 逐条原始分析"
    head, tail = md.split(marker, 1)
    tail = marker + tail

    blocks: list[dict] = []
    for sid, it in rows:
        url = "https://www.douyin.com/video/" + sid
        ap = OUT / "analyses" / (sid + ".json")
        data: dict = {}
        if ap.exists():
            try:
                data = json.loads(ap.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        body = data.get("analysis") if isinstance(data.get("analysis"), dict) else data
        if not (body.get("core") or "").strip():
            log("重跑分析 " + sid)
            tp = OUT / "transcripts" / (sid + ".json")
            if not tp.exists():
                log("  无逐字稿，跳过")
                continue
            title = (it.get("title") or "").strip()
            meta = json.dumps({"published_at": it.get("published_at") or "",
                               "video_url": url}, ensure_ascii=False)
            p = subprocess.run([sys.executable, "lib/analyze.py", "--transcript", str(tp),
                                "--title", title, "--meta", meta, "--out", str(ap)],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
            for line in reversed((p.stdout or "").strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            body = data.get("analysis") if isinstance(data.get("analysis"), dict) else data
        core = (body.get("core") or "").strip()
        log("  " + sid + " core=" + str(len(core)))
        blocks.append({"aweme_id": sid,
                       "published_at": it.get("published_at") or "",
                       "url": url,
                       "core": core,
                       "evidence": (body.get("evidence") or "").strip(),
                       "conclusion": (body.get("conclusion") or "").strip(),
                       "details": (body.get("details") or "").strip(),
                       "judgement": (body.get("judgement") or body.get("judgment") or "").strip()})

    ok = [b for b in blocks if b["core"]]
    log("成功条目 %d/%d" % (len(ok), len(blocks)))

    parts = []
    for r in ok:
        parts.append("\n".join([
            "### " + r["published_at"] + "  " + (r["url"] and "") ,
        ]))
    # 逐条原始分析段：用新结果替换对应块，保留原块标题
    for r in blocks:
        anchor = "- 原视频: " + r["url"]
        pos = tail.find(anchor)
        if pos < 0:
            continue
        start = tail.rfind("### ", 0, pos)
        end = tail.find("\n### ", pos)
        if end < 0:
            end = len(tail)
        header = tail[start:tail.find("\n", start)].rstrip()
        newblock = "\n".join([
            header, "",
            "- 原视频: " + r["url"],
            "- 核心观点: " + (r["core"] or "（本条分析失败）"),
            "- 主要论据: " + r["evidence"],
            "- 结论和立场: " + r["conclusion"],
            "- 关键细节: " + r["details"],
            "- 独立判断: " + r["judgement"],
            "",
        ])
        tail = tail[:start] + newblock + tail[end:].lstrip("\n")

    summary = ""
    if ok:
        chunks = []
        for r in ok:
            chunks.append("\n".join([
                "### " + r["published_at"] + "  " + r["url"],
                "核心观点: " + r["core"],
                "主要论据: " + r["evidence"],
                "结论和立场: " + r["conclusion"],
                "关键细节: " + r["details"],
                "独立判断: " + r["judgement"],
            ]))
        sys_p = ("你是财经内容分析助手。用户给你同一位抖音博主最近几条视频的逐条分析，"
                 "每条分析都基于该视频的语音转写。请写一份中文综合总结。")
        usr = ("下面是 " + str(len(ok)) + " 条逐条分析：\n\n" + "\n\n".join(chunks) +
               "\n\n请输出 markdown，严格包含这些小节：\n"
               "## 一句话概括\n"
               "## 逐条速览（每条一行：发布时间 + 主题 + 一句话主张）\n"
               "## 这几条的主线与内在逻辑\n"
               "## 前后是否一致 / 有无矛盾或立场变化\n"
               "## 对普通人的可执行结论\n"
               "## 需要警惕的地方（哪些是预测、哪些缺乏证据）\n\n"
               "要求：不吹捧、不编造；原始转写里没有的数字、公司名、日期不要补；"
               "严格区分【事实】【博主观点】【预测/未证实】。若某条转写不完整要说明。"
               "逐条速览必须覆盖全部 " + str(len(ok)) + " 条，一条都不能漏。")
        try:
            summary = L5.chat([{"role": "system", "content": sys_p},
                               {"role": "user", "content": usr}])
        except Exception as exc:  # noqa: BLE001
            summary = "综合总结失败: " + repr(exc)[:300]
        log("综合总结 " + str(len(summary)) + " 字")

    new_head = ("# 最近 " + str(len(blocks)) + " 条视频 · 综合分析\n\n"
                "生成时间: " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n\n"
                + summary + "\n\n---\n\n")
    md_path.write_text(new_head + tail, encoding="utf-8")
    print(json.dumps({"ok": len(ok) == len(blocks), "total": len(blocks), "ok_count": len(ok),
                      "md": str(md_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""抖音博主监控 · 单次执行入口（由 Windows 计划任务每 N 分钟调用一次）。

职责：拉作品列表 → 用 aweme_id 集合差集识别新作品 → 按阶段推进
      下载 → 转写 → DeepSeek 分析 → 发布到 GitHub 仓库（posts/ 下 markdown + 音频存档）
并把每一步的结果原子写入 data/state.json，进程被杀/重启后从断点继续。

用法:
    python lib/run_once.py                 # 正常一轮
    python lib/run_once.py --baseline      # 只建立去重基线，不推送任何历史作品
    python lib/run_once.py --simulate-new <aweme_id>   # 强制把某条当新作品跑全链（验收用）
    python lib/run_once.py --status        # 只打印 state 概况

设计约束（都来自实测）:
- 新作品判定只看 aweme_id 集合差集，不看列表位置（置顶视频 ID 很旧，位置判断必错）。
- 每阶段完成即落盘，避免"转写完但没推送"这类状态丢失导致重复通知。
- 同一条最多重试 poll.max_attempts_per_item 次，超过标 failed 停止自动重试（防无限重试）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = Path(__file__).resolve().parent
sys.path.insert(0, str(LIB))


def _ensure_streams() -> None:
    """计划任务用 pythonw 启动时 sys.stdout / sys.stderr 是 None，任何 print 都会抛异常。
    这里把它们接到 logs/ 下的文件，保证无人值守运行时不会因为一条 print 就整轮失败。"""
    d = ROOT / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
        if sys.stdout is None:
            sys.stdout = open(d / "stdout.log", "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(d / "stderr.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        pass


_ensure_streams()

STAGES = ["discovered", "downloaded", "transcribed", "analyzed", "notified"]
LOCK_NAME = ".run_once.lock"
LOCK_STALE_SEC = 1800


def acquire_run_lock() -> bool:
    """单实例锁：计划任务上一轮还没跑完时，新的一轮直接退出，避免两份进程并发写 state。"""
    p = ROOT / "data" / LOCK_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            age = time.time() - p.stat().st_mtime
            if age > LOCK_STALE_SEC:
                log(f"发现过期锁（{int(age)}s），清理后继续")
                p.unlink(missing_ok=True)
            else:
                log(f"上一轮仍在运行（锁 {int(age)}s），本轮跳过")
                return False
        except OSError:
            pass
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        log("抢锁失败（并发），本轮跳过")
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_run_lock() -> None:
    (ROOT / "data" / LOCK_NAME).unlink(missing_ok=True)


def _parse_iso(v: str) -> float | None:
    try:
        return _dt.datetime.fromisoformat(v).timestamp()
    except (TypeError, ValueError):
        return None


def _cutoff_ts(st: dict) -> float:
    """新作品的时间下限（unix 秒）。早于它的一律算历史作品。

    为什么必须有这一刀：headless Chrome 每次渲染出的卡片数量会抖动（虚拟列表），
    上一轮没露出来的老卡片这一轮露出来，会被"不在 state 里"误判成新作品。
    用 aweme_id 反推出的发布时间来卡，与列表位置完全无关。
    """
    refs = [t for t in (_parse_iso(st.get("prev_fetch_at") or ""),
                        _parse_iso(st.get("last_fetch_at") or ""),
                        _parse_iso(st.get("baseline_at") or "")) if t]
    base = max(refs) if refs else time.time()
    return base - 1800


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _id_ts(aweme_id: str) -> int:
    """抖音 aweme_id 是雪花 ID：int(id) >> 32 就是发布时间的 unix 秒。"""
    try:
        ts = int(aweme_id) >> 32
    except (TypeError, ValueError):
        return 0
    return ts if 946684800 <= ts <= 4102444800 else 0


def _iso_from_ts(ts: int) -> str:
    if not ts:
        return ""
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{_dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        d = ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "run_once.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    with open(ROOT / "config.json", encoding="utf-8") as fh:
        return json.load(fh)


def state_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["state"]


def load_state(cfg: dict) -> dict:
    p = state_path(cfg)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as fh:
                st = json.load(fh)
            if isinstance(st, dict) and isinstance(st.get("items"), dict):
                return st
        except (OSError, json.JSONDecodeError) as exc:
            log(f"state 读取失败，按空状态处理: {exc}")
    return {"version": 1, "baseline_at": None, "items": {}}


def save_state(cfg: dict, st: dict) -> None:
    p = state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(p)


def run_module(argv: list[str], timeout: int = 1800) -> tuple[dict, int]:
    """跑 lib 下某个子模块，取 stdout 最后一行 JSON。"""
    log("RUN " + " ".join(argv))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable] + argv, cwd=str(ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    out = (proc.stdout or "").strip()
    data: dict = {}
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if proc.returncode != 0 and not data:
        data = {"ok": False, "error": f"exit={proc.returncode}", "stderr": (proc.stderr or "")[-800:]}
    return data, proc.returncode


def notify_payload(item: dict, analysis: dict) -> dict:
    # analyze.py 把五段放在 analysis.analysis 子对象里（顶层还有 ok/model/usage/raw），
    # 这里要下钻一层，否则推出去全是空字段。
    body = analysis.get("analysis")
    if not isinstance(body, dict):
        body = analysis
    return {
        "aweme_id": item["aweme_id"],
        "nickname": item.get("nickname") or "",
        "title": item.get("title") or "",
        "publish_time": item.get("published_at") or "",
        "video_url": item.get("url") or f"https://www.douyin.com/video/{item['aweme_id']}",
        "analysis": {
            "core": body.get("core", ""),
            "evidence": body.get("evidence", ""),
            "conclusion": body.get("conclusion", ""),
            "judgement": body.get("judgement") or body.get("judgment") or "",
            "details": body.get("details", ""),
        },
    }


def advance(item: dict, cfg: dict) -> dict:
    """把一条作品按当前 stage 往前推一步。返回更新后的 item（调用方负责落盘）。"""
    stage = item.get("stage", "discovered")
    aweme_id = item["aweme_id"]
    pc = cfg["paths"]

    media_dir = ROOT / cfg["download"]["dir"]
    # 媒体产物可能有两种：.m4a（CDP 抓的 DASH 音频轨，首选）或 .mp4（老的整段视频回退路径）
    media_file = None
    for ext in (".m4a", ".mp4"):
        cand = media_dir / f"{aweme_id}{ext}"
        if cand.exists() and cand.stat().st_size > 100_000:
            media_file = cand
            break
    if stage == "discovered" and media_file is not None:
        return {**item, "stage": "downloaded", "media": str(media_file),
                "media_bytes": media_file.stat().st_size, "last_error": None}

    tr_file = ROOT / pc["transcripts"] / f"{aweme_id}.json"
    if stage == "downloaded" and tr_file.exists():
        return {**item, "stage": "transcribed", "transcript": str(tr_file), "last_error": None}

    an_file = ROOT / pc["analyses"] / f"{aweme_id}.json"
    if stage == "transcribed" and an_file.exists():
        return {**item, "stage": "analyzed", "analysis": str(an_file), "last_error": None}

    if stage == "discovered":
        argv = [str(LIB / "download.py"), aweme_id, "--out-dir", str(media_dir)]
        if cfg["download"].get("bind_source_address"):
            argv += ["--bind", cfg["download"]["bind_source_address"]]
        res, _ = run_module(argv, timeout=600)
        if not res.get("ok"):
            return {**item, "last_error": f"download: {res.get('error')}", "attempts": item.get("attempts", 0) + 1}
        title = item.get("title") or res.get("title") or ""
        return {**item, "stage": "downloaded", "media": res["path"], "media_bytes": res.get("bytes"),
                "title": title, "last_error": None}

    if stage == "downloaded":
        out = ROOT / pc["transcripts"] / f"{aweme_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        argv = [str(LIB / "transcribe.py"), item["media"], "--out", str(out)]
        if cfg["asr"].get("model_dir"):
            argv += ["--model-dir", cfg["asr"]["model_dir"]]
        if cfg["asr"].get("language"):
            argv += ["--language", cfg["asr"]["language"]]
        res, _ = run_module(argv, timeout=3600)
        if not res.get("ok"):
            return {**item, "last_error": f"transcribe: {res.get('error')}", "attempts": item.get("attempts", 0) + 1}
        text = res.get("text") or ""
        if len(text.strip()) < 20:
            return {**item, "last_error": f"transcribe: 逐字稿过短({len(text)}字)", "attempts": item.get("attempts", 0) + 1}
        return {**item, "stage": "transcribed", "transcript": str(out), "transcript_chars": len(text),
                "incomplete": bool(res.get("incomplete")), "last_error": None}

    if stage == "transcribed":
        out = ROOT / pc["analyses"] / f"{aweme_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        meta = {"author": item.get("nickname"), "video_id": aweme_id, "platform": "douyin",
                "published_at": item.get("published_at"), "url": item.get("url"),
                "transcript_incomplete": item.get("incomplete", False)}
        argv = [str(LIB / "analyze.py"), "--transcript", item["transcript"],
                "--title", item.get("title") or "", "--meta", json.dumps(meta, ensure_ascii=False),
                "--out", str(out)]
        res, _ = run_module(argv, timeout=900)
        if not res.get("ok"):
            return {**item, "last_error": f"analyze: {res.get('error')}", "attempts": item.get("attempts", 0) + 1}
        return {**item, "stage": "analyzed", "analysis": str(out), "last_error": None}

    if stage == "analyzed":
        with open(item["analysis"], encoding="utf-8") as fh:
            analysis = json.load(fh)
        payload = notify_payload(item, analysis)
        pfile = ROOT / pc["outbox"] / f"{aweme_id}.payload.json"
        pfile.parent.mkdir(parents=True, exist_ok=True)
        pfile.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

        pub = cfg.get("publish") or {}
        target = (pub.get("target") or "github").lower()

        # 主路径：发布到 GitHub 仓库（markdown + 逐字稿 + 音频存档）
        if target == "github" and pub.get("enabled", True):
            argv = [str(LIB / "publish_github.py"), "--payload", str(pfile)]
            if item.get("media"):
                argv += ["--media", item["media"]]
            if item.get("transcript"):
                argv += ["--transcript", item["transcript"]]
            if pub.get("repo_dir"):
                argv += ["--repo-dir", str(pub["repo_dir"])]
            if pub.get("push") is False:
                argv += ["--no-push"]
            if pub.get("upload_media") is False:
                argv += ["--no-media"]
            res, _ = run_module(argv, timeout=900)
            if not res.get("ok"):
                return {**item, "last_error": f"publish: {res.get('error')}",
                        "attempts": item.get("attempts", 0) + 1}
            return {**item, "stage": "notified", "notified_at": now_iso(),
                    "published_url": res.get("url"), "commit": res.get("commit"),
                    "publish_note": res.get("note"), "last_error": None}

        # 备选路径：老的 Discord webhook
        argv = [str(LIB / "notify.py"), "--payload", str(pfile)]
        res, _ = run_module(argv, timeout=600)
        if not res.get("ok"):
            return {**item, "last_error": f"notify: {res.get('error') or res.get('http_status')}",
                    "attempts": item.get("attempts", 0) + 1}
        if res.get("dry_run"):
            # dry-run 不算"已送达"：保留 analyzed 阶段，凭据到位后自动补发。
            # 不累加 attempts —— 缺凭据是配置问题，不该把作品判成 failed。
            return {**item, "stage": "analyzed", "dry_run": True,
                    "last_error": "dry_run: 缺 Discord 凭据，仅落盘 outbox",
                    "attempts": item.get("attempts", 0)}
        return {**item, "stage": "notified", "notified_at": now_iso(),
                "message_ids": res.get("message_ids"), "chunks": res.get("chunks"), "last_error": None}

    return item


def build_item(raw: dict, cfg: dict) -> dict:
    return {
        "aweme_id": raw["aweme_id"],
        "ts": raw.get("ts") or _id_ts(raw["aweme_id"]),
        "title": raw.get("title") or "",
        "published_at": raw.get("published_at") or _iso_from_ts(_id_ts(raw["aweme_id"])),
        "url": raw.get("url") or f"https://www.douyin.com/video/{raw['aweme_id']}",
        "nickname": (cfg.get("user") or {}).get("nickname", ""),
        "stage": "discovered",
        "first_seen": now_iso(),
        "attempts": 0,
        "last_error": None,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="只建立去重基线")
    ap.add_argument("--simulate-new", default=None, help="把指定 aweme_id 强制当新作品跑全链")
    ap.add_argument("--status", action="store_true", help="只打印 state 概况")
    ap.add_argument("--max-items", type=int, default=0)
    ap.add_argument("--no-fetch", action="store_true", help="不拉列表，只推进已记录作品的未完成阶段")
    a = ap.parse_args()

    cfg = load_config()

    if a.status:
        st = load_state(cfg)
        items = st.get("items", {})
        by_stage: dict[str, int] = {}
        for it in items.values():
            by_stage[it.get("stage", "?")] = by_stage.get(it.get("stage", "?"), 0) + 1
        print(json.dumps({"ok": True, "baseline_at": st.get("baseline_at"), "total": len(items),
                          "by_stage": by_stage}, ensure_ascii=False))
        return 0

    if not acquire_run_lock():
        print(json.dumps({"ok": True, "skipped": "another run in progress"}, ensure_ascii=False))
        return 0
    try:
        return _main_locked(a, cfg)
    finally:
        release_run_lock()


def _main_locked(a, cfg) -> int:
    st = load_state(cfg)

    summary: dict = {"ok": True, "started_at": now_iso(), "new": [], "advanced": [], "errors": []}

    # ---- 1) 拉列表 ----
    feed: dict = {"ok": False, "error": "skipped"}
    if not a.no_fetch:
        import fetch_feed  # noqa: E402
        try:
            feed = fetch_feed.fetch(cfg["user"]["sec_uid"],
                                    budget_ms=cfg["fetch"].get("budget_ms", 25000),
                                    expect_nickname=(cfg.get("user") or {}).get("nickname", ""))
        except Exception as exc:  # noqa: BLE001
            feed = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        summary["feed"] = {"ok": feed.get("ok"), "count": feed.get("count"), "error": feed.get("error")}
        if not feed.get("ok"):
            log(f"列表拉取失败: {feed.get('error')}")
            st["last_fetch_error"] = {"at": now_iso(), "error": feed.get("error")}
            save_state(cfg, st)
            summary["ok"] = False
            print(json.dumps(summary, ensure_ascii=False))
            return 0  # 单次失败不当作致命，交给下一轮
        st.pop("last_fetch_error", None)
        st["prev_fetch_at"] = st.get("last_fetch_at")
        st["last_fetch_at"] = now_iso()

    # ---- 2) 基线 / 新作品识别 ----
    items = st.setdefault("items", {})
    if feed.get("ok") and not st.get("baseline_at"):
        for raw in feed["items"]:
            items[raw["aweme_id"]] = {**build_item(raw, cfg), "stage": "baseline"}
        st["baseline_at"] = now_iso()
        st["user"] = feed.get("user")
        save_state(cfg, st)
        summary["baseline"] = {"created_at": st["baseline_at"], "seeded": len(feed["items"])}
        log(f"已建立基线，共 {len(feed['items'])} 条历史作品（不推送）")
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    if a.simulate_new:
        sid = a.simulate_new
        meta = {"aweme_id": sid, "title": "", "published_at": "", "url": f"https://www.douyin.com/video/{sid}"}
        if feed.get("ok"):
            for raw in feed["items"]:
                if raw["aweme_id"] == sid:
                    meta = raw
                    break
        old = items.get(sid) or {}
        # 保留已有产物与标题：--simulate-new 只重置阶段，不该把已下载/已转写/已分析的成果与标题抹掉
        keep_keys = ("title", "published_at", "media", "media_bytes", "transcript",
                     "transcript_chars", "incomplete", "analysis")
        keep = {k: old[k] for k in keep_keys if old.get(k)}
        items[sid] = {**build_item(meta, cfg), **keep, "simulated": True,
                      "first_seen": old.get("first_seen") or now_iso()}
        log(f"模拟新作品: {sid}（stage 重置为 discovered）")
        summary["simulated"] = sid

    if feed.get("ok") and st.get("baseline_at"):
        known = set(items)
        cutoff = _cutoff_ts(st)
        fresh: list[dict] = []
        stale: list[dict] = []
        for raw in feed["items"]:
            if raw["aweme_id"] in known:
                continue
            if (raw.get("ts") or 0) >= cutoff:
                fresh.append(raw)
            else:
                stale.append(raw)
        # 抓取窗口抖动会露出未记录的老卡片：补进基线，绝不当新作品推送
        for raw in stale:
            items[raw["aweme_id"]] = {**build_item(raw, cfg), "stage": "baseline"}
            log(f"补录历史作品 {raw['aweme_id']} {raw.get('published_at')}（早于监控起点，不推送）")
        summary["backfilled"] = [r["aweme_id"] for r in stale]
        fresh.sort(key=lambda r: r["ts"])  # 旧→新
        for raw in fresh:
            items[raw["aweme_id"]] = build_item(raw, cfg)
            summary["new"].append(raw["aweme_id"])
            log(f"发现新作品 {raw['aweme_id']} {raw.get('published_at')} {raw.get('title')[:30]}")
        if fresh or stale:
            save_state(cfg, st)

    # ---- 3) 推进未完成阶段 ----
    max_items = a.max_items or cfg["poll"].get("max_items_per_run", 3)
    max_attempts = cfg["poll"].get("max_attempts_per_item", 5)
    todo = [it for it in items.values() if it.get("stage") in ("discovered", "downloaded", "transcribed", "analyzed")]
    todo.sort(key=lambda it: (0 if it.get("simulated") else 1, it.get("ts") or 0))
    pending_left = 0
    done = 0
    for it in todo:
        if done >= max_items:
            pending_left += 1
            continue
        if it.get("attempts", 0) >= max_attempts:
            if it.get("stage") != "failed":
                it["stage_prev"] = it.get("stage")
                it["stage"] = "failed"
                log(f"{it['aweme_id']} 超过 {max_attempts} 次重试，标 failed")
                save_state(cfg, st)
            continue
        cur = it
        for _ in range(5):  # 一条作品一次调用里可以连推多阶段，最多 5 跳
            nxt = advance(cur, cfg)
            items[cur["aweme_id"]] = nxt
            save_state(cfg, st)
            if nxt.get("dry_run"):
                summary.setdefault("dry_run", []).append(cur["aweme_id"])
                log(f"{cur['aweme_id']} 已分析完成，缺 Discord 凭据 → 落盘 outbox 待补发")
                break
            if nxt.get("last_error"):
                summary["errors"].append({"aweme_id": cur["aweme_id"], "stage": nxt.get("stage"),
                                          "error": nxt.get("last_error")})
                log(f"{cur['aweme_id']} {nxt.get('stage')} 失败: {nxt.get('last_error')}")
                break
            cur = nxt
            if cur.get("stage") == "notified":
                summary["advanced"].append(cur["aweme_id"])
                log(f"{cur['aweme_id']} 全链完成并已送达")
                break
        done += 1

    summary["pending_left"] = pending_left
    summary["totals"] = {"tracked": len(items),
                         "notified": sum(1 for x in items.values() if x.get("stage") == "notified"),
                         "failed": sum(1 for x in items.values() if x.get("stage") == "failed")}
    summary["finished_at"] = now_iso()
    save_state(cfg, st)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/notify.py — 抖音博主监控项目的 Discord 推送模块。

三种后端，按优先级自动选择：
  1) webhook  : 环境变量 DISCORD_WEBHOOK_URL 或 config.json 的 discord.webhook_url
  2) bot      : 环境变量 DISCORD_BOT_TOKEN + config.json 的 discord.channel_id
  3) dry_run  : 上面两者都缺时，把最终消息写到 data/outbox/<aweme_id>.txt

所有出站请求走本地代理 http://127.0.0.1:7890（本机 7897 未监听，别改）。
凭据只从环境变量 / config.json 读取，源码里没有任何硬编码。
日志与 stdout 只打印脱敏后的目标（前 30 字符 + ***）。

CLI:
    python lib/notify.py --payload <json文件>
    python lib/notify.py --self-test
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests
except Exception as exc:  # pragma: no cover
    requests = None
    _REQUESTS_IMPORT_ERROR = repr(exc)
else:
    _REQUESTS_IMPORT_ERROR = ""

BASE_DIR = Path(__file__).resolve().parent.parent          # douyin-monitor/
DATA_DIR = BASE_DIR / "data"
OUTBOX_DIR = DATA_DIR / "outbox"
LOG_DIR = BASE_DIR / "logs"
DEFAULT_CONFIG = BASE_DIR / "config.json"
DEFAULT_LOG = LOG_DIR / "notify.log"

PROXY = "http://127.0.0.1:7890"
DISCORD_HARD_LIMIT = 2000      # Discord 单条硬上限
CHUNK_BUDGET = 1900            # 自己的软上限，留余量给序号与 emoji
MAX_429_RETRIES = 3
MAX_5XX_RETRIES = 2
HTTP_TIMEOUT = 25

HEADER_TAG = "【抖音博主更新】"
SECTION_TITLES = ("核心观点", "主要论据", "结论和立场", "DeepSeek 独立判断")
PLACEHOLDER_RE = re.compile(
    r"^(|none|null|nil|todo|tbd|xxx+|your[_-].*|<.*>|put[_-].*|placeholder.*|changeme|待填.*)$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _init_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def mask(secret: str | None) -> str:
    """脱敏：只保留前 30 字符，其余打星。日志里任何凭据目标都必须过这个函数。"""
    if not secret:
        return "<unset>"
    s = str(secret)
    return (s[:30] + "***") if len(s) > 30 else (s + "***")


def log(msg: str, log_path: Path | None = None) -> None:
    """追加一行日志；写盘失败不影响主流程。"""
    path = log_path or DEFAULT_LOG
    line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _usable(value) -> str:
    """过滤空值与占位符（config.json 里留空/写 PUT_XXX_HERE 都视为未配置）。"""
    v = "" if value is None else str(value).strip()
    if not v or PLACEHOLDER_RE.match(v):
        return ""
    return v


def load_config(path: Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        log(f"config 不存在，按空配置继续: {p}")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log(f"config 解析失败，按空配置继续: {p} ({exc})")
        return {}


# --------------------------------------------------------------------------
# 后端判定
# --------------------------------------------------------------------------
def resolve_backend(cfg: dict) -> tuple[str, dict]:
    """
    返回 (backend, params)。

    判定顺序（与任务书一致）：
      webhook 有值            -> webhook
      否则 bot_token+channel  -> bot
      否则                    -> dry_run
    """
    dcfg = cfg.get("discord") or {}
    if not isinstance(dcfg, dict):
        dcfg = {}

    env_hook = _usable(os.environ.get("DISCORD_WEBHOOK_URL"))
    cfg_hook = _usable(dcfg.get("webhook_url"))
    webhook = env_hook or cfg_hook
    hook_src = "env:DISCORD_WEBHOOK_URL" if env_hook else ("config:discord.webhook_url" if cfg_hook else "")

    env_token = _usable(os.environ.get("DISCORD_BOT_TOKEN"))
    cfg_token = _usable(dcfg.get("bot_token"))
    token = env_token or cfg_token
    token_src = "env:DISCORD_BOT_TOKEN" if env_token else ("config:discord.bot_token" if cfg_token else "")

    channel = _usable(dcfg.get("channel_id")) or _usable(os.environ.get("DISCORD_CHANNEL_ID"))

    if webhook:
        if not webhook.lower().startswith(("http://", "https://")):
            log(f"webhook 值不是 URL，忽略 (source={hook_src}, value={mask(webhook)})")
        else:
            return "webhook", {"webhook_url": webhook, "source": hook_src}

    if token and channel:
        api_base = _usable(dcfg.get("api_base")) or "https://discord.com/api/v10"
        return "bot", {"token": token, "channel_id": channel, "api_base": api_base,
                       "source": f"{token_src}+config:channel_id"}

    missing = []
    if not (token and channel):
        missing.append("DISCORD_WEBHOOK_URL 或 (DISCORD_BOT_TOKEN + discord.channel_id)")
    return "dry_run", {"reason": "未配置任何可用凭据", "missing": missing, "channel_seen": bool(channel)}


# --------------------------------------------------------------------------
# payload 归一化
# --------------------------------------------------------------------------
def pick(d: dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def fmt_time(value) -> str:
    if value in (None, "", 0):
        return "（未提供）"
    if isinstance(value, bool):
        return "（未提供）"
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(value)
    s = str(value).strip()
    if re.fullmatch(r"\d{10}|\d{13}", s):
        return fmt_time(int(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    # 带毫秒/时区的 ISO 兜底
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def _as_text(value, bullet: bool = False) -> str:
    if value in (None, "", [], {}):
        return "（未提供）"
    if isinstance(value, str):
        return value.strip() or "（未提供）"
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if str(x).strip()]
        if not items:
            return "（未提供）"
        prefix = "- " if bullet else ""
        return "\n".join(prefix + it if not it.startswith(("-", "•", "*", "1.")) else it for it in items)
    if isinstance(value, dict):
        return "\n".join(f"- {k}：{v}" for k, v in value.items())
    return str(value).strip() or "（未提供）"


def normalize_payload(payload: dict) -> dict:
    """把上游（转写+DeepSeek 分析）送来的各种字段名归一化。"""
    payload = payload if isinstance(payload, dict) else {}

    nickname = pick(payload, "nickname", "author_nickname", "author", "blogger", "user_name", "博主")
    title = pick(payload, "title", "video_title", "desc", "description", "标题")
    aweme_id = str(pick(payload, "aweme_id", "video_id", "item_id", "id", "vid", default="")).strip()
    publish_time = fmt_time(pick(payload, "publish_time", "create_time", "published_at",
                                  "create_time_ms", "time", "发布时间"))

    video_url = str(pick(payload, "video_url", "url", "share_url", default="")).strip()
    if not video_url or "douyin.com" not in video_url:
        vid = aweme_id or str(pick(payload, "video_id", default="")).strip()
        video_url = f"https://www.douyin.com/video/{vid}" if vid else "（未提供）"
    if not aweme_id:
        m = re.search(r"/video/(\d+)", video_url)
        aweme_id = m.group(1) if m else "unknown"

    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        analysis = {}

    def sec(*names, bullet=False, fallback_key=""):
        for src in (analysis, payload):
            v = pick(src, *names, default="")
            if v not in ("", None, [], {}):
                return _as_text(v, bullet=bullet)
        if fallback_key:
            v = pick(payload, fallback_key, default="")
            if v not in ("", None, [], {}):
                return _as_text(v, bullet=bullet)
        return "（未提供）"

    sections = {
        "核心观点": sec("core", "core_view", "main_point", "核心观点", fallback_key="core_view"),
        "主要论据": sec("evidence", "arguments", "points", "主要论据", bullet=True, fallback_key="evidence"),
        "结论和立场": sec("stance", "conclusion", "position", "结论和立场", fallback_key="conclusion"),
        "DeepSeek 独立判断": sec("judgment", "judgement", "independent_judgment", "独立判断", "DeepSeek 独立判断",
                                fallback_key="judgment"),
    }

    return {
        "aweme_id": aweme_id or "unknown",
        "nickname": str(nickname or "（未知博主）").strip(),
        "title": str(title or "（无标题）").strip(),
        "publish_time": publish_time,
        "video_url": video_url,
        "sections": sections,
    }


# --------------------------------------------------------------------------
# 消息构造与分段
# --------------------------------------------------------------------------
def header_lines(meta: dict) -> list[str]:
    return [
        HEADER_TAG,
        f"博主：{meta['nickname']}",
        f"视频标题：{meta['title']}",
        f"发布时间：{meta['publish_time']}",
        f"原视频：{meta['video_url']}",
    ]


def block_texts(meta: dict) -> list[str]:
    return [f"**{t}：**\n{meta['sections'][t]}" for t in SECTION_TITLES]


def build_full_text(meta: dict) -> str:
    return "\n".join(header_lines(meta)) + "\n\n" + "\n\n".join(block_texts(meta))


def _split_line(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    out, buf = [], ""
    for seg in re.split(r"(?<=[。！？；!?;])", line):
        if not seg:
            continue
        if len(buf) + len(seg) <= limit:
            buf += seg
            continue
        if buf:
            out.append(buf)
            buf = ""
        while len(seg) > limit:
            out.append(seg[:limit])
            seg = seg[limit:]
        buf = seg
    if buf:
        out.append(buf)
    return out or [line[:limit]]


def _atomize(text: str, limit: int) -> list[str]:
    """把一段（可能超长）文本拆成若干 <= limit 的原子块，优先在换行/句号处断开。"""
    limit = max(limit, 200)
    if len(text) <= limit:
        return [text]
    units: list[str] = []
    cur = ""
    for line in text.split("\n"):
        for piece in _split_line(line, limit):
            if cur and len(cur) + 1 + len(piece) > limit:
                units.append(cur)
                cur = piece
            else:
                cur = piece if not cur else cur + "\n" + piece
    if cur:
        units.append(cur)
    return units


def split_message(meta: dict) -> list[str]:
    """
    返回要依次发送的消息列表。
    单条能塞下就原样返回 1 条；超长按段落边界切多条，每条都保留完整头部，
    并在标题行标注 (i/n)。
    """
    full = build_full_text(meta)
    if len(full) <= CHUNK_BUDGET:
        return [full]

    header = header_lines(meta)
    reserve = 12                                  # 给 "（1/10）" 之类留位
    budget = CHUNK_BUDGET - reserve
    header_len = len("\n".join(header)) + 2       # 头部 + 空行
    body_budget = max(budget - header_len, 400)

    units: list[str] = []
    for block in block_texts(meta):
        units.extend(_atomize(block, body_budget))

    pages: list[list[str]] = []
    cur: list[str] = []
    cur_len = header_len
    for u in units:
        add = len(u) if not cur else len(u) + 2
        if cur and cur_len + add > budget:
            pages.append(cur)
            cur = [u]
            cur_len = header_len + len(u)
        else:
            cur.append(u)
            cur_len += add
    if cur:
        pages.append(cur)

    total = len(pages)
    out: list[str] = []
    for idx, page in enumerate(pages, 1):
        head = list(header)
        head[0] = f"{HEADER_TAG}({idx}/{total})"
        out.append("\n".join(head) + "\n\n" + "\n\n".join(page))
    return out


# --------------------------------------------------------------------------
# HTTP 发送（含重试）
# --------------------------------------------------------------------------
def _proxies() -> dict:
    return {"http": PROXY, "https": PROXY}


def _retry_after(resp) -> float:
    try:
        body = resp.json()
        if isinstance(body, dict) and "retry_after" in body:
            return max(float(body["retry_after"]), 0.5)
    except Exception:
        pass
    hdr = resp.headers.get("Retry-After") if resp is not None else None
    if hdr:
        try:
            return max(float(hdr), 0.5)
        except ValueError:
            pass
    return 2.0


def post_message(url: str, content: str, *, token: str | None, label: str,
                 proxy: str = PROXY, timeout: int = HTTP_TIMEOUT) -> dict:
    """
    发一条消息。返回 {ok, message_id, http_status, error, attempts}。
    429 按 retry_after 等待重试；5xx 退避重试 2 次；4xx 直接报错不重试。
    """
    if requests is None:
        return {"ok": False, "message_id": None, "http_status": None,
                "error": f"requests 不可用: {_REQUESTS_IMPORT_ERROR}", "attempts": 0}

    headers = {"Content-Type": "application/json", "User-Agent": "douyin-monitor/1.0"}
    if token:
        headers["Authorization"] = f"Bot {token}"
    body = {"content": content, "allowed_mentions": {"parse": []}}

    n429 = n5xx = 0
    attempts = 0
    while True:
        attempts += 1
        use_proxy = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = requests.post(url, json=body, headers=headers,
                                 proxies=use_proxy,
                                 timeout=timeout)
        except Exception as exc:
            return {"ok": False, "message_id": None, "http_status": None,
                    "error": f"{type(exc).__name__}: {exc}", "attempts": attempts}

        status = resp.status_code
        if status in (200, 201, 204):
            mid = None
            try:
                data = resp.json()
                if isinstance(data, dict):
                    mid = data.get("id")
            except Exception:
                mid = None
            log(f"发送成功 {label} status={status} attempts={attempts} id={mid}")
            return {"ok": True, "message_id": mid, "http_status": status,
                    "error": None, "attempts": attempts}

        if status == 429:
            if n429 >= MAX_429_RETRIES:
                log(f"429 超过重试上限 {label}")
                return {"ok": False, "message_id": None, "http_status": status,
                        "error": "429 rate limited (retries exhausted)", "attempts": attempts}
            wait = min(_retry_after(resp), 60.0)
            n429 += 1
            log(f"429 限流 {label}，等待 {wait:.1f}s 后重试 ({n429}/{MAX_429_RETRIES})")
            time.sleep(wait)
            continue

        if 500 <= status < 600:
            if n5xx >= MAX_5XX_RETRIES:
                return {"ok": False, "message_id": None, "http_status": status,
                        "error": f"server error {status} (retries exhausted)", "attempts": attempts}
            n5xx += 1
            backoff = 2 ** n5xx
            log(f"{status} 服务端错误 {label}，退避 {backoff}s 重试 ({n5xx}/{MAX_5XX_RETRIES})")
            time.sleep(backoff)
            continue

        # 4xx 及其它：不重试
        snippet = ""
        try:
            snippet = resp.text[:300]
        except Exception:
            pass
        return {"ok": False, "message_id": None, "http_status": status,
                "error": f"http {status}: {snippet}", "attempts": attempts}


def _with_wait(url: str) -> str:
    """webhook 加 ?wait=true 才能拿到 message id。"""
    if "wait=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "wait=true"


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
def notify(payload: dict, *, cfg: dict | None = None, config_path: Path | None = None,
           force_dry_run: bool = False, outbox_dir: Path | None = None,
           log_path: Path | None = None, proxy: str = PROXY) -> dict:
    cfg = cfg if cfg is not None else load_config(config_path)
    meta = normalize_payload(payload)
    parts = split_message(meta)
    backend, params = resolve_backend(cfg)
    if force_dry_run:
        backend, params = "dry_run", {"reason": "force_dry_run"}

    log(f"notify start aweme_id={meta['aweme_id']} backend={backend} parts={len(parts)} "
        f"lens={[len(p) for p in parts]}")

    # ---- dry-run：落盘到 data/outbox/<aweme_id>.txt ----
    if backend == "dry_run":
        target_dir = Path(outbox_dir) if outbox_dir else OUTBOX_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / f"{meta['aweme_id']}.txt"
        body = "\n\n".join(
            f"===== part {i}/{len(parts)} =====\n{p}" for i, p in enumerate(parts, 1)
        ) if len(parts) > 1 else parts[0]
        header = (f"[dry_run] 未真实送达 Discord —— 仅落盘本地。\n"
                  f"[dry_run] reason: {params.get('reason')}\n"
                  f"[dry_run] 需要 DISCORD_WEBHOOK_URL（或 DISCORD_BOT_TOKEN + discord.channel_id）才能真实送达。\n"
                  f"[dry_run] 生成时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                  f"{'-' * 60}\n")
        out_path.write_text(header + body + "\n", encoding="utf-8")
        log(f"dry_run 落盘 {out_path.name} parts={len(parts)} bytes={out_path.stat().st_size}")
        return {
            "ok": True,
            "backend": "dry_run",
            "sent": 0,
            "message_ids": [],
            "dry_run": True,
            "chunks": len(parts),
            "written_files": [str(out_path)],
            "note": "未真实送达；仅写入本地 outbox。需要 DISCORD_WEBHOOK_URL 或 (DISCORD_BOT_TOKEN + discord.channel_id)。",
        }

    # ---- 真实发送 ----
    if backend == "webhook":
        url = _with_wait(params["webhook_url"])
        label = f"webhook:{mask(params['webhook_url'])}"
        token = None
    else:
        api_base = params.get("api_base") or "https://discord.com/api/v10"
        url = f"{api_base.rstrip('/')}/channels/{params['channel_id']}/messages"
        label = f"bot:channel={params['channel_id']}"
        token = params["token"]

    sent = 0
    ids: list = []
    for i, part in enumerate(parts, 1):
        res = post_message(url, part, token=token, label=f"{label} part={i}/{len(parts)}", proxy=proxy)
        if not res["ok"]:
            return {
                "ok": False,
                "backend": backend,
                "sent": sent,
                "message_ids": ids,
                "dry_run": False,
                "chunks": len(parts),
                "error": res["error"],
                "http_status": res["http_status"],
                "failed_part": i,
            }
        sent += 1
        if res.get("message_id"):
            ids.append(res["message_id"])
        if i < len(parts):
            time.sleep(1.0)      # 温和限速

    return {
        "ok": True,
        "backend": backend,
        "sent": sent,
        "message_ids": ids,
        "dry_run": False,
        "chunks": len(parts),
    }


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------
SELFTEST_PAYLOAD = {
    "aweme_id": "7412345678901234567",
    "nickname": "张三说设计",
    "title": "AI 出图到底能不能替代原画？我拆了三种工作流",
    "publish_time": 1761552000,
    "analysis": {
        "core": "作者认为 AI 出图现阶段无法替代原画，但会吃掉 70% 的重复劳动，尤其是基础细化与批量变体。",
        "evidence": [
            "案例一：某游戏外包组把概念阶段的外包单量从 40 张砍到 12 张，但精修工时反而上升。",
            "案例二：作者自己用三种工作流各跑 30 张，只有一条能在 3 轮内出可用稿。",
            "数据：12 家外包商里 9 家已经要求画师自备 AI 流程。",
        ],
        "stance": "立场偏保守：工具可以省时间，但审美判断、结构理解、客户沟通这三件事仍然在人身上。",
        "judgment": "判断：作者的结论方向成立，但样本量偏小（30 张/3 工作流），对'替代率'的估计不可直接引用；"
                    "更站得住的说法是'流程重组'而非'岗位替代'。此处补充一段超长内容用于验证分段逻辑——"
                    + "关于工具链的边界，需要区分生成、精修、审核三个阶段各自的成本结构，"
                    * 3,
    },
}


def selftest() -> dict:
    _init_stdout()
    cfg = load_config()
    backend, params = resolve_backend(cfg)
    print(f"[self-test] config: {DEFAULT_CONFIG} exists={DEFAULT_CONFIG.exists()}")
    print(f"[self-test] 后端判定: {backend}  source={params.get('source') or params.get('reason')}")
    if backend == "webhook":
        print(f"[self-test] webhook(脱敏): {mask(params.get('webhook_url'))}")
    elif backend == "bot":
        print(f"[self-test] token(脱敏): {mask(params.get('token'))}  channel={params.get('channel_id')}")
    print(f"[self-test] 环境变量: DISCORD_WEBHOOK_URL={'SET' if _usable(os.environ.get('DISCORD_WEBHOOK_URL')) else 'UNSET'}"
          f"  DISCORD_BOT_TOKEN={'SET' if _usable(os.environ.get('DISCORD_BOT_TOKEN')) else 'UNSET'}")

    meta = normalize_payload(SELFTEST_PAYLOAD)
    full = build_full_text(meta)
    parts = split_message(meta)
    print(f"[self-test] 完整消息 {len(full)} 字符 -> 切成 {len(parts)} 条，各条长度 {[len(p) for p in parts]}")
    for i, p in enumerate(parts, 1):
        assert len(p) <= DISCORD_HARD_LIMIT, f"第 {i} 条超 2000 字符"
    assert all(p.startswith(HEADER_TAG) for p in parts), "存在缺头部的分段"
    print("[self-test] 分段断言通过：每条 <=2000 字符，且都带头部标识")

    # 超长单段落也验证一次
    long_payload = json.loads(json.dumps(SELFTEST_PAYLOAD, ensure_ascii=False))
    long_payload["analysis"]["core"] = "这是一段用于压测分段逻辑的超长单段落。" * 120
    lp_parts = split_message(normalize_payload(long_payload))
    print(f"[self-test] 超长单段落用例 -> {len(lp_parts)} 条，max={max(len(p) for p in lp_parts)} 字符")
    assert max(len(p) for p in lp_parts) <= DISCORD_HARD_LIMIT

    print("[self-test] ---- 即将真实走一遍 notify() ----")
    result = notify(SELFTEST_PAYLOAD)
    print("[self-test] 前 200 字符预览:\n" + parts[0][:200].replace("\n", " | "))
    return result


def main(argv: list[str] | None = None) -> int:
    _init_stdout()
    ap = argparse.ArgumentParser(description="抖音监控 → Discord 推送")
    ap.add_argument("--payload", help="payload JSON 文件路径")
    ap.add_argument("--self-test", action="store_true", help="用内置样例跑一次")
    ap.add_argument("--force-dry-run", action="store_true", help="强制 dry-run（只落盘不发）")
    ap.add_argument("--config", help="config.json 路径（默认项目根 config.json）")
    ap.add_argument("--outbox", help="dry-run 输出目录（默认 data/outbox）")
    ap.add_argument("--proxy", default=PROXY, help=f"出站代理，默认 {PROXY}")
    args = ap.parse_args(argv)

    cfg_path = Path(args.config) if args.config else None

    if args.self_test:
        result = selftest()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if not args.payload:
        result = {"ok": False, "backend": None, "error": "缺少 --payload 或 --self-test",
                  "http_status": None, "dry_run": False}
        print(json.dumps(result, ensure_ascii=False))
        return 2

    p = Path(args.payload)
    if not p.exists():
        result = {"ok": False, "backend": None, "error": f"payload 文件不存在: {p}",
                  "http_status": None, "dry_run": False}
        print(json.dumps(result, ensure_ascii=False))
        return 2
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        result = {"ok": False, "backend": None, "error": f"payload JSON 解析失败: {exc}",
                  "http_status": None, "dry_run": False}
        print(json.dumps(result, ensure_ascii=False))
        return 2

    result = notify(payload, config_path=cfg_path, force_dry_run=args.force_dry_run,
                    outbox_dir=Path(args.outbox) if args.outbox else None, proxy=args.proxy)
    if result.get("dry_run"):
        print(f"[dry-run] 未真实送达 Discord，已写本地 outbox: {result.get('written_files')}")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

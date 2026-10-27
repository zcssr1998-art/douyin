#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib/analyze.py  -  DeepSeek 分析模块（抖音博主监控项目）

职责：把一段逐字稿（txt / json）交给本地 DeepSeek 中转，要求它输出严格 JSON 的五段式分析。

CLI:
    python lib/analyze.py --transcript <路径> [--title "..."] [--meta <json或json文件>] [--out <json路径>]

stdout 最后一行恒为一个 JSON：
    成功 {"ok":true,"model":..,"analysis":{core,evidence,conclusion,details,judgement},"raw":..,"warnings":[..]}
    失败 {"ok":false,...,"analysis":null,"raw":..,"warnings":[..],"error":"..."}

铁律（本机实测，不要改）：
  * 中转地址 http://127.0.0.1:8769/v1/chat/completions，入站不校验 Authorization（仍发 "Bearer local"）。
  * 模型 id 固定 "cline-pass/deepseek-v4.1-flash"。
  * 超时 180s，失败重试 2 次，退避 5s / 15s。
  * 不写任何密钥进源码/日志/Git；本模块根本不需要密钥。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any

import requests

# ---------------------------------------------------------------- 常量

BT = chr(96)                 # 反引号，源码里不出现字面反引号，便于外部工具链处理
FENCE = BT * 3

BASE_URL = os.environ.get("DS_ANALYZE_BASE_URL", "http://127.0.0.1:8769/v1").rstrip("/")
CHAT_URL = BASE_URL + "/chat/completions"
MODEL = os.environ.get("DS_ANALYZE_MODEL", "cline-pass/deepseek-v4.1-flash")
AUTH_HEADER = "Bearer local"          # 中转不校验，形式占位
TIMEOUT_SEC = 180
RETRY_DELAYS = (5, 15)                # 失败重试 2 次：5s、15s
SHORT_TRANSCRIPT_CHARS = 200
FIELDS = ("core", "evidence", "conclusion", "details", "judgement")


def log(msg: str) -> None:
    """过程日志一律走 stderr，stdout 只留最后一行 JSON。"""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- 逐字稿读取

def _as_text(obj: Any) -> str | None:
    """从各种可能的 json 结构里抠出正文。"""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("text", "transcript", "content", "full_text", "fullText", "body", "result", "data"):
            if key in obj:
                got = _as_text(obj[key])
                if got:
                    return got
        for key in ("segments", "sentences", "items", "lines"):
            if isinstance(obj.get(key), list):
                parts = []
                for it in obj[key]:
                    if isinstance(it, dict):
                        parts.append(str(it.get("text") or it.get("content") or ""))
                    elif isinstance(it, str):
                        parts.append(it)
                joined = "\n".join(p for p in parts if p.strip())
                if joined.strip():
                    return joined
        return None
    if isinstance(obj, list):
        parts = []
        for it in obj:
            got = _as_text(it)
            if got:
                parts.append(got)
        return "\n".join(parts) if parts else None
    return None


def _find_incomplete(obj: Any) -> bool:
    """递归找 incomplete / truncated 之类的布尔标记。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("incomplete", "is_incomplete", "truncated", "is_truncated", "partial") and isinstance(v, bool):
                if v:
                    return True
            if _find_incomplete(v):
                return True
    elif isinstance(obj, list):
        return any(_find_incomplete(x) for x in obj)
    return False


def load_transcript(path: str) -> tuple[str, dict]:
    """返回 (正文, 元信息)。元信息含 source_format / incomplete 等。"""
    if not os.path.isfile(path):
        raise FileNotFoundError("逐字稿不存在: " + path)
    with open(path, "rb") as fh:
        raw_bytes = fh.read()
    text = raw_bytes.decode("utf-8-sig", errors="replace")

    info: dict[str, Any] = {"path": os.path.abspath(path), "bytes": len(raw_bytes)}
    stripped = text.lstrip()

    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            info["source_format"] = "json-broken"
            info["json_error"] = str(exc)
            log("[warn] 看起来是 JSON 但解析失败(%s)，按纯文本处理" % exc)
            return text.strip(), info
        body = _as_text(obj)
        info["source_format"] = "json"
        info["incomplete"] = _find_incomplete(obj)
        if body is None:
            info["no_text_field"] = True
            log("[warn] JSON 里没找到正文字段，退回整份原文当正文")
            return text.strip(), info
        return body.strip(), info

    info["source_format"] = "txt"
    info["incomplete"] = bool(re.search(r'"?((is_)?incomplete|truncated)"?\s*[:=]\s*true', text, re.I))
    return text.strip(), info


def resolve_meta(arg: str | None) -> dict:
    """--meta 既接受 JSON 字符串，也接受 .json 文件路径。"""
    if not arg:
        return {}
    if os.path.isfile(arg):
        try:
            with open(arg, "rb") as fh:
                got = json.loads(fh.read().decode("utf-8-sig", errors="replace"))
            return got if isinstance(got, dict) else {"value": got}
        except json.JSONDecodeError as exc:
            log("[warn] --meta 文件不是合法 JSON（%s），忽略" % exc)
            return {}
    try:
        got = json.loads(arg)
        return got if isinstance(got, dict) else {"value": got}
    except json.JSONDecodeError:
        log("[warn] --meta 既不是文件也不是合法 JSON，忽略")
        return {}


# ---------------------------------------------------------------- Prompt

SYSTEM_PROMPT = (
    "你是一名严谨的中文内容分析师。你的输出必须是一个合法的 JSON 对象本身，"
    "不要 markdown 代码块，不要 " + FENCE + "json 围栏，不要任何解释性前后缀，不要注释，不要尾随逗号。"
    "所有字符串用双引号，字符串内部不要出现未转义的双引号或换行。"
)

JSON_TEMPLATE = """{
  "core": "核心观点，1~3 句，说清这条视频到底在主张什么（不要复述逐字稿，要概括）",
  "evidence": "具体论据：视频里给出的数据、事件、案例、对比、推理链条。逐条列出，能带原话里的数字就带上；没有论据就写「未提供具体论据」",
  "conclusion": "最终结论与立场：明确写出态度（赞成/反对/看多/看空/建议行动等）。若视频没有明确态度，就写「未表达明确立场」。不得替博主补立场",
  "details": "关键细节：数字、百分比、日期、人物、公司/机构、产品名、时间节点、明确预测。逐条列出，没有就写「无」",
  "judgement": "你（DeepSeek）的独立判断。必须把内容分成四类并分别标注：[客观事实]（可验证的公共事实）、[博主观点]（主观主张）、[未经证实的说法]（博主提出但未给依据或无法核实）、[AI推理]（你自己推出的判断及理由）。不得无依据附和博主，不得编造事实，不得把预测当成已验证事实。若视频涉及投资、财经、行情判断，额外单列一条以「可能影响市场的判断：」开头，写明这个判断是什么、依据是视频里的哪些证据、以及它仍然只是预测而非事实。"
}"""


def build_user_prompt(transcript: str, title: str | None, meta: dict, degraded: list[str]) -> str:
    lines = []
    lines.append("【逐字稿原文】")
    lines.append(FENCE)
    lines.append(transcript)
    lines.append(FENCE)
    lines.append("")
    lines.append("上面是一条抖音视频的逐字稿（语音转写，可能存在错别字、断句混乱、口语重复），请基于它做结构化分析。")

    if title:
        lines.append("")
        lines.append("【视频标题】")
        lines.append(title)

    if meta:
        keep = {k: v for k, v in meta.items()
                if k.lower() not in ("cookie", "token", "authorization", "api_key", "apikey", "password", "secret")}
        if keep:
            lines.append("")
            lines.append("【附加元信息】")
            lines.append(json.dumps(keep, ensure_ascii=False))

    if degraded:
        lines.append("")
        lines.append("【重要限制】")
        for d in degraded:
            lines.append("- " + d)
        lines.append("因此：只能基于已有内容分析，缺失的部分必须明确标注为「无法确认」，"
                     "不要用常识或猜测去补全关键数字、日期、人名、公司名、结论。")

    lines.append("")
    lines.append("【输出要求】只输出一个 JSON 对象，且必须正好包含这五个键：")
    lines.append("")
    lines.append(JSON_TEMPLATE)
    lines.append("")
    lines.append("【硬性约束】")
    lines.append("1. 五个字段都必须是非空字符串，用中文书写。")
    lines.append("2. 严禁编造逐字稿里没有的事实、数据、机构名；不确定就写「逐字稿未提及」或「无法确认」。")
    lines.append("3. 不要把视频里的预测/断言升级成事实陈述。")
    lines.append("4. 直接输出 JSON 对象，第一个字符必须是 {，最后一个字符必须是 }。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 请求

def call_model(messages: list, warnings: list) -> tuple:
    """返回 (content 文本, 响应元信息)。全部失败抛 RuntimeError。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": False,
    }
    headers = {"Content-Type": "application/json", "Authorization": AUTH_HEADER}

    attempts = 1 + len(RETRY_DELAYS)
    last_exc = None
    for i in range(attempts):
        t0 = time.time()
        try:
            log("[req] 第 %d/%d 次 POST %s (model=%s)" % (i + 1, attempts, CHAT_URL, MODEL))
            resp = requests.post(CHAT_URL, headers=headers, json=payload, timeout=TIMEOUT_SEC)
            dt = time.time() - t0
            log("[req] 返回 HTTP %s，耗时 %.2fs，body %d 字节" % (resp.status_code, dt, len(resp.content)))
            if resp.status_code != 200:
                snippet = resp.text[:400].replace("\n", " ")
                warnings.append("HTTP %s: %s" % (resp.status_code, snippet))
                raise RuntimeError("HTTP %s: %s" % (resp.status_code, snippet))
            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                content = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
            if not content:
                content = msg.get("reasoning_content") or ""
            if not content or not str(content).strip():
                raise RuntimeError("响应里没有 content 内容")
            info = {
                "http_model": data.get("model"),
                "usage": data.get("usage"),
                "finish_reason": choice.get("finish_reason"),
            }
            return str(content), info
        except Exception as exc:
            last_exc = exc
            dt = time.time() - t0
            if i < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[i]
                log("[retry] 第 %d 次失败（%.2fs）：%s: %s → %ds 后重试" % (i + 1, dt, type(exc).__name__, exc, delay))
                warnings.append("第 %d 次请求失败：%s: %s" % (i + 1, type(exc).__name__, exc))
                time.sleep(delay)
            else:
                log("[fail] 第 %d 次失败，放弃：%s: %s" % (i + 1, type(exc).__name__, exc))
    raise RuntimeError("全部 %d 次请求均失败：%s" % (attempts, last_exc))


# ---------------------------------------------------------------- 解析

def strip_fences(text: str) -> str:
    """剥掉围栏和前后废话，掐到最外层花括号。"""
    s = text.strip().replace("\ufeff", "").replace("\u200b", "")
    m = re.search(FENCE + r"[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*" + FENCE, s, re.S)
    if m:
        s = m.group(1).strip()
    else:
        s = re.sub("^" + FENCE + r"[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(FENCE + r"\s*$", "", s)
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    return s.strip()


def parse_analysis(content: str) -> tuple:
    """返回 (分析字典 或 None, 问题列表)。"""
    problems = []
    s = strip_fences(content)
    obj = None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        problems.append("严格 JSON 解析失败：%s" % exc)

    if obj is None:
        try:
            obj = json.loads(re.sub(r"\b(True|False|None)\b",
                                    lambda m: {"True": "true", "False": "false", "None": "null"}[m.group(1)], s))
            problems.append("已用 True/False/None 归一化后解析成功")
        except json.JSONDecodeError:
            obj = None

    if obj is None:
        try:
            obj = json.loads(re.sub(r",(\s*[}\]])", r"\1", s))
            problems.append("已剔除尾随逗号后解析成功")
        except json.JSONDecodeError:
            obj = None

    if obj is None:
        salvaged = {}
        for f in FIELDS:
            mm = re.search(r'"' + f + r'"\s*:\s*"(.*?)"\s*(?:,\s*"|\s*\})', s, re.S)
            if mm:
                raw = mm.group(1)
                try:
                    raw = json.loads('"' + raw.replace("\n", "\\n") + '"')
                except Exception:
                    pass
                salvaged[f] = raw
        if salvaged:
            problems.append("JSON 整体解析失败，已用正则抢救出 %d 个字段：%s" % (len(salvaged), sorted(salvaged)))
            obj = salvaged

    if not isinstance(obj, dict):
        return None, problems

    for wrapper in ("analysis", "data", "result", "output"):
        if isinstance(obj.get(wrapper), dict) and any(k in obj[wrapper] for k in FIELDS):
            problems.append('模型多包了一层 "%s"，已展开' % wrapper)
            obj = obj[wrapper]
            break

    out = {}
    for f in FIELDS:
        val = obj.get(f)
        if val is None:
            for k, v in obj.items():
                if str(k).strip().lower() == f:
                    val = v
                    break
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        if val is None or not str(val).strip():
            problems.append("缺少字段 %s（已补空串）" % f)
            out[f] = ""
        else:
            out[f] = str(val).strip()

    extra = [k for k in obj if k not in FIELDS]
    if extra:
        problems.append("模型多给了字段：%s" % extra[:8])
    return out, problems


# ---------------------------------------------------------------- 输出

def _emit(result: dict, out_path: str | None) -> None:
    line = json.dumps(result, ensure_ascii=False)
    if out_path:
        try:
            d = os.path.dirname(os.path.abspath(out_path))
            if d:
                os.makedirs(d, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(line + "\n")
            log("[out] 已写入 %s" % os.path.abspath(out_path))
        except Exception as exc:
            log("[warn] --out 写盘失败：%s" % exc)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="逐字稿 -> DeepSeek 五段式分析（严格 JSON 输出）")
    ap.add_argument("--transcript", required=True, help="逐字稿 txt 或 json 路径")
    ap.add_argument("--title", default=None, help="视频标题（可选）")
    ap.add_argument("--meta", default=None, help="附加元信息：JSON 字符串或 .json 文件路径（可选）")
    ap.add_argument("--out", default=None, help="把 stdout 那一行 JSON 同时写进这个文件（可选）")
    args = ap.parse_args(argv)

    warnings = []
    started = time.time()
    result = {"ok": False, "model": MODEL, "analysis": None, "raw": "", "warnings": warnings}

    try:
        text, info = load_transcript(args.transcript)
    except Exception as exc:
        warnings.append("读取逐字稿失败：%s: %s" % (type(exc).__name__, exc))
        result["error"] = str(exc)
        _emit(result, args.out)
        return 2

    chars = len(re.sub(r"\s", "", text))
    meta = resolve_meta(args.meta)
    if isinstance(meta.get("incomplete"), bool) and meta["incomplete"]:
        info["incomplete"] = True

    degraded = []
    if info.get("incomplete"):
        degraded.append("逐字稿被标记为「转写不完整」（incomplete=true），后半段或多处内容缺失。")
    if chars < SHORT_TRANSCRIPT_CHARS:
        degraded.append("逐字稿极短（去除空白后仅 %d 字，低于 %d 字阈值），信息量不足以支撑完整分析。"
                        % (chars, SHORT_TRANSCRIPT_CHARS))
    if degraded:
        warnings.append("转写不完整/过短：incomplete=%s, 有效字数=%s；已在 prompt 中要求模型把缺失部分标注为「无法确认」。"
                        % (bool(info.get("incomplete")), chars))
    if info.get("json_error"):
        warnings.append("逐字稿 JSON 解析失败，按纯文本处理：%s" % info["json_error"])

    log("[in] %s format=%s 有效字数=%d incomplete=%s"
        % (info["path"], info.get("source_format"), chars, bool(info.get("incomplete"))))

    if not text.strip():
        warnings.append("逐字稿正文为空，无法分析")
        result["error"] = "empty transcript"
        _emit(result, args.out)
        return 2

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(text, args.title, meta, degraded)},
    ]

    t_req = time.time()
    try:
        content, rinfo = call_model(messages, warnings)
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        warnings.append("模型调用失败：%s" % result["error"])
        result["elapsed_sec"] = round(time.time() - started, 2)
        log("[total] 失败，耗时 %ss" % result["elapsed_sec"])
        _emit(result, args.out)
        return 3
    req_elapsed = time.time() - t_req

    result["raw"] = content
    if rinfo.get("http_model"):
        result["model"] = rinfo["http_model"]
    if rinfo.get("usage"):
        result["usage"] = rinfo["usage"]
    if rinfo.get("finish_reason") and rinfo["finish_reason"] != "stop":
        warnings.append("finish_reason=%s（可能被截断）" % rinfo["finish_reason"])

    analysis, problems = parse_analysis(content)
    warnings.extend(problems)

    if analysis is None:
        warnings.append("模型返回不是可解析的 JSON，原文已放入 raw，ok=false")
        result["ok"] = False
        result["analysis"] = None
        result["error"] = "model output is not parseable JSON"
    else:
        result["ok"] = all(analysis.get(f) for f in FIELDS)
        result["analysis"] = analysis
        if not result["ok"]:
            warnings.append("解析成功但字段为空：%s" % [f for f in FIELDS if not analysis.get(f)])

    result["elapsed_sec"] = round(time.time() - started, 2)
    result["request_sec"] = round(req_elapsed, 2)
    result["transcript_chars"] = chars
    log("[total] ok=%s 请求 %.2fs 总 %ss warnings=%d"
        % (result["ok"], req_elapsed, result["elapsed_sec"], len(warnings)))
    _emit(result, args.out)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())

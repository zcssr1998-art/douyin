#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""语音转写模块（douyin-monitor / lib/transcribe.py）

链路：PyAV 解封装 + 重采样 -> faster-whisper（本地 CTranslate2 模型，CPU int8）-> JSON。

CLI:
    python lib/transcribe.py <音频或视频文件路径> [--out <json路径>]

stdout 最后一行（且只有这一行是 JSON）:
    {"ok":true,"model":"...","language":"zh","duration":123.4,
     "segments":[{"start":0.0,"end":3.2,"text":"..."}],"text":"全文","incomplete":false}
    {"ok":false,"error":"..."}   失败时

设计要点：
  1. 模型从本地目录直接加载，绝不访问 HuggingFace（本机 HF 不通）。
  2. 音频不走 ffmpeg：PyAV 自己解封装、重采样 16k 单声道 float32（本机没装 ffmpeg）。
  3. 中文：language="zh" + 简体中文 initial_prompt 引导标点，禁止自动语种检测漂移。
  4. incomplete=true 表示覆盖度不足：VAD 丢段导致"实际识别语音时长 / 音频总时长"低于阈值，
     或识别器报告时长与真实解码时长相差超过 20%（容器声明的时长不可信，一律以解码样本数为准）。
  5. 单文件处理，不做批处理。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 常量 / 配置

# 本机已实测可用的 CTranslate2 模型目录（model.bin / config.json / tokenizer.json / vocabulary.txt）
DEFAULT_MODEL_DIR = r"D:\AI\models\whisper-small"

# 项目配置：<project>/config.json，只读，缺失或字段为空都回落到上面的常量
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

TARGET_RATE = 16000
INITIAL_PROMPT = "以下是普通话的句子，请加标点。"
LANGUAGE = "zh"
COVERAGE_MIN = 0.80          # 识别语音时长占比低于此值 -> incomplete
DURATION_DRIFT_MAX = 0.20    # 识别时长与真实时长偏差超过此比例 -> incomplete

# 常见繁体 -> 简体兜底表（只收无歧义高频字，避免误改；本机没装 opencc/zhconv）
_T2S = str.maketrans({
    "這": "这", "說": "说", "個": "个", "們": "们", "來": "来", "時": "时", "後": "后",
    "會": "会", "對": "对", "為": "为", "國": "国", "學": "学", "點": "点", "現": "现",
    "實": "实", "樣": "样", "麼": "么", "見": "见", "問": "问", "題": "题", "麼": "么",
    "聽": "听", "話": "话", "語": "语", "聲": "声", "說": "说", "載": "载", "圖": "图",
    "視": "视", "頻": "频", "標": "标", "記": "记", "級": "级", "兩": "两", "種": "种",
    "應": "应", "該": "该", "於": "于", "與": "与", "還": "还", "讓": "让", "給": "给",
    "帶": "带", "開": "开", "關": "关", "從": "从", "過": "过", "進": "进",
    "發": "发", "現": "现", "動": "动", "狀": "状", "態": "态", "數": "数", "據": "据",
    "網": "网", "線": "线", "頁": "页", "麵": "面", "們": "们", "麼": "么", "覺": "觉",
    "處": "处", "總": "总", "結": "结", "認": "认", "識": "识", "讀": "读", "寫": "写",
    "運": "运", "轉": "转", "換": "换", "設": "设", "計": "计", "畫": "画", "當": "当",
    "經": "经", "過": "过", "樣": "样", "辦": "办", "軟": "软", "體": "体", "統": "统",
    "裡": "里", "裡": "里", "幾": "几", "長": "长", "間": "间", "夠": "够", "嗎": "吗",
    "誰": "谁", "產": "产", "業": "业", "務": "务", "員": "员", "錢": "钱", "買": "买",
    "賣": "卖", "價": "价", "單": "单", "雙": "双", "邊": "边", "頭": "头", "車": "车",
    "機": "机", "電": "电", "腦": "脑", "裏": "里", "舉": "举", "愛": "爱",
})


def to_simplified(text: str) -> str:
    """把常见繁体字换成简体，纯兜底，不做词级转换。"""
    return text.translate(_T2S)


def resolve_model_dir(cli_value: str | None = None) -> Path:
    """模型目录优先级：--model-dir > 环境变量 ASR_MODEL_DIR > config.json 的 asr.model_dir > 常量。"""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("ASR_MODEL_DIR")
    if env:
        return Path(env)
    try:
        if CONFIG_PATH.is_file():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            value = (cfg.get("asr") or {}).get("model_dir")
            if value and str(value).strip():
                return Path(str(value).strip())
    except Exception:
        pass
    return Path(DEFAULT_MODEL_DIR)

# ---------------------------------------------------------------- 音频解码

def decode_audio(path: str, target_rate: int = TARGET_RATE):
    """用 PyAV 解封装任意容器，重采样成 16k 单声道 float32 一维数组。

    返回 (samples: np.ndarray[float32], duration_sec: float)。
    每一帧音频都过重采样器，返回的是真实解码样本数算出的时长，
    不信容器头里写的 duration（抖音直出的 mp4 常有 0 或错值）。
    """
    import av
    import numpy as np

    chunks: list = []
    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError("文件里没有音频流（视频轨静音或文件损坏）")
        stream = container.streams.audio[0]
        stream.thread_type = "AUTO"
        resampler = av.AudioResampler(format="flt", layout="mono", rate=target_rate)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        # flush 重采样器内部残留
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))

    if not chunks:
        raise ValueError("解码后没有任何音频样本")
    samples = np.concatenate(chunks).astype("float32", copy=False)
    duration = float(samples.shape[0]) / float(target_rate)
    if duration <= 0.05:
        raise ValueError(f"音频太短（{duration:.3f}s），无法转写")
    return samples, duration

# ---------------------------------------------------------------- 转写

def transcribe_file(
    media_path: str,
    out_path: str | None = None,
    model_dir: str | None = None,
    language: str = LANGUAGE,
) -> dict:
    """单文件转写。返回字典（同时按需写文件），调用方负责打印。"""
    started = time.time()
    resolved = resolve_model_dir(model_dir)

    if not (resolved / "model.bin").is_file():
        raise FileNotFoundError(f"本地模型目录里没有 model.bin：{resolved}")

    load_started = time.time()
    from faster_whisper import WhisperModel

    model = WhisperModel(str(resolved), device="cpu", compute_type="int8")
    load_seconds = round(time.time() - load_started, 2)

    samples, real_duration = decode_audio(media_path)

    asr_started = time.time()
    segments_iter, info = model.transcribe(
        samples,
        language=language,
        task="transcribe",
        beam_size=5,
        best_of=5,
        temperature=0.0,
        initial_prompt=INITIAL_PROMPT,
        condition_on_previous_text=False,   # 长音频关掉，防止复读循环
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
    )

    segments = []
    speech_seconds = 0.0
    for seg in segments_iter:
        text = to_simplified((seg.text or "").strip())
        if not text:
            continue
        start = round(float(seg.start), 2)
        end = round(float(seg.end), 2)
        segments.append({"start": start, "end": end, "text": text})
        speech_seconds += max(0.0, float(seg.end) - float(seg.start))

    asr_seconds = round(time.time() - asr_started, 2)
    full_text = "".join(s["text"] for s in segments)

    coverage = (speech_seconds / real_duration) if real_duration > 0 else 0.0
    reported_duration = float(getattr(info, "duration", 0.0) or 0.0)
    drift = (
        abs(reported_duration - real_duration) / real_duration
        if real_duration > 0 and reported_duration > 0
        else 0.0
    )
    incomplete = bool(coverage < COVERAGE_MIN or drift > DURATION_DRIFT_MAX)

    result = {
        "ok": True,
        "model": str(resolved),
        "language": getattr(info, "language", language) or language,
        "duration": round(real_duration, 2),
        "segments": segments,
        "text": full_text,
        "incomplete": incomplete,
        # 以下为诊断字段，不影响上游约定（多出来的键不影响 JSON 解析）
        "coverage": round(coverage, 3),
        "duration_drift": round(drift, 3),
        "segments_count": len(segments),
        "load_seconds": load_seconds,
        "asr_seconds": asr_seconds,
        "total_seconds": round(time.time() - started, 2),
    }

    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def main(argv=None) -> int:
    # 保证 Windows 控制台 / 管道都按 UTF-8 输出，否则中文会被 GBK 打成乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="单文件语音转写（faster-whisper，本地模型）")
    parser.add_argument("media", help="音频或视频文件路径（mp4/m4a/wav/mp3/…）")
    parser.add_argument("--out", default=None, help="结果 JSON 落盘路径（可选）")
    parser.add_argument("--model-dir", default=None, help="覆盖模型目录（默认取常量/config）")
    parser.add_argument("--language", default=LANGUAGE, help="语言代码，默认 zh")
    args = parser.parse_args(argv)

    try:
        if not Path(args.media).is_file():
            raise FileNotFoundError(f"输入文件不存在：{args.media}")
        result = transcribe_file(args.media, args.out, args.model_dir, args.language)
    except Exception as exc:  # 任何异常都必须变成一行 JSON，不能抛栈给上游
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""LLM（GLM，Anthropic 兼容层）与 ASR（智谱 glm-asr）客户端 — v3.1 全自动内容管线。
配置读 data/llm.json：{"base_url": "...", "api_key": "...", "model": "..."}（bind mount，不进 git）。
未配置时精修/ASR 优雅降级（释义走 ECDICT、语音提示用键盘听写）。"""
import json
import urllib.request
from pathlib import Path

CONF_FILE = Path(__file__).parent / "data" / "llm.json"
ASR_URL = "https://open.bigmodel.cn/api/paas/v4/audio/transcriptions"


def conf():
    if not CONF_FILE.exists():
        return None
    try:
        d = json.loads(CONF_FILE.read_text())
        if d.get("base_url") and d.get("api_key") and d.get("model"):
            return d
    except Exception:
        pass
    return None


def chat(prompt, system=None, max_tokens=4000, timeout=120):
    """messages 调用；content 为 thinking+text 混合块，取全部 text 拼接。失败抛异常。"""
    c = conf()
    if not c:
        raise RuntimeError("LLM 未配置（data/llm.json）")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    req = urllib.request.Request(
        c["base_url"].rstrip("/") + "/v1/messages",
        data=json.dumps({"model": c["model"], "max_tokens": max_tokens,
                         "messages": msgs}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": c["api_key"],
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    parts = [b.get("text", "") for b in out.get("content", []) if b.get("type") == "text"]
    return "".join(parts)


def transcribe(audio_bytes: bytes, filename: str = "audio.mp3") -> str:
    """语音识别（智谱 glm-asr，OpenAI whisper 兼容）。返回文本，失败抛异常。"""
    c = conf()
    if not c:
        raise RuntimeError("LLM 未配置（data/llm.json）")
    import uuid
    boundary = uuid.uuid4().hex
    ctype = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "mp4": "audio/mp4",
             "wav": "audio/wav", "webm": "audio/webm"}.get(filename.rsplit(".", 1)[-1].lower(),
                                                          "application/octet-stream")
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nglm-asr\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {ctype}\r\n\r\n').encode() + audio_bytes + \
           f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(ASR_URL, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {c['api_key']}",
                                          "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    return (out.get("text") or "").strip()

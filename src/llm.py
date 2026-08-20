# -*- coding: utf-8 -*-
"""
多协议 LLM 客户端：读配置 -> 自动识别并适配 OpenAI 兼容 / Anthropic / Gemini 协议 -> 返回文本。

定位：
- 零第三方依赖（纯 urllib），任意装 Python 3 的机器都能跑。
- config.json 三字段：base_url（服务地址）+ api_key + model；provider 可选：
    auto     自动探测协议（默认，按 openai -> anthropic -> gemini 实测）
    openai   OpenAI 兼容（DeepSeek/OpenAI/通义/智谱/Kimi/本地 vLLM 等）
    anthropic  Claude 原生 /v1/messages
    gemini    Google /v1beta/models/:generateContent
- 探测 = 一次真实最小对话，成功即证明「连得上 + key 有效 + 模型可用」。
- 连接测试：test_connection() 返回结构化诊断，供网页 /llm/test 一键验证。

API key 来源优先级：环境变量 LLM_API_KEY  >  config.json 里的 api_key（兼容旧字段 deepseek_api_key）。
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

FROZEN = getattr(sys, "frozen", False)   # 是否打包成 exe（PyInstaller 运行时为 True）
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 项目根目录（exe 或脚本所在目录）
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
PROVIDERS = ("auto", "openai", "anthropic", "gemini")
_DETECT_ORDER = ("openai", "anthropic", "gemini")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


class _LLMError(RuntimeError):
    """带阶段分类的 LLM 调用错误。

    stage 取值：auth / network / model_not_found / endpoint / bad_request / server / parse / unknown。
    """

    def __init__(self, stage, detail):
        super().__init__(f"[{stage}] {detail}")
        self.stage = stage
        self.detail = detail


_HINTS = {
    "auth": "检查 api_key 是否正确或已过期",
    "network": "检查 base_url 能否访问（网络/防火墙/代理）",
    "model_not_found": "检查 model 名称是否正确",
    "endpoint": "检查 base_url 是否是该服务的正确地址（可能需要补 /v1）",
    "bad_request": "该服务与当前参数不兼容，可在 config.json 里显式指定 provider",
    "server": "服务端异常，稍后重试或检查服务状态",
    "parse": "响应格式异常，检查 base_url/model 是否正确",
    "unknown": "未知错误，看 detail 信息",
}


def _hint(stage):
    return _HINTS.get(stage, _HINTS["unknown"])


def load_config(path=CONFIG_PATH):
    """读 config.json，合并默认值；环境变量 LLM_API_KEY 优先级最高。

    字段通用化：provider（协议，默认 auto）+ api_key + base_url + model。
    向后兼容旧字段 deepseek_api_key。
    """
    cfg = {
        "provider": "auto",
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "api_key": "",
    }
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                extra = json.load(f)
            if isinstance(extra, dict):
                for k in ("provider", "base_url", "model", "api_key"):
                    if k in extra and extra[k] not in (None, ""):
                        cfg[k] = extra[k]
                if not cfg["api_key"] and extra.get("deepseek_api_key"):
                    cfg["api_key"] = extra["deepseek_api_key"]
        except (json.JSONDecodeError, OSError):
            pass
    env_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        cfg["api_key"] = env_key
    return cfg


def get_api_key(config):
    key = (config.get("api_key") or "").strip()
    if not key:
        raise RuntimeError(
            "缺少 API key。请在 config.json 里填 api_key，"
            "或设置环境变量 LLM_API_KEY。"
        )
    return key


# ---------- 端点与消息翻译 ----------

def _base_url(config):
    return (config.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")


def _openai_url(base):
    return base + "/chat/completions"


def _anthropic_url(base):
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _gemini_url(base, model):
    return base + "/v1beta/models/" + model + ":generateContent"


def _split_system(messages):
    """把 OpenAI 格式消息里的 system 抽出来，其余原样返回。"""
    system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    rest = [m for m in messages if m.get("role") != "system"]
    return system, rest


def _anthropic_messages(messages):
    """OpenAI 消息 -> Anthropic messages；system 抽出，相邻同 role 合并（Anthropic 要求交替）。"""
    system, rest = _split_system(messages)
    merged = []
    for m in rest:
        role = m.get("role", "user")
        content = m.get("content", "")
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] = merged[-1]["content"].rstrip() + "\n" + content
        else:
            merged.append({"role": role, "content": content})
    return system, merged


def _gemini_contents(messages):
    """OpenAI 消息 -> Gemini contents；system 抽到 system_instruction。"""
    system, rest = _split_system(messages)
    contents = []
    for m in rest:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    return system, contents


# ---------- 底层 HTTP 与错误分类 ----------

def _classify(code, detail):
    if code in (401, 403):
        return _LLMError("auth", f"HTTP {code}: {detail[:300]}")
    if code == 404:
        if "model" in detail.lower():
            return _LLMError("model_not_found", f"HTTP 404: {detail[:300]}")
        return _LLMError("endpoint", f"HTTP 404: {detail[:300]}")
    if code in (400, 422):
        return _LLMError("bad_request", f"HTTP {code}: {detail[:300]}")
    if 500 <= code < 600:
        return _LLMError("server", f"HTTP {code}: {detail[:300]}")
    return _LLMError("unknown", f"HTTP {code}: {detail[:300]}")


def _http_post(url, headers, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise _classify(e.code, detail)
    except urllib.error.URLError as e:
        raise _LLMError("network", f"网络错误：{e.reason}")
    except Exception as e:
        raise _LLMError("unknown", str(e))


def _parse_openai(body):
    try:
        obj = json.loads(body)
        return obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        raise _LLMError("parse", f"OpenAI 响应解析失败：{e}")


def _parse_anthropic(body):
    try:
        obj = json.loads(body)
        return obj["content"][0]["text"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        raise _LLMError("parse", f"Anthropic 响应解析失败：{e}")


def _parse_gemini(body):
    try:
        obj = json.loads(body)
        return obj["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        raise _LLMError("parse", f"Gemini 响应解析失败：{e}")


# ---------- 三协议请求函数 ----------

def _call_openai(messages, config, temperature, max_tokens, retries, json_mode):
    key = get_api_key(config)
    url = _openai_url(_base_url(config))
    model = config.get("model") or DEFAULT_MODEL
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    use_json = json_mode
    last = None
    for attempt in range(retries + 1):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        try:
            body = _http_post(url, headers, payload)
            return _parse_openai(body)
        except _LLMError as e:
            # json_object 模式不被识别时，关掉它再试一次
            if e.stage == "bad_request" and use_json:
                use_json = False
                last = e
                continue
            last = e
        if attempt < retries:
            time.sleep(2)
    raise last or _LLMError("unknown", "未知错误")


def _call_anthropic(messages, config, temperature, max_tokens, retries):
    key = get_api_key(config)
    url = _anthropic_url(_base_url(config))
    model = config.get("model") or DEFAULT_MODEL
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    }
    system, msgs = _anthropic_messages(messages)
    payload = {"model": model, "max_tokens": max_tokens, "temperature": temperature}
    if system:
        payload["system"] = system
    payload["messages"] = msgs
    last = None
    for attempt in range(retries + 1):
        try:
            body = _http_post(url, headers, payload)
            return _parse_anthropic(body)
        except _LLMError as e:
            last = e
        if attempt < retries:
            time.sleep(2)
    raise last or _LLMError("unknown", "未知错误")


def _call_gemini(messages, config, temperature, max_tokens, retries):
    key = get_api_key(config)
    model = config.get("model") or DEFAULT_MODEL
    url = _gemini_url(_base_url(config), model)
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    system, contents = _gemini_contents(messages)
    payload = {"contents": contents}
    if system:
        payload["system_instruction"] = {"parts": [{"text": system}]}
    last = None
    for attempt in range(retries + 1):
        try:
            body = _http_post(url, headers, payload)
            return _parse_gemini(body)
        except _LLMError as e:
            last = e
        if attempt < retries:
            time.sleep(2)
    raise last or _LLMError("unknown", "未知错误")


def _call(provider, messages, config, temperature, max_tokens, retries, json_mode):
    if provider == "openai":
        return _call_openai(messages, config, temperature, max_tokens, retries, json_mode)
    if provider == "anthropic":
        return _call_anthropic(messages, config, temperature, max_tokens, retries)
    if provider == "gemini":
        return _call_gemini(messages, config, temperature, max_tokens, retries)
    raise _LLMError("unknown", f"未知 provider：{provider}")


# ---------- 协议识别与自动探测 ----------

_DETECT_CACHE = {}   # base_url -> 已探测命中的 provider（进程内缓存）


def _resolve_provider(config):
    """显式 provider 返回其值；auto 或非法值返回 None（表示需要探测）。"""
    p = (config.get("provider") or "auto").strip().lower()
    return p if p in ("openai", "anthropic", "gemini") else None


def _detect_provider(config):
    """auto 模式下按 openai -> anthropic -> gemini 实测，返回命中的 provider。

    致命错误（认证/网络/模型不存在）直接抛；端点不存在/参数不符换下一个协议。
    探测成功按 base_url 缓存，同一次进程内不重复探测。
    """
    base = _base_url(config)
    hit = _DETECT_CACHE.get(base)
    if hit:
        return hit
    last = None
    for proto in _DETECT_ORDER:
        try:
            _call(proto, [{"role": "user", "content": "ping"}], config,
                  temperature=0, max_tokens=8, retries=0, json_mode=False)
            _DETECT_CACHE[base] = proto
            return proto
        except _LLMError as e:
            last = e
            if e.stage in ("auth", "network", "model_not_found"):
                raise
    raise last or _LLMError("unknown", "无法识别 LLM 协议（三个协议都未连通）")


# ---------- 对外入口 ----------

def chat(messages, config=None, temperature=0.2, max_tokens=4000, retries=2):
    """调用已适配的 LLM 协议，返回 assistant 文本；失败抛 RuntimeError（含阶段与建议）。"""
    config = config or load_config()
    provider = _resolve_provider(config)
    if provider is None:
        provider = _detect_provider(config)
    try:
        return _call(provider, messages, config, temperature, max_tokens, retries, json_mode=True)
    except _LLMError as e:
        raise RuntimeError(f"{e}（{_hint(e.stage)}）") from None


def test_connection(config=None):
    """连接测试：发一个最小请求，返回结构化诊断，供 /llm/test 使用。"""
    config = config or load_config()
    t0 = time.time()
    try:
        get_api_key(config)
    except RuntimeError as e:
        return {
            "ok": False,
            "provider": config.get("provider", "auto"),
            "stage": "auth",
            "detail": str(e),
            "hint": _hint("auth"),
        }
    provider = _resolve_provider(config)
    if provider is None:
        try:
            provider = _detect_provider(config)
        except _LLMError as e:
            return {
                "ok": False,
                "provider": "auto",
                "stage": e.stage,
                "detail": e.detail,
                "hint": _hint(e.stage),
            }
    try:
        text = _call(provider, [{"role": "user", "content": "回复 ok"}], config,
                     temperature=0, max_tokens=8, retries=0, json_mode=False)
    except _LLMError as e:
        return {
            "ok": False,
            "provider": provider,
            "stage": e.stage,
            "detail": e.detail,
            "hint": _hint(e.stage),
        }
    return {
        "ok": True,
        "provider": provider,
        "base_url": config.get("base_url"),
        "model": config.get("model"),
        "latency_ms": int((time.time() - t0) * 1000),
        "reply": (text or "").strip()[:50],
    }

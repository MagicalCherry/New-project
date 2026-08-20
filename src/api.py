# -*- coding: utf-8 -*-
"""
排产 Agent 线上接口（FastAPI 版）。

端点：
  POST /schedule   线上排产（body=场景 JSON 或 {"input": 场景}），不调 LLM
  POST /ingest     导入训练数据（body={input,output} 或它们的列表），存 训练方案数据/
  POST /config     设置 LLM 配置（api_key/model/base_url，字段都可选），存 config.json
  GET  /rules      读当前软规则
  GET  /health     健康检查
  GET  /docs       FastAPI 自动交互式文档（Swagger UI），可在网页上填参数、上传 json

启动：
  python src/api.py               # 默认 0.0.0.0:8000
  python src/api.py --port 9000
  uvicorn api:app --host 0.0.0.0 --port 8000

依赖：pip install fastapi uvicorn
"""

import json
import os
import sys
from datetime import datetime

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import uvicorn

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import llm
from scheduler import schedule, load_rules

FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "训练方案数据")
RULES_PATH = os.path.join(BASE_DIR, "rules.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "agent_state.json")

_SCENARIO_KEYS = ("jobs", "machines", "crafts", "procedures")

app = FastAPI(
    title="排产 Agent API",
    description="规则式排产 + 历史数据导入 + LLM 配置。排产只跑确定性代码，不逐单调 LLM。",
    version="2.0",
)


def _is_scenario(obj):
    return isinstance(obj, dict) and all(k in obj for k in _SCENARIO_KEYS)


@app.post("/schedule")
def api_schedule(payload: dict):
    """线上排产：body=场景 JSON，或 {"input": 场景}。返回 machineResults。"""
    if not isinstance(payload, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象（排产场景）"}, status_code=400)
    scenario = payload["input"] if _is_scenario(payload.get("input")) else payload
    if not _is_scenario(scenario):
        return JSONResponse({"error": "缺少场景必要字段 jobs/machines/crafts/procedures"}, status_code=400)
    try:
        return schedule(scenario, rules=load_rules(RULES_PATH))
    except Exception as e:
        return JSONResponse({"error": f"排产失败：{e}"}, status_code=500)


@app.post("/ingest")
def api_ingest(payload=Body(...)):
    """导入训练数据：body={input, output} 或它们的列表。存进 训练方案数据/ 供下轮重训。"""
    items = payload if isinstance(payload, list) else [payload]
    os.makedirs(DATA_DIR, exist_ok=True)
    saved = 0
    for item in items:
        if not (isinstance(item, dict) and "input" in item and "output" in item):
            continue
        if not _is_scenario(item["input"]):
            continue
        name = f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        with open(os.path.join(DATA_DIR, name), "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False)
        saved += 1
    return {"saved": saved, "skipped": len(items) - saved}


def _load_cfg():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}
    return cfg


def _override_cfg(payload, base):
    """从请求体提取非空字段覆盖配置；provider 额外校验合法值。"""
    override = {}
    for k in ("api_key", "model", "base_url"):
        if payload.get(k) not in (None, ""):
            override[k] = payload[k]
    if payload.get("provider") in llm.PROVIDERS:
        override["provider"] = payload["provider"]
    return {**base, **override}


@app.post("/config")
def api_config(payload: dict):
    """设置 LLM 配置：body={"api_key": "...", "model": "...", "base_url": "...", "provider": "auto|openai|anthropic|gemini"}（字段都可选）。

    只更新传入的非空字段，其余保留；api_key 可填任意大模型服务商的 key。
    """
    if not isinstance(payload, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
    cfg = _load_cfg()
    new_cfg = _override_cfg(payload, cfg)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(new_cfg, f, ensure_ascii=False, indent=2)
    return {
        "saved": True,
        "provider": new_cfg.get("provider"),
        "model": new_cfg.get("model"),
        "base_url": new_cfg.get("base_url"),
        "has_api_key": bool(new_cfg.get("api_key")),
    }


@app.post("/llm/test")
def api_llm_test(payload: dict):
    """测试 LLM 连通性：body 可选覆盖 {api_key?, model?, base_url?, provider?, save?}。

    用覆盖后的配置实际发一个最小请求并返回诊断（协议、阶段、延迟）。
    save=true 且测试通过时，把覆盖的配置写入 config.json（"测完一键保存"）。
    """
    if not isinstance(payload, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
    cfg = _load_cfg()
    test_cfg = _override_cfg(payload, cfg)
    result = llm.test_connection(test_cfg)
    if result.get("ok") and payload.get("save"):
        override = {k: test_cfg[k] for k in ("api_key", "model", "base_url", "provider") if test_cfg.get(k)}
        cfg.update(override)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        result["saved"] = True
    return result


@app.get("/rules")
def api_rules():
    return load_rules(RULES_PATH)


@app.get("/health")
def api_health():
    state = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    rules = load_rules(RULES_PATH)
    return {
        "status": "ok",
        "rules_version": rules.get("version"),
        "cycle_count": state.get("cycle_count", 0),
        "last_cycle_at": state.get("last_cycle_at"),
    }


if __name__ == "__main__":
    port = 8000
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    uvicorn.run(app, host="0.0.0.0", port=port)

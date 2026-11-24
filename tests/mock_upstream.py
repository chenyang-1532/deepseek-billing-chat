"""演示用上游渠道：模拟 DeepSeek 的 Anthropic 兼容 /v1/messages 接口。
返回标准 Anthropic SSE 流（message_start 携带 input_tokens，message_delta 携带 output_tokens），
让 new-api 能正常完成 token 计量与扣费。
"""
import asyncio
import json
import os
import sqlite3

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()

REPLY = "你好！我是演示模式下模型生成的回复。网关计费链路已打通：本次对话的 token 已计量。"

# 测试触发器（仅测试环境使用，正常请求不含这些标记）：
#   TRIGGER_500 → 上游 500（测群聊 persona_failed 路径）
#   DRAIN_402   → 回复前把发起用户钱包余额抽干（测群聊回合中途 402）
MOCK_NAPI_DB = os.environ.get("MOCK_NAPI_DB", "")
# 透传官方（语义类断言需要真实 LLM 回复）：无触发器的非流式请求转发真实 DeepSeek，
# 失败则回退固定文案（本机沙箱无官方 key 时三件套等场景不受影响）
MOCK_OFFICIAL_KEY = os.environ.get("MOCK_OFFICIAL_KEY", "")
MOCK_OFFICIAL_BASE = os.environ.get(
    "MOCK_OFFICIAL_BASE", "https://api.deepseek.com/anthropic")


def _drain_quota() -> None:
    """把发起用户钱包余额抽干（群聊中途耗尽测试用）。

    注意：new-api 转发 Anthropic 渠道时 Authorization 会被换成渠道自己的 key，
    无法从请求头识别发起用户——改借 tokens.accessed_time：发起用户的 token 刚
    被 new-api 校验过（秒级新鲜），串行测试场景下即唯一活跃 token。置 quota=1
    让后续余额闸全部命中（相对扣减仍由 new-api postConsume 完成）。"""
    if not MOCK_NAPI_DB:
        return
    try:
        c = sqlite3.connect(MOCK_NAPI_DB, timeout=10)
        # 只 drain 最新被访问的那 1 个 token 的用户：DRAIN_402 请求本身刚经 new-api
        # 校验发起人 token（accessed_time 秒级最新）；宽窗口会误伤其他测试号的 token
        # （如上一测试刚用过的 u1），导致后续并发用例被第一道余额闸 402。
        rows = c.execute(
            "SELECT user_id FROM tokens WHERE status=1 "
            "ORDER BY accessed_time DESC LIMIT 1").fetchall()
        for (uid,) in rows:
            c.execute("UPDATE users SET quota=1 WHERE id=?", (uid,))
        c.commit()
        c.close()
        print("[mock_upstream] DRAIN_402: drained token user(s) %s" % [r[0] for r in rows])
    except Exception as e:  # noqa: BLE001
        print("[mock_upstream] drain 失败:", e)


def _passthrough_official(body: dict) -> bytes | None:
    """非流式请求透传真实 DeepSeek（model 映射 mock-flash→deepseek-flash）。
    未配置官方 key / 转发失败返回 None（调用方回退固定文案）。"""
    if not MOCK_OFFICIAL_KEY:
        return None
    b = dict(body)
    if b.get("model") == "mock-flash":
        b["model"] = "deepseek-flash"
    import urllib.request
    req = urllib.request.Request(
        MOCK_OFFICIAL_BASE + "/v1/messages",
        data=json.dumps(b, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json",
                 "authorization": "Bearer " + MOCK_OFFICIAL_KEY},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print("[mock_upstream] passthrough 失败(回退固定文案):", e)
        return None


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def count_tokens(text: str) -> int:
    # 粗略估算：中文按字、英文按词，模拟真实计量
    return max(1, len(text) // 2)


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    # 从 Anthropic messages 里提取用户输入
    prompt = ""
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            prompt += content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    prompt += part.get("text", "")
    # 把 system 也算进输入 token
    system_text = ""
    sys = body.get("system")
    if isinstance(sys, str):
        system_text = sys
    elif isinstance(sys, list):
        for part in sys:
            if isinstance(part, dict) and part.get("type") == "text":
                system_text += part.get("text", "")

    input_tokens = count_tokens(prompt) + count_tokens(system_text) + 10
    output_tokens = count_tokens(REPLY)
    model = body.get("model", "deepseek-chat")
    msg_id = "msg_demo_0001"

    # stream:false → 非流式 JSON 响应（网关 v21 强制非流式路径用：new-api 透传后
    # 站点把 JSON 转回 SSE 回填前端）
    if body.get("stream") is False:
        if "TRIGGER_500" in prompt or "TRIGGER_500" in system_text:
            return JSONResponse({"error": {"message": "mock upstream triggered 500"}}, status_code=500)
        if "DRAIN_402" in prompt or "DRAIN_402" in system_text:
            _drain_quota()
        # 无触发器 → 透传官方真实回复（语义类断言依赖真 LLM）；失败回退固定文案
        real = _passthrough_official(body)
        if real is not None:
            return Response(content=real, media_type="application/json")
        return JSONResponse({
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": REPLY}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        })

    async def stream():
        yield sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id, "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        })
        yield sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        # 分两段吐字，模拟流式
        chunks = [REPLY[:len(REPLY) // 2], REPLY[len(REPLY) // 2:]]
        for c in chunks:
            yield sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": c},
            })
            await asyncio.sleep(0.15)
        yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        })
        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/messages/count_tokens")
async def count_tokens_endpoint(request: Request):
    body = await request.json()
    prompt = ""
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, str):
            prompt += content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    prompt += part.get("text", "")
    return JSONResponse({"input_tokens": count_tokens(prompt) + 10})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=3002)

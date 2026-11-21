"""演示用上游渠道：模拟 DeepSeek 的 Anthropic 兼容 /v1/messages 接口。
返回标准 Anthropic SSE 流（message_start 携带 input_tokens，message_delta 携带 output_tokens），
让 new-api 能正常完成 token 计量与扣费。
"""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()

REPLY = "你好！我是演示模式下模型生成的回复。网关计费链路已打通：本次对话的 token 已计量。"


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

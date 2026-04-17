import json
import re
from typing import Any, Dict, List, Optional, Union

from fastapi.responses import StreamingResponse

from compat.tool_calling import infer_openai_tool_call, log_incoming_tools, message_text


def create_chunk(id: str, created: int, model: str, delta: dict, finish_reason: Union[str, None]) -> str:
    return json.dumps({
        "id": id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
        "system_fingerprint": None,
    }, ensure_ascii=False)


def chat_completion_response(chat_id: str, created_time: int, model: str, message: Dict[str, Any], finish_reason: str) -> Dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created_time,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def simple_streaming_response(chat_id: str, created_time: int, model: str, deltas: List[Dict[str, Any]], finish_reason: str) -> StreamingResponse:
    async def generator():
        for delta in deltas:
            yield f"data: {create_chunk(chat_id, created_time, model, delta, None)}\n\n"
        yield f"data: {create_chunk(chat_id, created_time, model, {{}}, finish_reason)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


def tool_streaming_response(chat_id: str, created_time: int, model: str, tool_call: Dict[str, Any]) -> StreamingResponse:
    return simple_streaming_response(
        chat_id,
        created_time,
        model,
        [{"role": "assistant"}, {"tool_calls": [{
            "index": 0,
            "id": tool_call["id"],
            "type": tool_call["type"],
            "function": tool_call["function"],
        }]}],
        "tool_calls",
    )


def tool_call_response(chat_id: str, created_time: int, model: str, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    return chat_completion_response(
        chat_id,
        created_time,
        model,
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        "tool_calls",
    )


def last_tool_message(messages: List[Any]) -> Optional[Any]:
    for message in reversed(messages):
        role = getattr(message, "role", None) if not isinstance(message, dict) else message.get("role")
        if role == "tool":
            return message
    return None


def final_response_from_tool_result(req: Any) -> Optional[str]:
    """Apply a minimal local finalization shim for simple tool-result turns.

    This does not call the upstream model for another reasoning step. It only
    converts the latest tool output into a final assistant message for a small
    set of predictable patterns.
    """
    tool_message = last_tool_message(req.messages)
    if tool_message is None:
        return None
    content = getattr(tool_message, "content", None) if not isinstance(tool_message, dict) else tool_message.get("content")
    tool_text = message_text(content).strip()
    user_text = ""
    for message in reversed(req.messages):
        role = getattr(message, "role", None) if not isinstance(message, dict) else message.get("role")
        if role == "user":
            msg_content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
            user_text = message_text(msg_content).lower()
            break
    first_line_match = re.search(r"^\s*1\s*\|\s*(.+)$", tool_text, re.MULTILINE)
    if first_line_match and any(phrase in user_text for phrase in ["first line", "dòng đầu", "dong dau"]):
        return first_line_match.group(1).strip()
    if tool_text:
        return tool_text
    return "Done."


def maybe_handle_openai_tool_request(req: Any, chat_id: str, created_time: int):
    log_incoming_tools(chat_id, getattr(req, "tools", None), getattr(req, "tool_choice", None), getattr(req, "parallel_tool_calls", None))
    final_response = final_response_from_tool_result(req)
    if final_response is not None:
        if getattr(req, "stream", False):
            return simple_streaming_response(chat_id, created_time, req.model, [{"role": "assistant"}, {"content": final_response}], "stop")
        return chat_completion_response(chat_id, created_time, req.model, {"role": "assistant", "content": final_response}, "stop")

    tool_call = infer_openai_tool_call(req, chat_id)
    if not tool_call:
        return None
    if getattr(req, "stream", False):
        return tool_streaming_response(chat_id, created_time, req.model, tool_call)
    return tool_call_response(chat_id, created_time, req.model, tool_call)

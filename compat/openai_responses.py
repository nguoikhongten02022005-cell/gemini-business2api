import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from compat.openai_chat import maybe_handle_openai_tool_request
from compat.tool_calling import message_text


class ResponsesInputItem(BaseModel):
    role: Optional[str] = None
    type: Optional[str] = None
    content: Optional[Any] = None
    text: Optional[str] = None
    name: Optional[str] = None
    call_id: Optional[str] = None
    output: Optional[Any] = None


class ResponsesRequest(BaseModel):
    model: str = "gemini-auto"
    input: Union[str, List[Dict[str, Any]], List[ResponsesInputItem]]
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    stream: bool = False
    instructions: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    max_output_tokens: Optional[int] = None
    parallel_tool_calls: Optional[bool] = None


class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, List[Dict[str, Any]]]] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatRequestCompat(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"input_text", "output_text", "text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item_type == "text" and isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "\n".join(parts)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
    return ""


def normalize_responses_input(input_value: Union[str, List[Dict[str, Any]], List[ResponsesInputItem]], instructions: Optional[str]) -> List[ChatMessage]:
    messages: List[ChatMessage] = []
    if instructions:
        messages.append(ChatMessage(role="system", content=instructions))

    if isinstance(input_value, str):
        messages.append(ChatMessage(role="user", content=input_value))
        return messages

    for raw_item in input_value:
        item = raw_item if isinstance(raw_item, ResponsesInputItem) else ResponsesInputItem.model_validate(raw_item)
        if item.type == "function_call_output":
            if not item.call_id:
                raise HTTPException(status_code=400, detail="function_call_output items require call_id")
            output_text = _flatten_content(item.output)
            messages.append(ChatMessage(role="tool", tool_call_id=item.call_id, content=output_text))
            continue

        role = item.role
        if role in {"user", "assistant", "system", "developer"}:
            mapped_role = "system" if role == "developer" else role
            text = item.text if isinstance(item.text, str) else _flatten_content(item.content)
            messages.append(ChatMessage(role=mapped_role, content=text))
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported responses input item: role={role!r} type={item.type!r}")

    return messages


def responses_output_from_chat_message(message: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        output = []
        for tool_call in tool_calls:
            function = tool_call.get("function") or {}
            output.append({
                "type": "function_call",
                "id": tool_call.get("id"),
                "call_id": tool_call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments") or "{}",
                "status": "completed",
            })
        return output, ""

    text = message_text(message.get("content"))
    return ([{
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
        "status": "completed",
    }], text)


def build_responses_payload(response_id: str, model: str, created_at: int, output: List[Dict[str, Any]], output_text: str, status: str = "completed") -> Dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "created": created_at,
        "model": model,
        "status": status,
        "output": output,
        "output_text": output_text,
        "parallel_tool_calls": False,
    }


def build_error_payload(message: str, error_type: str = "invalid_request_error", status_code: int = 400) -> HTTPException:
    raise HTTPException(status_code=status_code, detail={"message": message, "type": error_type})


def maybe_handle_responses_request(req: ResponsesRequest):
    if req.parallel_tool_calls:
        build_error_payload("parallel_tool_calls is not supported yet", "not_supported_error", 501)

    if req.tool_choice not in (None, "auto", "required", "none") and not isinstance(req.tool_choice, dict):
        build_error_payload("Unsupported tool_choice value", "invalid_request_error", 400)

    if req.tools:
        for tool in req.tools:
            if tool.get("type") != "function":
                build_error_payload("Only function tools are supported", "invalid_request_error", 400)
            parameters = (tool.get("function") or {}).get("parameters")
            if parameters is not None and not isinstance(parameters, dict):
                build_error_payload("Tool parameters must be a JSON schema object", "invalid_request_error", 400)

    messages = normalize_responses_input(req.input, req.instructions)
    chat_req = ChatRequestCompat(
        model=req.model,
        messages=messages,
        stream=req.stream,
        temperature=req.temperature,
        top_p=req.top_p,
        tools=req.tools,
        tool_choice=req.tool_choice,
        parallel_tool_calls=req.parallel_tool_calls,
    )
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    chat_id = f"chatcmpl-{uuid.uuid4()}"
    chat_result = maybe_handle_openai_tool_request(chat_req, chat_id, created_at)
    if chat_result is None:
        return None

    if isinstance(chat_result, StreamingResponse):
        async def generator():
            async for chunk in chat_result.body_iterator:
                if isinstance(chunk, bytes):
                    yield chunk
                else:
                    yield chunk.encode("utf-8")
        return StreamingResponse(generator(), media_type="text/event-stream")

    message = chat_result["choices"][0]["message"]
    output, output_text = responses_output_from_chat_message(message)
    return build_responses_payload(response_id, req.model, created_at, output, output_text)

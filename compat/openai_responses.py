import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import HTTPException
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


SUPPORTED_TEXT_ITEM_TYPES = {"input_text", "output_text", "text"}
SUPPORTED_MESSAGE_ROLES = {"user", "assistant", "system", "developer"}


def build_error_payload(message: str, error_type: str = "invalid_request_error", status_code: int = 400) -> None:
    raise HTTPException(status_code=status_code, detail={"message": message, "type": error_type})


def _flatten_text_content(content: Any, field_name: str) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for index, item in enumerate(content):
            if not isinstance(item, dict):
                build_error_payload(f"{field_name}[{index}] must be an object")
            item_type = item.get("type")
            if item_type not in SUPPORTED_TEXT_ITEM_TYPES:
                build_error_payload(f"Unsupported {field_name}[{index}].type: {item_type!r}")
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            if item_type == "text" and isinstance(item.get("content"), str):
                parts.append(item["content"])
                continue
            build_error_payload(f"{field_name}[{index}] requires a text string")
        return "\n".join(parts)

    if isinstance(content, dict):
        item_type = content.get("type")
        if item_type is not None and item_type not in SUPPORTED_TEXT_ITEM_TYPES:
            build_error_payload(f"Unsupported {field_name}.type: {item_type!r}")
        text = content.get("text")
        if isinstance(text, str):
            return text
        if item_type == "text" and isinstance(content.get("content"), str):
            return content["content"]
        build_error_payload(f"{field_name} requires a text string")

    if content is None:
        build_error_payload(f"{field_name} is required")

    build_error_payload(f"{field_name} must be a string or text item list")


def _coerce_tool_output_text(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (int, float, bool)):
        return json.dumps(output, ensure_ascii=False)
    if isinstance(output, (list, dict)):
        try:
            return _flatten_text_content(output, "function_call_output.output")
        except HTTPException:
            return json.dumps(output, ensure_ascii=False)
    return str(output)


def validate_responses_request(req: ResponsesRequest) -> None:
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
                build_error_payload("function_call_output items require call_id")
            messages.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=item.call_id,
                    content=_coerce_tool_output_text(item.output),
                )
            )
            continue

        role = item.role
        if role in SUPPORTED_MESSAGE_ROLES:
            mapped_role = "system" if role == "developer" else role
            if isinstance(item.text, str):
                text = item.text
            else:
                text = _flatten_text_content(item.content, f"input[{len(messages)}].content")
            messages.append(ChatMessage(role=mapped_role, content=text))
            continue

        build_error_payload(f"Unsupported responses input item: role={role!r} type={item.type!r}")

    return messages


def chat_request_from_responses(req: ResponsesRequest) -> ChatRequestCompat:
    validate_responses_request(req)
    return ChatRequestCompat(
        model=req.model,
        messages=normalize_responses_input(req.input, req.instructions),
        stream=False,
        temperature=req.temperature,
        top_p=req.top_p,
        tools=req.tools,
        tool_choice=req.tool_choice,
        parallel_tool_calls=req.parallel_tool_calls,
    )


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


def responses_payload_from_chat_result(chat_result: Dict[str, Any], response_id: str, requested_model: Optional[str] = None, created_at: Optional[int] = None) -> Dict[str, Any]:
    message = chat_result["choices"][0]["message"]
    output, output_text = responses_output_from_chat_message(message)
    resolved_model = chat_result.get("model") or requested_model or "unknown"
    resolved_created_at = chat_result.get("created") or created_at or int(time.time())
    return build_responses_payload(response_id, resolved_model, resolved_created_at, output, output_text)


def maybe_handle_responses_request(
    req: ResponsesRequest,
    chat_req: Optional[ChatRequestCompat] = None,
    response_id: Optional[str] = None,
    created_at: Optional[int] = None,
):
    chat_req = chat_req or chat_request_from_responses(req)
    response_id = response_id or f"resp_{uuid.uuid4().hex}"
    created_at = created_at or int(time.time())
    chat_id = f"chatcmpl-{uuid.uuid4()}"
    chat_result = maybe_handle_openai_tool_request(chat_req, chat_id, created_at)
    if chat_result is None:
        return None
    return responses_payload_from_chat_result(chat_result, response_id, requested_model=req.model, created_at=created_at)

import json
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from compat.openai_responses import (
    ChatMessage,
    ChatRequestCompat,
    ResponsesRequest,
    build_error_payload,
    build_responses_payload,
    chat_request_from_responses,
    responses_output_from_chat_message,
)
from compat.tool_calling import infer_openai_tool_call, message_text
from core import storage

ChatExecutor = Callable[[ChatRequestCompat], Awaitable[Dict[str, Any]]]

MAX_RESPONSE_STEPS = 8


def _content_blocks(text: str, block_type: str) -> List[Dict[str, Any]]:
    return [{"type": block_type, "text": text}]


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return message_text(content)


def _message_content_blocks(content: Any, block_type: str) -> List[Dict[str, Any]]:
    text = _message_content_text(content)
    return _content_blocks(text, block_type)


def chat_message_to_item(message: ChatMessage, direction: str = "input") -> Dict[str, Any]:
    if message.role == "tool":
        text = _message_content_text(message.content)
        return {
            "direction": direction,
            "item_type": "function_call_output",
            "type": "function_call_output",
            "role": "tool",
            "call_id": message.tool_call_id,
            "content": _content_blocks(text, "output_text"),
            "text": text,
            "status": "completed",
            "created_at": time.time(),
        }

    text = _message_content_text(message.content)
    return {
        "direction": direction,
        "item_type": "message",
        "type": "message",
        "role": message.role,
        "content": _message_content_blocks(message.content, "input_text"),
        "text": text,
        "status": "completed",
        "created_at": time.time(),
    }


def output_items_to_storage_items(output_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for item in output_items:
        item_type = item.get("type") or "message"
        if item_type == "function_call":
            results.append({
                "direction": "output",
                "item_type": "function_call",
                "type": "function_call",
                "role": "assistant",
                "call_id": item.get("call_id") or item.get("id"),
                "name": item.get("name"),
                "arguments": _parse_arguments(item.get("arguments")),
                "content": None,
                "text": "",
                "status": item.get("status") or "completed",
                "created_at": time.time(),
            })
            continue

        content = item.get("content") or []
        text = ""
        if isinstance(content, list):
            text = "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
        results.append({
            "direction": "output",
            "item_type": "message",
            "type": "message",
            "role": item.get("role") or "assistant",
            "content": content,
            "text": text,
            "status": item.get("status") or "completed",
            "created_at": time.time(),
        })
    return results


def _parse_arguments(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except Exception:
            return arguments
    return arguments


def _tool_call_message_from_items(items: List[Dict[str, Any]]) -> ChatMessage:
    tool_calls = []
    for item in items:
        arguments = item.get("arguments")
        if isinstance(arguments, dict):
            arguments_text = json.dumps(arguments, ensure_ascii=False)
        elif isinstance(arguments, str):
            arguments_text = arguments
        else:
            arguments_text = "{}"
        tool_calls.append({
            "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": item.get("name"),
                "arguments": arguments_text,
            },
        })
    return ChatMessage(role="assistant", content=None, tool_calls=tool_calls)


def items_to_chat_messages(items: List[Dict[str, Any]]) -> List[ChatMessage]:
    messages: List[ChatMessage] = []
    pending_tool_calls: List[Dict[str, Any]] = []

    def flush_pending() -> None:
        nonlocal pending_tool_calls
        if pending_tool_calls:
            messages.append(_tool_call_message_from_items(pending_tool_calls))
            pending_tool_calls = []

    for item in items:
        direction = item.get("direction") or "output"
        item_type = item.get("item_type") or item.get("type")
        if direction == "output" and item_type == "function_call":
            pending_tool_calls.append(item)
            continue

        flush_pending()

        if item_type == "function_call_output":
            text = item.get("text") or _message_content_text(item.get("content"))
            messages.append(ChatMessage(role="tool", tool_call_id=item.get("call_id"), content=text))
            continue

        if item_type == "message":
            content = item.get("content")
            text = item.get("text") or _message_content_text(content)
            role = item.get("role") or ("assistant" if direction == "output" else "user")
            if not content:
                block_type = "output_text" if direction == "output" else "input_text"
                content = _content_blocks(text, block_type)
            messages.append(ChatMessage(role=role, content=text if isinstance(text, str) else content))
            continue

    flush_pending()
    return messages


def pending_function_calls(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    pending: Dict[str, Dict[str, Any]] = {}
    for item in items:
        item_type = item.get("item_type") or item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and call_id:
            pending[call_id] = item
        elif item_type == "function_call_output" and call_id:
            pending.pop(call_id, None)
    return pending


def validate_new_tool_outputs(chain_items: List[Dict[str, Any]], new_input_items: List[Dict[str, Any]]) -> None:
    pending_calls = pending_function_calls(chain_items)
    seen_new: set[str] = set()
    for item in new_input_items:
        item_type = item.get("item_type") or item.get("type")
        if item_type != "function_call_output":
            continue
        call_id = item.get("call_id")
        if not call_id:
            build_error_payload("function_call_output items require call_id")
        if call_id not in pending_calls:
            build_error_payload(f"Unknown or already completed function_call_output call_id: {call_id}")
        if call_id in seen_new:
            build_error_payload(f"Duplicate function_call_output for call_id: {call_id}")
        seen_new.add(call_id)


def merge_chain_items(chain_items: List[Dict[str, Any]], input_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = list(chain_items)
    merged.extend(input_items)
    return merged


def _conversation_key(parent_record: Optional[dict], response_id: str) -> str:
    if parent_record and parent_record.get("conversation_key"):
        return parent_record["conversation_key"]
    return f"response_graph:{response_id}"


def _request_config(req: ResponsesRequest) -> Dict[str, Any]:
    return {
        "tool_choice": req.tool_choice,
        "parallel_tool_calls": req.parallel_tool_calls,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_output_tokens": req.max_output_tokens,
        "stream": req.stream,
    }


def _response_record(req: ResponsesRequest, response_id: str, previous_response_id: Optional[str], parent_record: Optional[dict], status: str, step_count: int, created_at: int, error_json: Optional[dict] = None) -> Dict[str, Any]:
    now = time.time()
    return {
        "id": response_id,
        "previous_response_id": previous_response_id,
        "conversation_key": _conversation_key(parent_record, response_id),
        "model": req.model,
        "status": status,
        "request_config_json": _request_config(req),
        "metadata_json": req.metadata,
        "usage_json": None,
        "error_json": error_json,
        "step_count": step_count,
        "created_at": created_at,
        "updated_at": now,
    }


def _payload_status(output_items: List[Dict[str, Any]], error_json: Optional[dict] = None) -> str:
    if error_json is not None:
        return "failed"
    if any(item.get("type") == "function_call" for item in output_items):
        return "requires_action"
    return "completed"


def _response_payload(response_id: str, req: ResponsesRequest, created_at: int, output_items: List[Dict[str, Any]], previous_response_id: Optional[str], error_json: Optional[dict] = None) -> Dict[str, Any]:
    output_text = ""
    for item in output_items:
        if item.get("type") == "message":
            content = item.get("content") or []
            output_text = "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
            break
    payload = build_responses_payload(
        response_id=response_id,
        model=req.model,
        created_at=created_at,
        output=output_items,
        output_text=output_text,
        status=_payload_status(output_items, error_json),
    )
    payload["previous_response_id"] = previous_response_id
    if error_json is not None:
        payload["error"] = error_json
    return payload


def _sse_event(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_text(text: str, size: int = 80) -> List[str]:
    if not text:
        return []
    return [text[index:index + size] for index in range(0, len(text), size)]


def _stream_payload(payload: Dict[str, Any]) -> StreamingResponse:
    async def generator():
        response_id = payload["id"]
        yield _sse_event("response.created", {"response": {"id": response_id, "model": payload.get("model"), "status": "in_progress"}})
        for item in payload.get("output", []):
            yield _sse_event("response.output_item.added", {"response_id": response_id, "item": item})
            if item.get("type") == "function_call":
                arguments = item.get("arguments") or "{}"
                yield _sse_event(
                    "response.function_call_arguments.delta",
                    {
                        "response_id": response_id,
                        "item_id": item.get("id") or item.get("call_id"),
                        "call_id": item.get("call_id"),
                        "delta": arguments,
                    },
                )
            elif item.get("type") == "message":
                content = item.get("content") or []
                text = "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
                for delta in _chunk_text(text):
                    yield _sse_event(
                        "response.output_text.delta",
                        {
                            "response_id": response_id,
                            "item_id": item.get("id"),
                            "delta": delta,
                        },
                    )
            yield _sse_event("response.output_item.done", {"response_id": response_id, "item": item})
        terminal_event = "response.failed" if payload.get("status") == "failed" else "response.completed"
        yield _sse_event(terminal_event, {"response": payload})
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


async def handle_responses_request(
    req: ResponsesRequest,
    request: Request,
    chat_executor: ChatExecutor,
):
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    chat_req = chat_request_from_responses(req)

    parent_record = None
    chain_items: List[Dict[str, Any]] = []
    if req.previous_response_id:
        parent_record = await storage.load_response_record(req.previous_response_id)
        if parent_record is None:
            build_error_payload(f"previous_response_id not found: {req.previous_response_id}")
        loaded_chain = await storage.load_response_chain_items(req.previous_response_id)
        if loaded_chain is None:
            build_error_payload(f"Could not load response chain for: {req.previous_response_id}")
        chain_items = loaded_chain

    input_items = [chat_message_to_item(message, direction="input") for message in chat_req.messages]
    assistant_messages = [message for message in chat_req.messages if message.role == "assistant"]
    new_tool_outputs = [message for message in chat_req.messages if message.role == "tool"]
    if new_tool_outputs and not req.previous_response_id:
        build_error_payload("function_call_output continuation requires previous_response_id")
    if req.previous_response_id:
        validate_new_tool_outputs(chain_items, input_items)
    merged_items = merge_chain_items(chain_items, input_items)

    response_messages = items_to_chat_messages(merged_items)
    response_step_count = (parent_record or {}).get("step_count", 0) + 1
    if response_step_count > MAX_RESPONSE_STEPS:
        build_error_payload(
            f"responses step limit exceeded ({MAX_RESPONSE_STEPS})",
            error_type="not_supported_error",
            status_code=501,
        )

    output_items: List[Dict[str, Any]]
    payload_status = "completed"

    if req.tools and not new_tool_outputs and not assistant_messages:
        tool_call = infer_openai_tool_call(chat_req, response_id)
        if tool_call:
            output_items = [{
                "type": "function_call",
                "id": tool_call.get("id"),
                "call_id": tool_call.get("id"),
                "name": (tool_call.get("function") or {}).get("name"),
                "arguments": (tool_call.get("function") or {}).get("arguments") or "{}",
                "status": "completed",
            }]
            payload_status = "requires_action"
        else:
            model_req = ChatRequestCompat(
                model=chat_req.model,
                messages=response_messages,
                stream=False,
                temperature=chat_req.temperature,
                top_p=chat_req.top_p,
                tools=chat_req.tools,
                tool_choice=chat_req.tool_choice,
                parallel_tool_calls=chat_req.parallel_tool_calls,
            )
            try:
                chat_result = await chat_executor(model_req)
            except HTTPException as exc:
                error_json = {"message": str(exc.detail), "type": "runtime_error", "status_code": exc.status_code}
                await storage.save_response_record(
                    _response_record(
                        req=req,
                        response_id=response_id,
                        previous_response_id=req.previous_response_id,
                        parent_record=parent_record,
                        status="failed",
                        step_count=response_step_count,
                        created_at=created_at,
                        error_json=error_json,
                    )
                )
                await storage.replace_response_items(response_id, list(merged_items))
                payload = _response_payload(
                    response_id=response_id,
                    req=req,
                    created_at=created_at,
                    output_items=[],
                    previous_response_id=req.previous_response_id,
                    error_json=error_json,
                )
                if req.stream:
                    return _stream_payload(payload)
                raise
            output_items, _ = responses_output_from_chat_message(chat_result["choices"][0]["message"])
            if any(item.get("type") == "function_call" for item in output_items):
                payload_status = "requires_action"
    else:
        model_req = ChatRequestCompat(
            model=chat_req.model,
            messages=response_messages,
            stream=False,
            temperature=chat_req.temperature,
            top_p=chat_req.top_p,
            tools=chat_req.tools,
            tool_choice=chat_req.tool_choice,
            parallel_tool_calls=chat_req.parallel_tool_calls,
        )
        try:
            chat_result = await chat_executor(model_req)
        except HTTPException as exc:
            error_json = {"message": str(exc.detail), "type": "runtime_error", "status_code": exc.status_code}
            await storage.save_response_record(
                _response_record(
                    req=req,
                    response_id=response_id,
                    previous_response_id=req.previous_response_id,
                    parent_record=parent_record,
                    status="failed",
                    step_count=response_step_count,
                    created_at=created_at,
                    error_json=error_json,
                )
            )
            await storage.replace_response_items(response_id, list(merged_items))
            payload = _response_payload(
                response_id=response_id,
                req=req,
                created_at=created_at,
                output_items=[],
                previous_response_id=req.previous_response_id,
                error_json=error_json,
            )
            if req.stream:
                return _stream_payload(payload)
            raise
        output_items, _ = responses_output_from_chat_message(chat_result["choices"][0]["message"])
        if any(item.get("type") == "function_call" for item in output_items):
            payload_status = "requires_action"

    storage_items = list(merged_items)
    storage_items.extend(output_items_to_storage_items(output_items))
    await storage.save_response_record(
        _response_record(
            req=req,
            response_id=response_id,
            previous_response_id=req.previous_response_id,
            parent_record=parent_record,
            status=payload_status,
            step_count=response_step_count,
            created_at=created_at,
        )
    )
    await storage.replace_response_items(response_id, storage_items)

    payload = _response_payload(
        response_id=response_id,
        req=req,
        created_at=created_at,
        output_items=output_items,
        previous_response_id=req.previous_response_id,
    )

    if req.stream:
        return _stream_payload(payload)
    return payload

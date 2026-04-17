import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("gemini.compat.tool_calling")

ENABLE_OPENAI_TOOL_SHIM = os.getenv("ENABLE_OPENAI_TOOL_SHIM", "1") == "1"


def message_text(content: Optional[Union[str, List[Dict[str, Any]]]]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)
    return ""


def last_user_text(messages: List[Any]) -> str:
    for message in reversed(messages):
        role = getattr(message, "role", None) if not isinstance(message, dict) else message.get("role")
        if role == "user":
            content = getattr(message, "content", None) if not isinstance(message, dict) else message.get("content")
            return message_text(content).strip()
    return ""


def user_signal_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text.strip()
    return lines[-1]


def extract_path_hint(text: str) -> Optional[str]:
    quoted = re.findall(r"[`'\"]([^`'\"]+)[`'\"]", text)
    for candidate in quoted:
        normalized = candidate.strip()
        if not normalized or "\n" in normalized or "...existing code..." in normalized:
            continue
        if " " in normalized:
            continue
        if "/" in normalized or "." in normalized:
            return normalized
    file_match = re.search(r"\b[\w./-]+\.[A-Za-z0-9_]+\b", text)
    if file_match:
        normalized = file_match.group(0).strip()
        if "...existing code..." not in normalized:
            return normalized
    return None


def absolute_path_hint(path_hint: Optional[str]) -> Optional[str]:
    if not path_hint:
        return None
    path = Path(path_hint)
    return str(path if path.is_absolute() else path.resolve())


def looks_like_file_path(path_hint: Optional[str]) -> bool:
    if not path_hint:
        return False
    base = os.path.basename(path_hint)
    return "." in base and not path_hint.endswith("/")


def looks_like_directory_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ["list", "ls", "dir", "directory", "folder", "thư mục"])


def looks_like_file_read_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ["first line", "read", "open", "show", "nội dung", "đọc", "file"])


def looks_like_search_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ["search", "find", "grep", "tìm"])


def looks_like_pwd_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ["pwd", "working directory", "workdir", "current directory"])


def looks_like_inline_code_blob(text: str) -> bool:
    lowered = text.lower()
    return "...existing code..." in lowered or len(text) > 500


def looks_like_edit_request(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ["edit", "replace", "change", "insert", "add", "remove", "write", "update", "sửa", "thêm", "xóa", "ghi"])


def should_avoid_tool_call(user_text: str, path_hint: Optional[str]) -> bool:
    return looks_like_inline_code_blob(user_text) and not path_hint


def tool_properties(tool: Dict[str, Any]) -> Dict[str, Any]:
    function = tool.get("function") or {}
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties")
    return properties if isinstance(properties, dict) else {}


def tool_required(tool: Dict[str, Any]) -> List[str]:
    function = tool.get("function") or {}
    parameters = function.get("parameters") or {}
    required = parameters.get("required")
    return [item for item in required if isinstance(item, str)] if isinstance(required, list) else []


def preferred_tool_field(tool: Dict[str, Any], candidates: List[str], fallback: str) -> str:
    properties = tool_properties(tool)
    required = tool_required(tool)
    for candidate in candidates:
        if candidate in required:
            return candidate
    for candidate in candidates:
        if candidate in properties:
            return candidate
    return fallback


def can_call_read_file(tool: Dict[str, Any]) -> bool:
    required = set(tool_required(tool))
    return not any(field in required for field in ["startLine", "start_line", "endLine", "end_line", "line"])


def build_path_arguments(tool: Dict[str, Any], path_hint: Optional[str], candidates: List[str], fallback: str) -> Optional[Dict[str, Any]]:
    absolute = absolute_path_hint(path_hint)
    if not absolute:
        return None
    field = preferred_tool_field(tool, candidates, fallback)
    return {field: absolute}


def build_search_arguments(tool: Dict[str, Any], user_text: str, path_hint: Optional[str]) -> Dict[str, Any]:
    pattern_match = re.search(r"(?:search|find|grep|tìm)\s+(?:for\s+)?[`'\"]?([^`'\"\n]+)[`'\"]?", user_text, re.IGNORECASE)
    pattern = pattern_match.group(1).strip() if pattern_match else user_text.strip()
    pattern_field = preferred_tool_field(tool, ["pattern", "query", "text"], "pattern")
    arguments: Dict[str, Any] = {pattern_field: pattern}
    absolute = absolute_path_hint(path_hint)
    if absolute:
        path_field = preferred_tool_field(tool, ["filePath", "dirPath", "path"], "path")
        arguments[path_field] = absolute
    return arguments


def build_pwd_arguments(tool: Dict[str, Any]) -> Dict[str, Any]:
    command_field = preferred_tool_field(tool, ["command", "cmd"], "command")
    return {command_field: "pwd"}


def line_number_from_text(text: str) -> Optional[int]:
    match = re.search(r"(?:line|dòng)\s+(\d+)", text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def validate_tool_arguments(tool: Dict[str, Any], arguments: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    parameters = (tool.get("function") or {}).get("parameters") or {}
    properties = parameters.get("properties") if isinstance(parameters, dict) else {}
    required = tool_required(tool)

    for field in required:
        if field not in arguments:
            return False, f"missing required field '{field}'"

    if isinstance(properties, dict):
        additional_properties = parameters.get("additionalProperties", True)
        if additional_properties is False:
            extra_fields = [key for key in arguments.keys() if key not in properties]
            if extra_fields:
                return False, f"unexpected field(s): {', '.join(extra_fields)}"

    return True, None


def build_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def log_incoming_tools(request_id: str, tools: Optional[List[Dict[str, Any]]], tool_choice: Any, parallel_tool_calls: Any) -> None:
    if not tools:
        return
    tool_summaries = []
    for tool in tools[:20]:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") if isinstance(parameters, dict) else {}
        required = parameters.get("required") if isinstance(parameters, dict) else []
        tool_summaries.append({
            "name": function.get("name"),
            "required": required if isinstance(required, list) else [],
            "properties": sorted(list(properties.keys())) if isinstance(properties, dict) else [],
        })
    logger.info(
        "[OPENAI_TOOL_SHIM] [req_%s] incoming_tools=%s tool_choice=%s parallel_tool_calls=%s",
        request_id,
        json.dumps(tool_summaries, ensure_ascii=False),
        json.dumps(tool_choice, ensure_ascii=False),
        parallel_tool_calls,
    )


def log_outgoing_tool_call(request_id: str, tool_call: Optional[Dict[str, Any]], reason: str) -> None:
    logger.info(
        "[OPENAI_TOOL_SHIM] [req_%s] decision=%s tool_call=%s",
        request_id,
        reason,
        json.dumps(tool_call, ensure_ascii=False) if tool_call else "null",
    )


def range_arguments(tool: Dict[str, Any], path_hint: Optional[str], text: str) -> Optional[Dict[str, Any]]:
    properties = tool_properties(tool)
    required = tool_required(tool)
    if not properties or not path_hint or not looks_like_edit_request(text):
        return None

    arguments: Dict[str, Any] = {}
    path_field = preferred_tool_field(tool, ["filePath", "path", "relativePath"], "filePath")
    arguments[path_field] = path_hint

    line_value = line_number_from_text(text)
    if line_value is None and any(field in required for field in ["startLine", "line", "endLine"]):
        return None

    for field in ["startLine", "line", "endLine"]:
        if field in properties and line_value is not None:
            arguments[field] = line_value
        camel = field[0].lower() + field[1:]
        if camel in properties and line_value is not None:
            arguments[camel] = line_value

    for field in ["oldString", "newString", "content", "code", "explanation"]:
        if field in properties:
            arguments[field] = ""

    valid, _ = validate_tool_arguments(tool, arguments)
    return arguments if valid else None


def first_schema_compatible_tool_call(available: Dict[str, Dict[str, Any]], path_hint: Optional[str], user_text: str) -> Optional[Dict[str, Any]]:
    if not looks_like_edit_request(user_text):
        return None
    for tool in available.values():
        arguments = range_arguments(tool, absolute_path_hint(path_hint), user_text)
        if arguments is None:
            continue
        valid, reason = validate_tool_arguments(tool, arguments)
        if valid:
            return build_tool_call(tool.get("function", {}).get("name"), arguments)
        logger.info("[OPENAI_TOOL_SHIM] schema fallback rejected: %s", reason)
    return None


def infer_openai_tool_call(req: Any, request_id: str = "") -> Optional[Dict[str, Any]]:
    if not ENABLE_OPENAI_TOOL_SHIM or not getattr(req, "tools", None):
        log_outgoing_tool_call(request_id, None, "shim_disabled_or_no_tools")
        return None

    if getattr(req, "parallel_tool_calls", None):
        log_outgoing_tool_call(request_id, None, "parallel_tool_calls_not_supported")
        return None

    available = {
        tool.get("function", {}).get("name"): tool
        for tool in req.tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    user_text = last_user_text(req.messages)
    signal_text = user_signal_text(user_text)
    path_hint = absolute_path_hint(extract_path_hint(signal_text))

    if should_avoid_tool_call(signal_text, path_hint):
        log_outgoing_tool_call(request_id, None, "inline_code_blob")
        return None

    if path_hint and looks_like_file_read_request(signal_text):
        tool = available.get("read_file")
        if tool and can_call_read_file(tool):
            arguments = build_path_arguments(tool, path_hint, ["filePath", "path"], "path")
            valid, reason = validate_tool_arguments(tool, arguments or {})
            if valid and arguments:
                tool_call = build_tool_call("read_file", arguments)
                log_outgoing_tool_call(request_id, tool_call, "read_file_by_path_hint")
                return tool_call
            logger.info("[OPENAI_TOOL_SHIM] read_file rejected: %s", reason)

    if path_hint and looks_like_directory_request(signal_text) and not looks_like_file_path(path_hint):
        tool = available.get("list_dir")
        if tool:
            arguments = build_path_arguments(tool, path_hint, ["dirPath", "filePath", "path"], "path")
            valid, reason = validate_tool_arguments(tool, arguments or {})
            if valid and arguments:
                tool_call = build_tool_call("list_dir", arguments)
                log_outgoing_tool_call(request_id, tool_call, "list_dir_by_path_hint")
                return tool_call
            logger.info("[OPENAI_TOOL_SHIM] list_dir rejected: %s", reason)

    if looks_like_search_request(signal_text):
        for name in ["grep_search", "search_in_files"]:
            tool = available.get(name)
            if not tool:
                continue
            arguments = build_search_arguments(tool, signal_text, path_hint)
            valid, reason = validate_tool_arguments(tool, arguments)
            if valid:
                tool_call = build_tool_call(name, arguments)
                log_outgoing_tool_call(request_id, tool_call, f"{name}_by_text_signal")
                return tool_call
            logger.info("[OPENAI_TOOL_SHIM] %s rejected: %s", name, reason)

    if looks_like_pwd_request(signal_text):
        tool = available.get("run_command")
        if tool:
            arguments = build_pwd_arguments(tool)
            valid, reason = validate_tool_arguments(tool, arguments)
            if valid:
                tool_call = build_tool_call("run_command", arguments)
                log_outgoing_tool_call(request_id, tool_call, "pwd_request")
                return tool_call
            logger.info("[OPENAI_TOOL_SHIM] run_command rejected: %s", reason)

    fallback = first_schema_compatible_tool_call(available, path_hint, signal_text)
    if fallback is not None:
        log_outgoing_tool_call(request_id, fallback, "schema_fallback")
        return fallback

    log_outgoing_tool_call(request_id, None, "no_safe_match")
    return None

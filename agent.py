#!/usr/bin/env python3

import fnmatch
import json
import os
import readline
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

BASE_URL = os.getenv("GEMINI_API_BASE", "http://localhost:7860/v1")
API_KEY = os.getenv("GEMINI_API_KEY", "your-api-key")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
WORKDIR = Path(os.getenv("AGENT_WORKDIR", os.getcwd())).resolve()
MAX_STEPS = 20
MAX_FILE_SIZE = 500_000
MAX_SEARCH_FILE_SIZE = 200_000
MAX_LIST_ITEMS = 200
MAX_SEARCH_FILES = 20
MAX_MATCHES_PER_FILE = 5
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse"}
BLOCKED_COMMAND_TOKENS = ["&&", "||", "|", ";", ">", "<", "`"]
FALLBACK_HINTS = (
    "upload",
    "uploaded file",
    "provided as an uploaded file",
    "cannot directly read",
    "could not find the file",
    "current working directory appears to be empty",
)

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

TOOL_DEFINITIONS = {
    "read_file": {
        "description": "Read a UTF-8 text file inside AGENT_WORKDIR and return numbered lines.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside AGENT_WORKDIR."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "description": "Create or overwrite a UTF-8 text file inside AGENT_WORKDIR.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside AGENT_WORKDIR."},
                "content": {"type": "string", "description": "Full UTF-8 file content."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "edit_file": {
        "description": "Replace one exact text block in a file. Only works when old_str matches exactly once.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside AGENT_WORKDIR."},
                "old_str": {"type": "string", "description": "Exact text to replace."},
                "new_str": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_str", "new_str"],
            "additionalProperties": False,
        },
    },
    "list_dir": {
        "description": "List files and directories inside AGENT_WORKDIR.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path inside AGENT_WORKDIR.", "default": "."},
            },
            "additionalProperties": False,
        },
    },
    "search_in_files": {
        "description": "Search for a literal text pattern in text files inside AGENT_WORKDIR.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Literal text to search for."},
                "path": {"type": "string", "description": "Relative file or directory path inside AGENT_WORKDIR.", "default": "."},
                "file_pattern": {"type": "string", "description": "Glob filter such as *.py.", "default": "*"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    "run_command": {
        "description": "Run a restricted local command inside AGENT_WORKDIR. Allowed: python/python3 script.py, python -m py_compile, pytest, git(read-only), ls, pwd.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command string to run."},
                "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 30},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    }
    for name, spec in TOOL_DEFINITIONS.items()
]

JSON_MODE_SYSTEM_PROMPT = f"""You are a minimal local coding agent.

Workspace root: {WORKDIR}

You can only use these local tools:
{json.dumps({name: spec['parameters'] for name, spec in TOOL_DEFINITIONS.items()}, ensure_ascii=False)}

Rules:
- Work only inside AGENT_WORKDIR.
- Read files before editing them.
- Use the available tools instead of describing imaginary actions.
- If a path or command is blocked, explain that in a final message.
- Reply with exactly one JSON object and nothing else.
- Never wrap the JSON in prose.

Valid responses:
{{"type":"tool_call","name":"read_file","arguments":{{"path":"agent.py"}}}}
{{"type":"final","message":"Done. I updated agent.py and verified it."}}
"""

JSON_TOOL_RESULT_TEMPLATE = """Tool result:
name: {name}
arguments: {arguments}
result:
{result}

Decide the next step and reply with exactly one JSON object."""

INITIAL_MESSAGES = [{"role": "system", "content": ""}]
SYSTEM_PROMPT = f"""You are a minimal local coding agent.

Workspace root: {WORKDIR}

Rules:
- Work only inside AGENT_WORKDIR.
- Use the provided tools whenever you need to inspect files, edit files, search, list directories, or run allowed commands.
- Read files before editing them.
- Do not invent tool results.
- If a path or command is blocked, explain that clearly to the user.
- Keep answers concise and focus on completing the task safely.
"""
INITIAL_MESSAGES[0]["content"] = SYSTEM_PROMPT

RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
BLUE = "\033[94m"


class ActionParseError(ValueError):
    pass


def relative_display(path: Path) -> str:
    try:
        return str(path.relative_to(WORKDIR)) or "."
    except ValueError:
        return str(path)


def resolve(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Path must be a non-empty string")
    candidate = Path(path.strip())
    if not candidate.is_absolute():
        candidate = WORKDIR / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(WORKDIR)
    except ValueError as exc:
        raise ValueError(f"Path nằm ngoài AGENT_WORKDIR: {path}") from exc
    return resolved


def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(1024)
    except OSError:
        return False


def read_text(path: Path, max_size: int = MAX_FILE_SIZE) -> str:
    if not path.exists():
        raise ValueError(f"File không tồn tại: {relative_display(path)}")
    if not path.is_file():
        raise ValueError(f"Không phải file: {relative_display(path)}")
    size = path.stat().st_size
    if size > max_size:
        raise ValueError(f"File quá lớn: {relative_display(path)} ({size} bytes)")
    if is_binary_file(path):
        raise ValueError(f"File có vẻ là binary: {relative_display(path)}")
    return path.read_text(encoding="utf-8", errors="replace")


def iter_files(base: Path):
    if base.is_file():
        yield base
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            yield Path(root) / name


def maybe_validate_command_path(arg: str) -> None:
    if not arg or arg.startswith("-"):
        return
    looks_like_path = arg in {".", ".."} or "/" in arg or arg.startswith(".") or "." in Path(arg).name
    if looks_like_path:
        resolve(arg)


def validate_python_command(args: list[str]) -> None:
    if len(args) < 2:
        raise ValueError("python command cần script hoặc -m py_compile")
    if args[1] == "-m":
        if len(args) < 4 or args[2] != "py_compile":
            raise ValueError("python -m chỉ cho phép py_compile")
        for arg in args[3:]:
            maybe_validate_command_path(arg)
        return
    if args[1].startswith("-"):
        raise ValueError("python command chỉ cho phép script.py hoặc -m py_compile")
    resolve(args[1])


def validate_git_command(args: list[str]) -> None:
    if len(args) < 2:
        raise ValueError("git command thiếu subcommand")
    subcommand = args[1]
    if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
        raise ValueError(f"git subcommand không được phép: {subcommand}")
    for arg in args[2:]:
        maybe_validate_command_path(arg)


def validate_ls_command(args: list[str]) -> None:
    for arg in args[1:]:
        maybe_validate_command_path(arg)


def validate_pytest_command(args: list[str]) -> None:
    for arg in args[1:]:
        maybe_validate_command_path(arg)


def validate_command(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command phải là chuỗi không rỗng")
    if any(token in command for token in BLOCKED_COMMAND_TOKENS):
        raise ValueError("command chứa shell metacharacter bị chặn")
    args = shlex.split(command)
    if not args:
        raise ValueError("command rỗng")
    program = args[0]
    if program in {"python", "python3"}:
        validate_python_command(args)
    elif program == "pytest":
        validate_pytest_command(args)
    elif program == "git":
        validate_git_command(args)
    elif program == "ls":
        validate_ls_command(args)
    elif program == "pwd":
        if len(args) != 1:
            raise ValueError("pwd không nhận tham số")
    else:
        raise ValueError(f"command không được phép: {program}")
    return args


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def tool_read_file(path: str) -> str:
    try:
        target = resolve(path)
        content = read_text(target)
        if content == "":
            return f"FILE {relative_display(target)} is empty"
        lines = content.splitlines()
        numbered = "\n".join(f"{index:4d} | {line}" for index, line in enumerate(lines, 1))
        return f"FILE {relative_display(target)} ({len(lines)} lines)\n\n{numbered}"
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_write_file(path: str, content: str) -> str:
    try:
        if not isinstance(content, str):
            raise ValueError("content phải là chuỗi")
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        line_count = content.count("\n") + (1 if content else 0)
        action = "updated" if existed else "created"
        return f"OK: {action} {relative_display(target)} ({line_count} lines)"
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_edit_file(path: str, old_str: str, new_str: str) -> str:
    try:
        if not isinstance(old_str, str) or not isinstance(new_str, str):
            raise ValueError("old_str và new_str phải là chuỗi")
        target = resolve(path)
        content = read_text(target)
        count = content.count(old_str)
        if count == 0:
            raise ValueError(f"Không tìm thấy old_str trong {relative_display(target)}")
        if count > 1:
            raise ValueError(f"old_str xuất hiện {count} lần; cần context chính xác hơn")
        target.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        return f"OK: edited {relative_display(target)}"
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_list_dir(path: str = ".") -> str:
    try:
        target = resolve(path)
        if not target.exists():
            raise ValueError(f"Thư mục không tồn tại: {path}")
        if not target.is_dir():
            raise ValueError(f"Không phải thư mục: {path}")
        items = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        lines = []
        for item in items[:MAX_LIST_ITEMS]:
            rel = relative_display(item)
            if item.is_dir():
                lines.append(f"DIR  {rel}/")
            else:
                size = item.stat().st_size
                lines.append(f"FILE {rel} ({size} bytes)")
        return f"LIST {relative_display(target)} ({len(items)} items)\n" + "\n".join(lines)
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_search_in_files(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
    try:
        if not isinstance(pattern, str) or pattern == "":
            raise ValueError("pattern phải là chuỗi không rỗng")
        target = resolve(path)
        if not target.exists():
            raise ValueError(f"Path không tồn tại: {path}")

        matched_files: list[str] = []
        for candidate in iter_files(target):
            rel = relative_display(candidate)
            if not fnmatch.fnmatch(rel, file_pattern):
                continue
            if candidate.stat().st_size > MAX_SEARCH_FILE_SIZE or is_binary_file(candidate):
                continue
            try:
                lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            matches = []
            for index, line in enumerate(lines, 1):
                if pattern in line:
                    matches.append(f"{index}: {line}")
                    if len(matches) >= MAX_MATCHES_PER_FILE:
                        break
            if matches:
                matched_files.append(f"FILE {rel}\n" + "\n".join(matches))
                if len(matched_files) >= MAX_SEARCH_FILES:
                    break

        if not matched_files:
            return f"No matches for {pattern!r} in {relative_display(target)}"
        return f"SEARCH {pattern!r}\n\n" + "\n\n".join(matched_files)
    except Exception as exc:
        return f"ERROR: {exc}"


def tool_run_command(command: str, timeout: int = 30) -> str:
    try:
        timeout = max(1, min(int(timeout), 120))
        args = validate_command(command)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKDIR),
            shell=False,
        )
        stdout = truncate(result.stdout.strip(), 3000) if result.stdout else ""
        stderr = truncate(result.stderr.strip(), 1000) if result.stderr else ""
        parts = [f"EXIT CODE: {result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if not stdout and not stderr:
            parts.append("(no output)")
        return "\n\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"ERROR: command timeout after {timeout}s"
    except Exception as exc:
        return f"ERROR: {exc}"


TOOL_MAP = {
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "list_dir": tool_list_dir,
    "search_in_files": tool_search_in_files,
    "run_command": tool_run_command,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    tool = TOOL_MAP.get(name)
    if tool is None:
        return f"ERROR: Tool không tồn tại: {name}"
    try:
        return tool(**arguments)
    except TypeError as exc:
        return f"ERROR: Tham số tool không hợp lệ: {exc}"


def build_assistant_message(message: Any) -> dict[str, Any]:
    assistant_message: dict[str, Any] = {"role": "assistant"}
    if message.content is not None:
        assistant_message["content"] = message.content
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        assistant_message["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": tool_call.type,
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            for tool_call in tool_calls
        ]
    return assistant_message


def parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    if not raw_arguments:
        return {}
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Tool arguments không phải JSON hợp lệ: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments phải là object JSON")
    return arguments


def append_tool_result(messages: list[dict[str, Any]], tool_call_id: str, result: str) -> None:
    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": result})


def get_final_text(message: Any) -> str:
    if isinstance(message.content, str) and message.content.strip():
        return message.content.strip()
    return "Done."


def has_tool_calls(message: Any) -> bool:
    return bool(getattr(message, "tool_calls", None))


def tool_call_name(tool_call: Any) -> str:
    return tool_call.function.name


def tool_call_arguments(tool_call: Any) -> str:
    return tool_call.function.arguments or "{}"


def tool_call_id(tool_call: Any) -> str:
    return tool_call.id


def tool_preview(result: str) -> str:
    return truncate(result, 200)


def print_tool_call(step: int, name: str, arguments: dict[str, Any]) -> None:
    print(f"\n{YELLOW}[{step}] {name}{RESET} {DIM}{json.dumps(arguments, ensure_ascii=False)}{RESET}")


def print_tool_result(result: str) -> None:
    print(f"{DIM}{tool_preview(result)}{RESET}")


def format_tool_argument_error(raw_arguments: str, exc: Exception) -> str:
    preview = truncate(raw_arguments, 400)
    return f"ERROR: {exc}\nRaw arguments:\n{preview}"


def model_error_message(exc: Exception) -> str:
    return f"Model request failed: {exc}"


def extract_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        candidates.append(text[first_brace:last_brace + 1])
    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def parse_action(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ActionParseError("Model không trả về nội dung")
    for candidate in extract_json_candidates(text):
        try:
            action = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(action, dict):
            raise ActionParseError("JSON phải là object")
        action_type = action.get("type")
        if action_type == "tool_call":
            name = action.get("name")
            arguments = action.get("arguments")
            if name not in TOOL_MAP:
                raise ActionParseError(f"Tool không hợp lệ: {name}")
            if not isinstance(arguments, dict):
                raise ActionParseError("arguments phải là object")
            return action
        if action_type == "final":
            if not isinstance(action.get("message"), str):
                raise ActionParseError("final.message phải là chuỗi")
            return action
        raise ActionParseError(f"type không hợp lệ: {action_type}")
    raise ActionParseError("Không parse được JSON action hợp lệ")


def should_fallback_to_json(messages: list[dict[str, Any]], message: Any) -> bool:
    if has_tool_calls(message):
        return False
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    if not isinstance(last_user, str):
        return False
    lowered_user = last_user.lower()
    likely_needed = any(token in lowered_user for token in ("read ", "open ", "edit ", "search", "find", "agent.py", "pwd", "list ", "đọc", "tìm"))
    content = message.content if isinstance(message.content, str) else ""
    lowered_content = content.lower()
    backend_hint = any(hint in lowered_content for hint in FALLBACK_HINTS)
    return likely_needed and backend_hint


def run_native_model(messages: list[dict[str, Any]]):
    return client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=OPENAI_TOOLS,
        tool_choice="auto",
        stream=False,
    )


def run_json_mode_model(messages: list[dict[str, str]]):
    return client.chat.completions.create(model=MODEL, messages=messages, stream=False)


def run_agent_json_fallback(messages: list[dict[str, Any]], max_steps: int = MAX_STEPS) -> str:
    json_messages: list[dict[str, str]] = [{"role": "system", "content": JSON_MODE_SYSTEM_PROMPT}]
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool":
            json_messages.append({
                "role": "user",
                "content": JSON_TOOL_RESULT_TEMPLATE.format(
                    name="tool",
                    arguments=json.dumps({"tool_call_id": message.get("tool_call_id")}, ensure_ascii=False),
                    result=message.get("content", ""),
                ),
            })
            continue
        content = message.get("content")
        if isinstance(content, str):
            json_messages.append({"role": role, "content": content})

    step = 0
    invalid_json_attempts = 0
    while step < max_steps:
        step += 1
        response = run_json_mode_model(json_messages)
        content = response.choices[0].message.content or ""
        try:
            action = parse_action(content)
            invalid_json_attempts = 0
        except ActionParseError as exc:
            invalid_json_attempts += 1
            preview = truncate(content.strip() or "(empty response)", 400)
            json_messages.append({"role": "assistant", "content": content or ""})
            if invalid_json_attempts >= 2:
                return f"Model không trả về JSON hợp lệ sau {invalid_json_attempts} lần.\n\nRaw response:\n{preview}\n\nError: {exc}"
            json_messages.append({
                "role": "user",
                "content": "Your previous response was not valid JSON. Reply with exactly one JSON object using either {\"type\":\"tool_call\",...} or {\"type\":\"final\",...}. Do not add prose.",
            })
            continue

        json_messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        if action["type"] == "final":
            return action["message"]

        tool_name = action["name"]
        arguments = action["arguments"]
        print_tool_call(step, tool_name, arguments)
        result = execute_tool(tool_name, arguments)
        print_tool_result(result)
        json_messages.append({
            "role": "user",
            "content": JSON_TOOL_RESULT_TEMPLATE.format(
                name=tool_name,
                arguments=json.dumps(arguments, ensure_ascii=False),
                result=result,
            ),
        })

    return f"Đã đạt giới hạn {max_steps} bước."


def run_agent_native(messages: list[dict[str, Any]], max_steps: int = MAX_STEPS) -> str:
    step = 0
    while step < max_steps:
        step += 1
        try:
            response = run_native_model(messages)
        except Exception as exc:
            return model_error_message(exc)

        message = response.choices[0].message
        if should_fallback_to_json(messages, message):
            print(f"{DIM}Backend không hỗ trợ native tool calling cho yêu cầu này, chuyển sang JSON fallback.{RESET}")
            return run_agent_json_fallback(messages, max_steps=max_steps)

        messages.append(build_assistant_message(message))
        if not has_tool_calls(message):
            return get_final_text(message)

        for tool_call in message.tool_calls:
            name = tool_call_name(tool_call)
            raw_arguments = tool_call_arguments(tool_call)
            try:
                arguments = parse_tool_arguments(raw_arguments)
            except ValueError as exc:
                result = format_tool_argument_error(raw_arguments, exc)
                print_tool_call(step, name, {"raw_arguments": raw_arguments})
                print_tool_result(result)
                append_tool_result(messages, tool_call_id(tool_call), result)
                continue

            print_tool_call(step, name, arguments)
            result = execute_tool(name, arguments)
            print_tool_result(result)
            append_tool_result(messages, tool_call_id(tool_call), result)

    return f"Đã đạt giới hạn {max_steps} bước."


def run_agent(messages: list[dict[str, Any]], max_steps: int = MAX_STEPS) -> str:
    return run_agent_native(messages, max_steps=max_steps)


def initial_messages() -> list[dict[str, Any]]:
    return [message.copy() for message in INITIAL_MESSAGES]


def build_cli_messages(task: str) -> list[dict[str, Any]]:
    messages = initial_messages()
    messages.append({"role": "user", "content": task})
    return messages


def reset_messages() -> list[dict[str, Any]]:
    return initial_messages()


def create_interactive_messages() -> list[dict[str, Any]]:
    return initial_messages()


def print_banner() -> None:
    print(
        f"""
{CYAN}{BOLD}Gemini Minimal Coding Agent{RESET}
{DIM}API: {BASE_URL}
Model: {MODEL}
Workdir: {WORKDIR}
Commands: exit, clear, workdir{RESET}
"""
    )


def main() -> None:
    print_banner()
    messages = create_interactive_messages()

    while True:
        try:
            user_input = input(f"\n{GREEN}{BOLD}You>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Tạm biệt.{RESET}")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "bye", "thoát"}:
            print(f"{DIM}Tạm biệt.{RESET}")
            break
        if user_input.lower() in {"clear", "xóa"}:
            messages = reset_messages()
            print(f"{DIM}Đã xóa lịch sử hội thoại.{RESET}")
            continue
        if user_input.lower() == "workdir":
            print(f"{DIM}{WORKDIR}{RESET}")
            continue

        messages.append({"role": "user", "content": user_input})
        print(f"\n{CYAN}Agent đang xử lý...{RESET}")

        try:
            response = run_agent(messages)
            print(f"\n{BLUE}{BOLD}Agent>{RESET} {response}")
        except Exception as exc:
            print(f"\n{RED}Lỗi: {exc}{RESET}")
            if messages and messages[-1]["role"] == "user":
                messages.pop()


def run_responses_cli(task: str | None = None) -> int:
    try:
        from cli.agent import main as cli_main
    except Exception:
        return 1
    argv = []
    if task:
        argv.append(task)
    return cli_main(argv)


if __name__ == "__main__":
    if os.getenv("AGENT_USE_RESPONSES_CLI") == "1":
        task = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
        raise SystemExit(run_responses_cli(task))
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        messages = build_cli_messages(task)
        print(f"{CYAN}Chạy task: {task}{RESET}\n")
        print(run_agent(messages))
    else:
        main()

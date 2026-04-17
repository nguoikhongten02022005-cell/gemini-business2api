import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

WORKDIR = Path(os.getenv("AGENT_WORKDIR", os.getcwd())).resolve()
MAX_FILE_SIZE = 500_000
MAX_SEARCH_FILE_SIZE = 200_000
MAX_LIST_ITEMS = 200
MAX_SEARCH_FILES = 50
MAX_MATCHES_PER_FILE = 10
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}
READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse"}
BLOCKED_COMMAND_TOKENS = ["&&", "||", "|", ";", ">", "<", "`"]
TRANSCRIPTS_DIR = WORKDIR / "data" / "agent_runs"
APPROVAL_MODES = {"read-only", "ask-for-approval", "full-auto"}
READ_ONLY_TOOLS = {"read_file", "list_dir", "grep_search", "glob_search", "git_status", "git_diff"}
MUTATING_TOOLS = {"write_file", "edit_file", "multi_edit", "format"}
COMMAND_TOOLS = {"run_command", "run_tests", "lint", "format"}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the workspace and return numbered lines.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a UTF-8 text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact text block in a file. Fails unless the old string matches exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_str": {"type": "string"},
                    "new_str": {"type": "string"},
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": "Apply multiple exact replacements to one file in order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_str": {"type": "string"},
                                "new_str": {"type": "string"},
                            },
                            "required": ["old_str", "new_str"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "edits"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search for a literal text pattern in workspace files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "file_pattern": {"type": "string", "default": "*"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_search",
            "description": "Find files matching a glob pattern inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a guarded local command inside the workspace. Only safe commands are allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git working tree status.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show git diff, optionally for a single path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Run the project's test command, defaulting to pytest when available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lint",
            "description": "Run the repository lint command or a detected default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "format",
            "description": "Run the repository formatter command or a detected default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 120},
                },
                "additionalProperties": False,
            },
        },
    },
]


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
        raise ValueError(f"Path is outside workspace: {path}") from exc
    return resolved


def is_binary_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(1024)
    except OSError:
        return False


def read_text(path: Path, max_size: int = MAX_FILE_SIZE) -> str:
    if not path.exists():
        raise ValueError(f"File does not exist: {relative_display(path)}")
    if not path.is_file():
        raise ValueError(f"Not a file: {relative_display(path)}")
    size = path.stat().st_size
    if size > max_size:
        raise ValueError(f"File too large: {relative_display(path)} ({size} bytes)")
    if is_binary_file(path):
        raise ValueError(f"Binary file is not supported: {relative_display(path)}")
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


def validate_command(command: str) -> List[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if any(token in command for token in BLOCKED_COMMAND_TOKENS):
        raise ValueError("shell metacharacters are blocked")
    args = shlex.split(command)
    if not args:
        raise ValueError("command is empty")
    program = args[0]
    if program == "git":
        if len(args) < 2 or args[1] not in READ_ONLY_GIT_SUBCOMMANDS:
            raise ValueError("Only read-only git subcommands are allowed")
        for arg in args[2:]:
            maybe_validate_command_path(arg)
        return args
    if program == "ls":
        for arg in args[1:]:
            maybe_validate_command_path(arg)
        return args
    if program == "pwd":
        if len(args) != 1:
            raise ValueError("pwd does not accept arguments")
        return args
    if program in {"python", "python3"}:
        if len(args) < 3 or args[1:3] != ["-m", "py_compile"]:
            raise ValueError("Only python -m py_compile is allowed in run_command")
        for arg in args[3:]:
            maybe_validate_command_path(arg)
        return args
    raise ValueError(f"Command is not allowed: {program}")


def validate_test_command(command: str) -> List[str]:
    if not isinstance(command, str) or not command.strip():
        command = "pytest"
    if any(token in command for token in BLOCKED_COMMAND_TOKENS):
        raise ValueError("shell metacharacters are blocked")
    args = shlex.split(command)
    if not args:
        raise ValueError("test command is empty")
    program = args[0]
    if program == "pytest":
        for arg in args[1:]:
            maybe_validate_command_path(arg)
        return args
    if program in {"python", "python3"} and len(args) >= 3 and args[1:3] == ["-m", "pytest"]:
        for arg in args[3:]:
            maybe_validate_command_path(arg)
        return args
    raise ValueError("Only pytest commands are supported by run_tests")


def detect_lint_command() -> Optional[str]:
    if shutil.which("ruff"):
        return "ruff check ."
    if shutil.which("flake8"):
        return "flake8 ."
    package_json = WORKDIR / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if "lint" in scripts:
                return "npm run lint"
        except Exception:
            return None
    return None


def detect_format_command() -> Optional[str]:
    if shutil.which("ruff"):
        return "ruff format ."
    if shutil.which("black"):
        return "black ."
    package_json = WORKDIR / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if "format" in scripts:
                return "npm run format"
        except Exception:
            return None
    return None


def validate_lint_or_format_command(command: str, purpose: str) -> List[str]:
    if not isinstance(command, str) or not command.strip():
        detected = detect_lint_command() if purpose == "lint" else detect_format_command()
        if not detected:
            raise ValueError(f"No default {purpose} command detected for this repository")
        command = detected
    if any(token in command for token in BLOCKED_COMMAND_TOKENS):
        raise ValueError("shell metacharacters are blocked")
    args = shlex.split(command)
    if not args:
        raise ValueError(f"{purpose} command is empty")
    program = args[0]
    allowed_programs = {"ruff", "black", "flake8", "npm", "npx", "eslint", "prettier"}
    if program not in allowed_programs:
        raise ValueError(f"{purpose} command is not allowed: {program}")
    if program in {"npm", "npx"} and len(args) < 2:
        raise ValueError(f"{purpose} command is incomplete")
    return args


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


class HookAdapter:
    def before_tool_use(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        return None

    def after_tool_use(self, tool_name: str, arguments: Dict[str, Any], result: str) -> None:
        return None

    def on_completion(self, summary: str) -> None:
        return None

    def on_error(self, stage: str, error: str) -> None:
        return None


class ToolRuntime:
    def __init__(
        self,
        mode: str = "ask-for-approval",
        input_func: Callable[[str], str] = input,
        hooks: Optional[HookAdapter] = None,
    ):
        if mode not in APPROVAL_MODES:
            raise ValueError(f"Unsupported mode: {mode}")
        self.mode = mode
        self.input_func = input_func
        self.hooks = hooks or HookAdapter()
        self.approve_all = False
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    def should_confirm(self, tool_name: str, arguments: Dict[str, Any]) -> bool:
        if self.mode == "full-auto" or self.approve_all:
            return False
        if self.mode == "read-only":
            return False
        if tool_name in READ_ONLY_TOOLS:
            return False
        if tool_name == "run_tests":
            return True
        if tool_name == "lint":
            return True
        if tool_name == "run_command":
            return True
        return True

    def ensure_allowed(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        if self.mode == "read-only" and tool_name in MUTATING_TOOLS.union({"run_command", "format"}):
            raise ValueError(f"Tool {tool_name} is blocked in read-only mode")
        if self.mode == "read-only" and tool_name == "lint":
            return
        if self.mode == "read-only" and tool_name == "run_tests":
            return
        if self.should_confirm(tool_name, arguments):
            preview = json.dumps(arguments, ensure_ascii=False)
            answer = self.input_func(f"Approve {tool_name} {preview}? [y]es/[n]o/[a]ll: ").strip().lower()
            if answer in {"a", "all"}:
                self.approve_all = True
                return
            if answer not in {"y", "yes"}:
                raise ValueError(f"Tool {tool_name} denied by user")

    def _run_subprocess(self, args: List[str], timeout: int, cwd: Optional[Path] = None) -> str:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 600)),
            cwd=str(cwd or WORKDIR),
            shell=False,
        )
        stdout = truncate(result.stdout.strip(), 5000) if result.stdout else ""
        stderr = truncate(result.stderr.strip(), 2000) if result.stderr else ""
        parts = [f"EXIT CODE: {result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if not stdout and not stderr:
            parts.append("(no output)")
        return "\n\n".join(parts)

    def read_file(self, path: str) -> str:
        target = resolve(path)
        content = read_text(target)
        if content == "":
            return f"FILE {relative_display(target)} is empty"
        lines = content.splitlines()
        numbered = "\n".join(f"{index:4d} | {line}" for index, line in enumerate(lines, 1))
        return f"FILE {relative_display(target)} ({len(lines)} lines)\n\n{numbered}"

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8")
        line_count = content.count("\n") + (1 if content else 0)
        action = "updated" if existed else "created"
        return f"OK: {action} {relative_display(target)} ({line_count} lines)"

    def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        target = resolve(path)
        content = read_text(target)
        count = content.count(old_str)
        if count == 0:
            raise ValueError(f"old_str not found in {relative_display(target)}")
        if count > 1:
            raise ValueError(f"old_str appears {count} times; provide more specific context")
        target.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        return f"OK: edited {relative_display(target)}"

    def multi_edit(self, path: str, edits: List[Dict[str, str]]) -> str:
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits must be a non-empty list")
        target = resolve(path)
        content = read_text(target)
        updated = content
        for index, edit in enumerate(edits):
            old_str = edit.get("old_str")
            new_str = edit.get("new_str")
            if not isinstance(old_str, str) or not isinstance(new_str, str):
                raise ValueError(f"edits[{index}] must contain string old_str/new_str")
            count = updated.count(old_str)
            if count == 0:
                raise ValueError(f"edits[{index}].old_str not found in {relative_display(target)}")
            if count > 1:
                raise ValueError(f"edits[{index}].old_str appears {count} times")
            updated = updated.replace(old_str, new_str, 1)
        target.write_text(updated, encoding="utf-8")
        return f"OK: applied {len(edits)} edits to {relative_display(target)}"

    def list_dir(self, path: str = ".") -> str:
        target = resolve(path)
        if not target.exists():
            raise ValueError(f"Directory does not exist: {path}")
        if not target.is_dir():
            raise ValueError(f"Not a directory: {path}")
        items = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        lines = []
        for item in items[:MAX_LIST_ITEMS]:
            rel = relative_display(item)
            if item.is_dir():
                lines.append(f"DIR  {rel}/")
            else:
                lines.append(f"FILE {rel} ({item.stat().st_size} bytes)")
        return f"LIST {relative_display(target)} ({len(items)} items)\n" + "\n".join(lines)

    def grep_search(self, pattern: str, path: str = ".", file_pattern: str = "*") -> str:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError("pattern must be a non-empty string")
        target = resolve(path)
        matched_files: List[str] = []
        for candidate in iter_files(target):
            rel = relative_display(candidate)
            if not fnmatch.fnmatch(rel, file_pattern):
                continue
            if candidate.stat().st_size > MAX_SEARCH_FILE_SIZE or is_binary_file(candidate):
                continue
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
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

    def glob_search(self, pattern: str, path: str = ".") -> str:
        target = resolve(path)
        matches: List[str] = []
        for candidate in iter_files(target):
            rel = relative_display(candidate)
            if fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
                if len(matches) >= MAX_SEARCH_FILES:
                    break
        if not matches:
            return f"No files match {pattern!r}"
        return "GLOB MATCHES\n" + "\n".join(matches)

    def run_command(self, command: str, timeout: int = 30) -> str:
        args = validate_command(command)
        return self._run_subprocess(args, timeout)

    def git_status(self) -> str:
        return self._run_subprocess(["git", "status", "--short"], 30)

    def git_diff(self, path: Optional[str] = None) -> str:
        args = ["git", "diff"]
        if path:
            args.extend(["--", relative_display(resolve(path))])
        return self._run_subprocess(args, 30)

    def run_tests(self, command: str = "pytest", timeout: int = 120) -> str:
        args = validate_test_command(command)
        return self._run_subprocess(args, timeout)

    def lint(self, command: str = "", timeout: int = 120) -> str:
        args = validate_lint_or_format_command(command, "lint")
        return self._run_subprocess(args, timeout)

    def format(self, command: str = "", timeout: int = 120) -> str:
        args = validate_lint_or_format_command(command, "format")
        return self._run_subprocess(args, timeout)

    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        handlers = {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "multi_edit": self.multi_edit,
            "list_dir": self.list_dir,
            "grep_search": self.grep_search,
            "glob_search": self.glob_search,
            "run_command": self.run_command,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
            "run_tests": self.run_tests,
            "lint": self.lint,
            "format": self.format,
        }
        if tool_name not in handlers:
            raise ValueError(f"Unknown tool: {tool_name}")
        self.ensure_allowed(tool_name, arguments)
        self.hooks.before_tool_use(tool_name, arguments)
        result = handlers[tool_name](**arguments)
        self.hooks.after_tool_use(tool_name, arguments, result)
        return result


def save_transcript(run_id: str, payload: Dict[str, Any]) -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_transcript(run_id: str) -> Dict[str, Any]:
    path = TRANSCRIPTS_DIR / f"{run_id}.json"
    if not path.exists():
        raise ValueError(f"Run transcript not found: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_tool_result(result: str) -> str:
    return truncate(result.strip(), 400)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

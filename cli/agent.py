import argparse
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

from openai import OpenAI

from cli.runtime import TOOL_DEFINITIONS, ToolRuntime, load_transcript, now_iso, save_transcript, summarize_tool_result

BASE_URL = os.getenv("GEMINI_API_BASE", "http://localhost:7860/v1")
API_KEY = os.getenv("GEMINI_API_KEY", "your-api-key")
MODEL = os.getenv("GEMINI_MODEL", "gemini-auto")
MAX_STEPS = int(os.getenv("CLI_AGENT_MAX_STEPS", "20"))

RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
BLUE = "\033[94m"


class HookLogger:
    def before_tool_use(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        print(f"{YELLOW}tool:{RESET} {tool_name} {DIM}{json.dumps(arguments, ensure_ascii=False)}{RESET}")

    def after_tool_use(self, tool_name: str, arguments: Dict[str, Any], result: str) -> None:
        print(f"{DIM}{summarize_tool_result(result)}{RESET}")

    def on_completion(self, summary: str) -> None:
        print(f"{BLUE}done:{RESET} {summary}")

    def on_error(self, stage: str, error: str) -> None:
        print(f"{RED}{stage}: {error}{RESET}")


class ResponsesCliAgent:
    def __init__(self, mode: str, model: str = MODEL, base_url: str = BASE_URL, api_key: str = API_KEY):
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.runtime = ToolRuntime(mode=mode, hooks=HookLogger())
        self.mode = mode

    def _create_response(self, **kwargs):
        try:
            return self.client.responses.create(**kwargs)
        except Exception:
            return None

    def _fallback_chat_completion(self, prompt: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                stream=False,
            )
        except Exception as exc:
            raise RuntimeError(f"Unable to reach gateway at {BASE_URL}: {exc}") from exc
        message = response.choices[0].message
        content = message.content or ""
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "\n".join(parts)
        return content or ""

    def _tool_output_input(self, call_id: str, output: str) -> Dict[str, Any]:
        return {"type": "function_call_output", "call_id": call_id, "output": output}

    def _extract_output_text(self, response: Any) -> str:
        text = getattr(response, "output_text", None)
        if isinstance(text, str) and text:
            return text
        output = getattr(response, "output", None) or []
        for item in output:
            item_type = getattr(item, "type", None) or item.get("type")
            if item_type != "message":
                continue
            content = getattr(item, "content", None) or item.get("content") or []
            parts = []
            for block in content:
                block_type = getattr(block, "type", None) or block.get("type")
                block_text = getattr(block, "text", None) if hasattr(block, "text") else block.get("text")
                if block_type == "output_text" and isinstance(block_text, str):
                    parts.append(block_text)
            if parts:
                return "\n".join(parts)
        return ""

    def _extract_function_calls(self, response: Any) -> List[Dict[str, Any]]:
        calls = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", None) or item.get("type")
            if item_type != "function_call":
                continue
            calls.append({
                "call_id": getattr(item, "call_id", None) or item.get("call_id") or getattr(item, "id", None) or item.get("id"),
                "name": getattr(item, "name", None) or item.get("name"),
                "arguments": getattr(item, "arguments", None) or item.get("arguments") or "{}",
            })
        return calls

    def run(self, prompt: str, resume_from: Optional[str] = None) -> Dict[str, Any]:
        run_id = resume_from or f"run_{uuid.uuid4().hex[:12]}"
        transcript = load_transcript(run_id) if resume_from else {
            "run_id": run_id,
            "mode": self.mode,
            "model": self.model,
            "base_url": BASE_URL,
            "created_at": now_iso(),
            "prompt": prompt,
            "steps": [],
            "response_ids": [],
            "final_answer": None,
            "error": None,
        }
        previous_response_id = transcript["response_ids"][-1] if transcript["response_ids"] else None
        pending_input: Any = prompt if previous_response_id is None else []

        for step in range(1, MAX_STEPS + 1):
            if previous_response_id is None and isinstance(pending_input, str):
                response = self._create_response(
                    model=self.model,
                    input=pending_input,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                )
            else:
                response = self._create_response(
                    model=self.model,
                    previous_response_id=previous_response_id,
                    input=pending_input,
                    tools=TOOL_DEFINITIONS,
                    tool_choice="auto",
                )

            if response is None:
                try:
                    final_answer = self._fallback_chat_completion(prompt)
                except Exception as exc:
                    transcript["error"] = str(exc)
                    transcript["steps"].append({"step": step, "type": "fallback_chat_error", "error": str(exc)})
                    save_transcript(run_id, transcript)
                    self.runtime.hooks.on_error("gateway", str(exc))
                    return transcript
                transcript["final_answer"] = final_answer
                transcript["steps"].append({"step": step, "type": "fallback_chat", "final_answer": final_answer})
                save_transcript(run_id, transcript)
                return transcript

            transcript["response_ids"].append(response.id)
            function_calls = self._extract_function_calls(response)
            step_entry: Dict[str, Any] = {
                "step": step,
                "response_id": response.id,
                "status": getattr(response, "status", None),
                "function_calls": function_calls,
                "output_text": self._extract_output_text(response),
            }

            if not function_calls:
                final_answer = step_entry["output_text"]
                transcript["steps"].append(step_entry)
                transcript["final_answer"] = final_answer
                save_transcript(run_id, transcript)
                self.runtime.hooks.on_completion(final_answer or "Completed with empty output")
                return transcript

            tool_outputs = []
            for function_call in function_calls:
                raw_arguments = function_call["arguments"] or "{}"
                try:
                    arguments = json.loads(raw_arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                except Exception as exc:
                    result = f"ERROR: invalid tool arguments: {exc}"
                    step_entry.setdefault("tool_results", []).append({
                        "call_id": function_call["call_id"],
                        "name": function_call["name"],
                        "arguments": raw_arguments,
                        "result": result,
                    })
                    tool_outputs.append(self._tool_output_input(function_call["call_id"], result))
                    continue

                try:
                    result = self.runtime.execute(function_call["name"], arguments)
                except Exception as exc:
                    self.runtime.hooks.on_error("tool", str(exc))
                    result = f"ERROR: {exc}"
                step_entry.setdefault("tool_results", []).append({
                    "call_id": function_call["call_id"],
                    "name": function_call["name"],
                    "arguments": arguments,
                    "result": result,
                })
                tool_outputs.append(self._tool_output_input(function_call["call_id"], result))

            transcript["steps"].append(step_entry)
            save_transcript(run_id, transcript)
            previous_response_id = response.id
            pending_input = tool_outputs

        transcript["error"] = f"Reached max steps ({MAX_STEPS})"
        save_transcript(run_id, transcript)
        self.runtime.hooks.on_error("runtime", transcript["error"])
        return transcript


def print_banner(mode: str) -> None:
    print(
        f"\n{CYAN}{BOLD}Gemini Responses CLI Agent{RESET}\n"
        f"{DIM}API: {BASE_URL}\nModel: {MODEL}\nMode: {mode}{RESET}\n"
    )


def interactive_loop(mode: str) -> int:
    print_banner(mode)
    agent = ResponsesCliAgent(mode=mode)
    while True:
        try:
            task = input(f"\n{GREEN}{BOLD}You>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Bye.{RESET}")
            return 0
        if not task:
            continue
        if task.lower() in {"exit", "quit", "bye"}:
            print(f"{DIM}Bye.{RESET}")
            return 0
        if task.startswith("resume "):
            run_id = task.split(" ", 1)[1].strip()
            transcript = agent.run(prompt="", resume_from=run_id)
        else:
            transcript = agent.run(prompt=task)
        if transcript.get("error"):
            print(f"{RED}{transcript['error']}{RESET}")
        elif transcript.get("final_answer"):
            print(f"{BLUE}{BOLD}Agent>{RESET} {transcript['final_answer']}")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local coding-agent CLI using /v1/responses")
    parser.add_argument("prompt", nargs="?", help="Task prompt for one-shot execution")
    parser.add_argument("--mode", choices=["read-only", "ask-for-approval", "full-auto"], default="ask-for-approval")
    parser.add_argument("--resume", help="Resume from a saved run id")
    parser.add_argument("--show-transcript", help="Print a saved transcript and exit")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.show_transcript:
        transcript = load_transcript(args.show_transcript)
        print(json.dumps(transcript, ensure_ascii=False, indent=2))
        return 0

    if not args.prompt and not args.resume:
        return interactive_loop(args.mode)

    print_banner(args.mode)
    agent = ResponsesCliAgent(mode=args.mode)
    transcript = agent.run(prompt=args.prompt or "", resume_from=args.resume)
    if transcript.get("error"):
        print(f"{RED}{transcript['error']}{RESET}")
        return 1
    print(transcript.get("final_answer") or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

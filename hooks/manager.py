from typing import Any, Callable, Dict, List


class HookManager:
    def __init__(self):
        self._hooks: Dict[str, List[Callable[..., Any]]] = {
            "before_tool_use": [],
            "after_tool_use": [],
            "on_completion": [],
            "on_error": [],
        }

    def register(self, event_name: str, callback: Callable[..., Any]) -> None:
        if event_name not in self._hooks:
            raise ValueError(f"Unsupported hook event: {event_name}")
        self._hooks[event_name].append(callback)

    def fire(self, event_name: str, *args, **kwargs) -> None:
        for callback in self._hooks.get(event_name, []):
            callback(*args, **kwargs)

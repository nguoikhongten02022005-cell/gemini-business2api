from typing import Any, Dict, List


class McpInvocationAdapter:
    def __init__(self, server_definition: Dict[str, Any]):
        self.server_definition = server_definition

    def discover_tools(self) -> List[Dict[str, Any]]:
        return []

    def invoke(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ok": False,
            "experimental": True,
            "message": "MCP transport is not implemented yet",
            "tool_name": tool_name,
            "arguments": arguments,
        }

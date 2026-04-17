import json
from pathlib import Path
from typing import Any, Dict, List


def load_mcp_config(base_path: str = ".") -> Dict[str, Any]:
    path = Path(base_path) / ".mcp.json"
    if not path.exists():
        return {"servers": []}
    return json.loads(path.read_text(encoding="utf-8"))


def list_server_definitions(base_path: str = ".") -> List[Dict[str, Any]]:
    config = load_mcp_config(base_path)
    servers = config.get("servers")
    return servers if isinstance(servers, list) else []

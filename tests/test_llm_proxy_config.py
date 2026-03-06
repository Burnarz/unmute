import json
from pathlib import Path

from unmute.llm_proxy.config import load_mcp_config, load_mcp_excluded_tools


def test_load_mcp_config(tmp_path: Path):
    path = tmp_path / "mcp-servers.json"
    path.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "filesystem",
                        "enabled": True,
                        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem"],
                        "install": ["npm", "i", "-g", "@modelcontextprotocol/server-filesystem"],
                        "env": {"X": "1"},
                        "startup_timeout_sec": 3,
                        "tool_timeout_sec": 7,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    cfg = load_mcp_config(path)
    assert len(cfg.servers) == 1
    assert cfg.servers[0].name == "filesystem"
    assert cfg.servers[0].command[0] == "npx"
    assert cfg.servers[0].install is not None
    assert cfg.servers[0].env == {"X": "1"}
    assert cfg.servers[0].startup_timeout_sec == 3
    assert cfg.servers[0].tool_timeout_sec == 7


def test_load_mcp_excluded_tools(tmp_path: Path):
    path = tmp_path / "mcp-tools-excluded.json"
    path.write_text(
        json.dumps({"excluded_tools": ["calculate", "mcp__filesystem__read_file"]}),
        encoding="utf-8",
    )

    cfg = load_mcp_excluded_tools(path)
    assert cfg.excluded_tools == {"calculate", "mcp__filesystem__read_file"}

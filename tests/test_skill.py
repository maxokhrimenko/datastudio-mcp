"""The /datastudio-mcp skill must match the MCP it documents."""

import asyncio
import json
import re
import tomllib
from pathlib import Path

from datastudio_mcp import __version__
from datastudio_mcp.browser import BrowserConfig
from datastudio_mcp.server import create_server

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "skills" / "datastudio-mcp" / "SKILL.md").read_text()


def test_skill_mcp_and_package_share_one_version():
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    stated = re.findall(r"\*\*(\d+\.\d+\.\d+)\*\*", SKILL.split("\n## ", 1)[0])
    assert stated == [package, package], "Update the skill's version line with pyproject"
    assert __version__ == package, "Run uv sync: installed metadata is behind pyproject"


def test_skill_names_only_tools_that_exist(tmp_path):
    server = create_server(BrowserConfig(tmp_path, tmp_path / "journal"))
    listed = asyncio.run(server.list_tools())
    tools = {tool.name for tool in listed}
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)", SKILL))
    # Tool arguments, plus result keys and statuses the skill also names.
    allowed = set(re.findall(r'"([a-z_]+)": \{', json.dumps([t.parameters for t in listed])))
    allowed |= {"blend_id", "mcp_version", "outcome_unknown", "login_required",
                "verification_failed"}
    assert named - tools - allowed == set(), "The skill names a tool the MCP does not have"
    assert "report_open" in named and len(named & tools) > 80

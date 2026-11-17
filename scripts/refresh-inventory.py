"""Regenerate docs/inventory/native-catalog.json from the live editor.

usage: uv run python scripts/refresh-inventory.py <report URL or UUID>

The report must be editable and have at least one attached datasource (data components
are instantiated on it, never saved). Afterwards `git diff docs/inventory/` shows every
type, preset or setting Google added, changed or removed; update ui-inventory.md from it.
"""

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client

from datastudio_mcp.browser import BrowserConfig
from datastudio_mcp.server import create_server

OUT = Path(__file__).resolve().parent.parent / "docs" / "inventory" / "native-catalog.json"


async def main(report: str) -> None:
    async with Client(create_server(BrowserConfig.from_env())) as client:
        opened = await client.call_tool("report_open", {"report": report})
        if (opened.structured_content or {}).get("status") != "ready":
            sys.exit(f"report_open: {opened.structured_content}")
        result = await client.call_tool("native_catalog", {"full": True})
        catalog = result.structured_content
        OUT.write_text(json.dumps(catalog, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        print(f"{OUT}: {catalog['type_count']} types/presets")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))

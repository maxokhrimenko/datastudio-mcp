"""Report-scoped Data Studio automation."""

from importlib.metadata import version

# pyproject.toml is the only place the version is set; the skill states the same one.
__version__ = version("datastudio-mcp")

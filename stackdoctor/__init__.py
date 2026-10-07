"""stackdoctor: read-only MCP server that diagnoses a Python backend stack."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject.toml is the single source of truth; this reads the installed package's metadata.
    __version__ = version("stackdoctor")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"

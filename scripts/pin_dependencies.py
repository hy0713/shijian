"""Maintenance helper: pin direct specifiers to versions already resolved in uv.lock."""

import re
import tomllib
from pathlib import Path

path = Path("pyproject.toml")
versions = {
    p["name"]: p["version"] for p in tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))["package"]
}
text = re.sub(
    r'"([a-z][a-z0-9-]*)(\[[^\]]+\])?>=[^"]+"',
    lambda m: '"' + m[1] + (m[2] or "") + "==" + versions[m[1]] + '"',
    path.read_text(encoding="utf-8"),
)
path.write_text(text, encoding="utf-8")

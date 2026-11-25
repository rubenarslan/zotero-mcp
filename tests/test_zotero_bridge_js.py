"""Run the bridge add-on's JavaScript tests (tests/zotero_bridge/*.test.mjs) under pytest.

They need Node 18 or newer (node:test); without it they are skipped, not failed.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TESTS = sorted((HERE / "zotero_bridge").glob("*.test.mjs"))


def _node():
    node = shutil.which("node")
    if not node:
        return None
    version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout
    match = re.match(r"v(\d+)", version.strip())
    return node if match and int(match.group(1)) >= 18 else None


@pytest.mark.skipif(_node() is None, reason="needs Node 18 or newer")
def test_bridge_javascript():
    assert TESTS, "tests/zotero_bridge/*.test.mjs is missing"
    proc = subprocess.run([_node(), "--test", *map(str, TESTS)], capture_output=True, text=True, timeout=25)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]

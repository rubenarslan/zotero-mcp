"""Tools that run actions in the Zotero desktop app, through the zotero-mcp bridge add-on.

They need Zotero running on this computer with the bridge installed
(``zotero-cli bridge``), so they live in the opt-in ``zotero-app`` toolset.
"""

from __future__ import annotations

import re

from zotero_mcp import client as _client
from zotero_mcp import utils as _utils
from zotero_mcp import zotero_bridge as _bridge
from zotero_mcp._app import mcp
from zotero_mcp._context import Context
from zotero_mcp.tools import _helpers

# FastMCP clients give up after about 60 s, and an abandoned call reads as an
# opaque failure. The bridge answers with partial results once the wait is up.
_MAX_TOOL_WAIT = 50
_MAX_KEYS = 100
_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")

_FILE_KINDS = {"application/pdf": "PDF", "application/epub+zip": "EPUB", "text/html": "snapshot"}


def parse_item_keys(item_keys) -> list[str]:
    """Distinct, upper-cased item keys. Raises ValueError for anything that is not a key."""
    keys = []
    for entry in _helpers._normalize_str_list_input(item_keys, "item_keys"):
        for key in re.split(r"[,\s]+", entry.strip()):
            key = key.upper()
            if key and key not in keys:
                keys.append(key)
    if not keys:
        raise ValueError("Give at least one item key.")
    bad = [k for k in keys if not _KEY_RE.match(k)]
    if bad:
        raise ValueError(f"Not item keys (8 letters or digits): {', '.join(bad)}")
    if len(keys) > _MAX_KEYS:
        raise ValueError(f"At most {_MAX_KEYS} items at a time; got {len(keys)}.")
    return keys


def find_full_text_results(item_keys, wait_seconds: float) -> dict:
    """The bridge's answer for Find Full Text on *item_keys* in the active library.

    Raises ValueError for bad keys and BridgeError when the bridge cannot run it.
    """
    keys = parse_item_keys(item_keys)
    return _bridge.run_action(
        "findFullText", keys, group_id=_client.get_active_group_id(), wait_seconds=wait_seconds,
    )


def _line(row: dict) -> str:
    key = row.get("key", "?")
    title = row.get("title")
    head = f"- `{key}`" + (f" {title}" if title else "")
    status = row.get("status")
    if status == "found":
        att = row.get("attachment") or {}
        kind = _FILE_KINDS.get(att.get("contentType", ""), "file")
        return f"{head}: found a {kind}, attached as `{att.get('key', '?')}`"
    if status == "not_found_online":
        return f"{head}: Zotero found no file for it"
    if status == "pending":
        return f"{head}: still searching"
    if status == "queued":
        return f"{head}: queued"
    if status == "skipped":
        return f"{head}: skipped, {row.get('reason') or 'Zotero would not search for it'}"
    if status == "failed":
        return f"{head}: failed ({row.get('reason') or 'unknown error'})"
    if status == "not_found":
        return f"{head}: no item with this key in the current library"
    return f"{head}: {status}"


_SUMMARY_ORDER = (
    ("found", "found"),
    ("not_found_online", "no file found"),
    ("pending", "still searching"),
    ("queued", "queued"),
    ("failed", "failed"),
    ("skipped", "skipped"),
    ("not_found", "not in this library"),
)


def format_find_full_text(body: dict) -> str:
    """Markdown for the bridge's answer: a count line, then one line per item."""
    rows = body.get("results") or []
    counts = {}
    for row in rows:
        counts[row.get("status")] = counts.get(row.get("status"), 0) + 1
    summary = ", ".join(f"{counts[s]} {label}" for s, label in _SUMMARY_ORDER if counts.get(s))
    lines = [f"Find Full Text in Zotero: {summary or 'nothing to do'}.", ""]
    lines += [_line(row) for row in rows]
    waiting = counts.get("pending", 0) + counts.get("queued", 0)
    if waiting:
        lines += ["", f"Zotero is still working on {waiting} item(s) and shows its progress in a window. "
                      "Check later with zotero_get_item_children."]
    if counts.get("found") and not _utils.is_local_mode():
        lines += ["", "New files reach the web library once Zotero syncs."]
    return "\n".join(lines)


@mcp.tool(
    name="zotero_find_full_text",
    description=(
        "Run Zotero's own \"Find Full Text\" menu command on items: the Zotero app looks for a PDF or EPUB "
        "via DOI, URL, open-access sources and the user's proxy, and attaches it. Needs Zotero running here "
        "with the zotero-mcp bridge add-on. Skips items that already have one or lack a DOI/URL/PMCID. "
        "item_keys: parent items (max 100). timeout: seconds to wait for results (0-50, default 40; 0 only "
        "queues)."
    ),
)
def find_full_text(item_keys: list[str] | str, timeout: int = 40, *, ctx: Context) -> str:
    """Trigger Find Full Text in the running Zotero app.

    Not under @with_zotero_api_lock: it does not use pyzotero, and it can wait
    most of a minute, which would hold up every other tool.
    """
    try:
        wait = min(max(int(timeout), 0), _MAX_TOOL_WAIT)
    except (TypeError, ValueError):
        return "Error: timeout must be a number of seconds from 0 to 50."
    try:
        body = find_full_text_results(item_keys, wait)
    except ValueError as e:
        return f"Error: {e}"
    except _bridge.BridgeError as e:
        return f"Error: {e}"
    return format_find_full_text(body)

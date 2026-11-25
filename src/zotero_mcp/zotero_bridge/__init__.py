"""The zotero-mcp bridge: a small Zotero add-on, and the client that calls it.

Some things only the Zotero app can do. "Find Full Text" uses Zotero's
resolvers, the user's proxy and their cookies, and no API exposes it. The
add-on in this folder (``bootstrap.js`` and ``manifest.json``, plain
JavaScript with no build step) registers two endpoints on Zotero's local
HTTP server:

* ``GET /zotero-mcp/ping``: what is installed, and which actions it knows.
* ``POST /zotero-mcp/action``: runs one action on items named by key.

:func:`build_xpi` zips the add-on into an installable ``.xpi``. ``zotero-cli
bridge`` wraps it. :func:`run_action` pings first, so a failure says which of
these went wrong: Zotero is closed, the add-on is missing, or the add-on is
too old for the action.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import requests

BRIDGE_ID = "bridge@zotero-mcp"
XPI_NAME = "zotero-mcp-bridge.xpi"
DEFAULT_DIR = Path.home() / ".config" / "zotero-mcp"

_HERE = Path(__file__).resolve().parent
_ADDON_FILES = ("manifest.json", "bootstrap.js")
_HEADERS = {"User-Agent": "python/zotero-mcp", "Accept": "application/json"}
# Time for the bridge to answer once its wait is over: it only reads attachment lists.
_ANSWER_MARGIN = 8

INSTALL_HINT = (
    "run `zotero-cli bridge`, then in Zotero choose Tools > Plugins, click the gear, "
    "Install Plugin From File, and pick the file it names"
)


class BridgeError(Exception):
    """A bridge call that did not run, with ``kind`` saying why.

    Kinds: ``zotero_not_running``, ``bridge_missing``, ``bridge_outdated``,
    ``forbidden``, ``bad_request``, ``not_found``, ``timeout``, ``bad_response``.
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def addon_version() -> str:
    """The version of the add-on this package would install."""
    return json.loads((_HERE / "manifest.json").read_text(encoding="utf-8"))["version"]


def build_xpi(dest_dir: Path | str | None = None) -> Path:
    """Write ``zotero-mcp-bridge.xpi`` into *dest_dir* and return its path.

    The default directory is ``~/.config/zotero-mcp``. The zip has fixed
    timestamps, so the same add-on always gives the same bytes.
    """
    dest = Path(dest_dir) if dest_dir is not None else DEFAULT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / XPI_NAME
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _ADDON_FILES:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (_HERE / name).read_bytes())
    os.replace(tmp, path)
    return path


def base_url() -> str:
    """Zotero's local server: 127.0.0.1, never ``localhost``, which may resolve to ::1."""
    port = os.getenv("ZOTERO_LOCAL_PORT", "").strip() or "23119"
    return f"http://127.0.0.1:{port}"


def ping(timeout: float = 5) -> dict:
    """The installed bridge's ``{ok, id, version, protocol, actions, allowed}``.

    Raises :class:`BridgeError` when Zotero or the bridge is not there.
    """
    url = f"{base_url()}/zotero-mcp/ping"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    except requests.exceptions.Timeout:
        raise BridgeError(
            "timeout",
            f"Zotero did not answer within {timeout:g} seconds. It may be busy or waiting on a dialog.",
        ) from None
    except requests.exceptions.ConnectionError:
        raise BridgeError(
            "zotero_not_running",
            f"Zotero is not running (nothing is listening on {base_url()}). Start Zotero and try again.",
        ) from None
    if resp.status_code == 404:
        raise BridgeError(
            "bridge_missing",
            "Zotero is running, but the zotero-mcp bridge add-on is not installed. "
            f"To install it, {INSTALL_HINT}.",
        )
    try:
        info = resp.json()
    except ValueError:
        info = None
    if resp.status_code != 200 or not isinstance(info, dict) or info.get("id") != BRIDGE_ID:
        raise BridgeError(
            "bad_response",
            f"Something other than the zotero-mcp bridge answered at {url} (HTTP {resp.status_code}).",
        )
    return info


def run_action(action: str, item_keys: list[str], *, group_id: int = 0, wait_seconds: float = 0) -> dict:
    """Run *action* in Zotero on *item_keys* and return the bridge's JSON answer.

    *wait_seconds* is how long the bridge waits for Zotero to finish before it
    answers with what it knows. 0 means it answers once the work is queued.
    """
    info = ping()
    if action not in (info.get("actions") or []):
        raise BridgeError(
            "bridge_outdated",
            f"The installed zotero-mcp bridge (version {info.get('version') or 'unknown'}) cannot "
            f"run {action}; this zotero-mcp ships version {addon_version()}. To update it, "
            f"{INSTALL_HINT} (it replaces the old version).",
        )
    payload = {"action": action, "groupID": int(group_id), "itemKeys": list(item_keys),
               "waitSeconds": wait_seconds}
    try:
        resp = requests.post(
            f"{base_url()}/zotero-mcp/action",
            headers={**_HEADERS, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=wait_seconds + _ANSWER_MARGIN,
        )
    except requests.exceptions.Timeout:
        raise BridgeError(
            "timeout",
            "Zotero took too long to answer. The action may still be running there.",
        ) from None
    except requests.exceptions.ConnectionError:
        raise BridgeError(
            "zotero_not_running",
            "Lost the connection to Zotero. Check that it is still running.",
        ) from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code in (200, 202) and isinstance(body, dict) and body.get("ok"):
        return body
    message = (body or {}).get("error") if isinstance(body, dict) else None
    message = message or (resp.text or "").strip()[:300] or f"HTTP {resp.status_code}"
    kind = {400: "bad_request", 403: "forbidden", 404: "not_found"}.get(resp.status_code, "bad_response")
    raise BridgeError(kind, message)

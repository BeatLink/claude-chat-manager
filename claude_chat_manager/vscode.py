"""Reads what the VS Code extension records about conversations: which it has archived, and the
label it gave each one's tab."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

from . import config as config_mod

# The extension keeps its own state under this key, with archived sessions listed as hidden.
EXTENSION_KEY = "Anthropic.claude-code"
HIDDEN_FIELD = "hiddenSessionIds"
TABS_FIELD = "panelTabSessions"

CANDIDATES = (
    ".config/VSCodium/User/globalStorage/state.vscdb",
    ".config/Code/User/globalStorage/state.vscdb",
    ".config/Code - OSS/User/globalStorage/state.vscdb",
    ".vscode-server/data/User/globalStorage/state.vscdb",
)


def state_db(cfg: config_mod.Config | None = None) -> Path | None:
    """The editor state database to read, or None when there is not one."""
    cfg = cfg or config_mod.load()
    if cfg.vscode_state_db:
        path = Path(cfg.vscode_state_db).expanduser()
        return path if path.is_file() else None
    found = [Path.home() / name for name in CANDIDATES]
    existing = [p for p in found if p.is_file()]
    return max(existing, key=lambda p: p.stat().st_mtime) if existing else None


def _extension_state(path: Path) -> dict:
    """The extension's own JSON out of one editor database, or nothing when it is not there."""
    # The editor holds the database open, so read a copy rather than contend with it.
    with tempfile.TemporaryDirectory(prefix="ccm-vscode-") as workdir:
        copy = Path(workdir) / "state.vscdb"
        try:
            shutil.copy(path, copy)
            with sqlite3.connect(copy) as db:
                row = db.execute(
                    "SELECT value FROM ItemTable WHERE key = ?", (EXTENSION_KEY,)
                ).fetchone()
        except (OSError, sqlite3.Error):
            return {}
    if not row:
        return {}
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Finding the databases means reading a file per workspace the editor has ever opened, so the
# mapping is kept for a short while; a tab opening or closing changes the contents, not the paths.
_WORKSPACE_CACHE: tuple[float, dict[str, Path]] = (0.0, {})
WORKSPACE_CACHE_SECONDS = 30.0


def workspace_state_dbs(cfg: config_mod.Config | None = None) -> dict[str, Path]:
    """Each open project's own state database, keyed by the folder it belongs to."""
    global _WORKSPACE_CACHE
    stamped, cached = _WORKSPACE_CACHE
    if cached and time.time() - stamped < WORKSPACE_CACHE_SECONDS:
        return cached
    out: dict[str, Path] = {}
    globalstorage = state_db(cfg)
    if globalstorage is None:
        return out
    root = globalstorage.parent.parent / "workspaceStorage"
    if not root.is_dir():
        return out
    for directory in root.iterdir():
        database = directory / "state.vscdb"
        marker = directory / "workspace.json"
        if not database.is_file() or not marker.is_file():
            continue
        try:
            folder = json.loads(marker.read_text()).get("folder") or ""
        except (OSError, ValueError):
            continue
        if not folder.startswith("file://"):
            continue
        # One folder accumulates a directory per editor install, so the freshest one is the live one.
        name = unquote(folder[len("file://") :])
        current = out.get(name)
        if current is None or database.stat().st_mtime > current.stat().st_mtime:
            out[name] = database
    _WORKSPACE_CACHE = (time.time(), out)
    return out


def tab_labels(project_path: str, cfg: config_mod.Config | None = None) -> dict[str, str]:
    """The label the editor gave each open conversation tab in this project, by session id.

    The labels are cut short with an ellipsis once they are long, so they are read from the editor
    rather than guessed from the conversation's title.
    """
    database = workspace_state_dbs(cfg).get(str(Path(project_path)))
    if database is None:
        return {}
    tabs = _extension_state(database).get(TABS_FIELD)
    if not isinstance(tabs, list):
        return {}
    return {
        str(tab["sessionId"]): str(tab["title"])
        for tab in tabs
        if isinstance(tab, dict) and tab.get("sessionId") and tab.get("title")
    }


def tab_label(session_id: str, project_path: str, cfg: config_mod.Config | None = None) -> str:
    """The label of one conversation's tab, or an empty string when it has none open."""
    return tab_labels(project_path, cfg).get(session_id, "")


def archived_ids(cfg: config_mod.Config | None = None) -> set[str]:
    """Session ids the editor is hiding, which is what its archive does."""
    path = state_db(cfg)
    if path is None:
        return set()
    hidden = _extension_state(path).get(HIDDEN_FIELD)
    return {str(session) for session in hidden} if isinstance(hidden, list) else set()


def session_for_tab(label: str, project_path: str, cfg: config_mod.Config | None = None) -> str:
    """Which conversation the tab with this label belongs to, as the editor itself records it."""
    wanted = " ".join(label.split())
    for session_id, title in tab_labels(project_path, cfg).items():
        if " ".join(title.split()) == wanted:
            return session_id
    return ""

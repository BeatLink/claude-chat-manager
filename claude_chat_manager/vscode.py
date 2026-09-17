"""Reads which conversations the VS Code extension has archived out of its session list."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import config as config_mod

# The extension keeps its own state under this key, with archived sessions listed as hidden.
EXTENSION_KEY = "Anthropic.claude-code"
HIDDEN_FIELD = "hiddenSessionIds"

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


def archived_ids(cfg: config_mod.Config | None = None) -> set[str]:
    """Session ids the editor is hiding, which is what its archive does."""
    path = state_db(cfg)
    if path is None:
        return set()
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
            return set()
    if not row:
        return set()
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return set()
    hidden = data.get(HIDDEN_FIELD) if isinstance(data, dict) else None
    return {str(session) for session in hidden} if isinstance(hidden, list) else set()

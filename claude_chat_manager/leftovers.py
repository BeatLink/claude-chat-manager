"""What a session leaves outside its transcript: scratchpad files, its environment directory,
its file history, and the record that says which process it belonged to."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from .store import human_age, human_size, unslug

UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
SESSION_ID = re.compile(rf"^{UUID}$")

SCRATCHPAD = "scratchpad"

# The four kinds, in the order the sweep screens list them, with the label each one shows.
KINDS = {
    "scratchpad": "Scratchpads",
    "session-env": "Session environments",
    "file-history": "File history",
    "session-record": "Session records",
}

KIND_HELP = {
    "scratchpad": "the temporary files a session wrote outside the project",
    "session-env": "the per-session environment directory",
    "file-history": "the snapshots taken before a session edited a file",
    "session-record": "the registry entry for a session whose process has gone",
}


@dataclass
class Leftover:
    """One session's files of a single kind, measured and ready to be removed."""

    kind: str
    session_id: str
    paths: list[str] = field(default_factory=list)
    project_slug: str = ""
    size: int = 0
    files: int = 0
    mtime: float = 0.0
    pid: int = 0

    @property
    def path(self) -> str:
        """The directory or file this leftover is rooted at."""
        return self.paths[0] if self.paths else ""

    @property
    def project_path(self) -> str:
        """The working tree the session ran in, where the slug says."""
        return unslug(self.project_slug) if self.project_slug else ""

    @property
    def open_path(self) -> str:
        """What a file manager should be pointed at, which for a scratchpad is the scratchpad itself."""
        inner = Path(self.path) / SCRATCHPAD
        return str(inner) if inner.is_dir() else self.path

    @property
    def exists(self) -> bool:
        """Whether anything of this leftover is still on disk."""
        return any(Path(p).exists() for p in self.paths)

    @property
    def size_human(self) -> str:
        """Size in units a person reads at a glance."""
        return human_size(self.size)

    @property
    def age(self) -> str:
        """Short relative age, as the lists show it."""
        return human_age(self.mtime)


# Measuring ----------------------------------------------------------------------------------------


def measure(path: Path, seen: set[tuple[int, int]] | None = None) -> tuple[int, int, float]:
    """Total bytes, file count and newest write time under a path, counting hard links once."""
    if seen is None:
        seen = set()
    try:
        stat = path.lstat()
    except OSError:
        return 0, 0, 0.0
    if not path.is_dir():
        return stat.st_size, 1, stat.st_mtime
    size = 0
    files = 0
    mtime = stat.st_mtime
    for parent, _dirs, names in os.walk(path, onerror=lambda _error: None):
        for name in names:
            try:
                entry = os.lstat(os.path.join(parent, name))
            except OSError:
                continue
            files += 1
            mtime = max(mtime, entry.st_mtime)
            if entry.st_nlink > 1:
                key = (entry.st_dev, entry.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            size += entry.st_size
    return size, files, mtime


# Finding ------------------------------------------------------------------------------------------


def claude_dir(cfg: config_mod.Config | None = None) -> Path:
    """The directory Claude Code keeps all of its own state in."""
    cfg = cfg or config_mod.load()
    return Path(cfg.claude_dir).expanduser()


def scratchpad_roots(cfg: config_mod.Config | None = None) -> list[Path]:
    """The per-user temporary directories holding one folder per project."""
    tmp = claude_dir(cfg) / "tmp"
    if not tmp.is_dir():
        return []
    return sorted(d for d in tmp.iterdir() if d.is_dir() and d.name.startswith("claude-"))


def scratchpads(cfg: config_mod.Config | None = None, measured: bool = True) -> list[Leftover]:
    """Every session directory under the temporary roots, grouped by the project it belonged to."""
    out = []
    for root in scratchpad_roots(cfg):
        for project in sorted(root.iterdir()):
            if not project.is_dir():
                continue
            for session in sorted(project.iterdir()):
                if not session.is_dir() or not SESSION_ID.match(session.name):
                    continue
                out.append(_entry("scratchpad", session.name, session, measured, project.name))
    return out


def _simple(kind: str, folder: str, cfg, measured: bool) -> list[Leftover]:
    """Every session directory directly inside one of Claude's own folders."""
    root = claude_dir(cfg) / folder
    if not root.is_dir():
        return []
    return [
        _entry(kind, d.name, d, measured)
        for d in sorted(root.iterdir())
        if d.is_dir() and SESSION_ID.match(d.name)
    ]


def _entry(kind: str, session_id: str, path: Path, measured: bool, slug: str = "") -> Leftover:
    """Build one leftover, measuring it only when the caller wants the numbers."""
    leftover = Leftover(kind=kind, session_id=session_id, paths=[str(path)], project_slug=slug)
    if measured:
        leftover.size, leftover.files, leftover.mtime = measure(path)
    else:
        try:
            leftover.mtime = path.lstat().st_mtime
        except OSError:
            pass
    return leftover


def session_envs(cfg: config_mod.Config | None = None, measured: bool = True) -> list[Leftover]:
    """The per-session environment directories."""
    return _simple("session-env", "session-env", cfg, measured)


def file_histories(cfg: config_mod.Config | None = None, measured: bool = True) -> list[Leftover]:
    """The per-session file history directories."""
    return _simple("file-history", "file-history", cfg, measured)


def _running(pid: int, proc_start: str) -> bool:
    """Whether the process a session record names is still the same process."""
    if not Path("/proc").is_dir():
        return True
    stat = Path(f"/proc/{pid}/stat")
    try:
        fields = stat.read_text().rsplit(") ", 1)[-1].split()
    except OSError:
        return False
    if not proc_start:
        return True
    # Fields after the command name start at the process state, so start time is the twentieth of them.
    return len(fields) > 19 and fields[19] == str(proc_start)


def session_records(cfg: config_mod.Config | None = None, measured: bool = True) -> list[Leftover]:
    """The registry entries whose process has gone, each with the key file that belongs to it."""
    root = claude_dir(cfg) / "sessions"
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            record = {}
        pid = int(record.get("pid") or 0)
        if pid and _running(pid, str(record.get("procStart") or "")):
            continue
        paths = [path] + sorted(root.glob(f"{path.stem}.*.key"))
        leftover = Leftover(
            kind="session-record",
            session_id=str(record.get("sessionId") or ""),
            paths=[str(p) for p in paths],
            pid=pid,
        )
        for item in paths:
            size, files, mtime = measure(item)
            leftover.size += size
            leftover.files += files
            leftover.mtime = max(leftover.mtime, mtime)
        out.append(leftover)
    return out


FINDERS = {
    "scratchpad": scratchpads,
    "session-env": session_envs,
    "file-history": file_histories,
    "session-record": session_records,
}


def collect(
    kinds: list[str] | None = None,
    cfg: config_mod.Config | None = None,
    measured: bool = True,
) -> list[Leftover]:
    """Every leftover of the chosen kinds."""
    cfg = cfg or config_mod.load()
    out = []
    for kind in kinds or list(KINDS):
        finder = FINDERS.get(kind)
        if finder:
            out.extend(finder(cfg, measured))
    return out


def scratchpad_for(session_id: str, cfg: config_mod.Config | None = None) -> Leftover | None:
    """The scratchpad one conversation's session wrote, if it left one."""
    for root in scratchpad_roots(cfg):
        for project in root.iterdir():
            candidate = project / session_id
            if candidate.is_dir():
                return _entry("scratchpad", session_id, candidate, True, project.name)
    return None


# Orphans ------------------------------------------------------------------------------------------


def known_sessions(cfg: config_mod.Config | None = None) -> set[str]:
    """Every session id that still has a transcript, counting the ones waiting in the trash."""
    cfg = cfg or config_mod.load()
    ids = set()
    root = cfg.projects_path
    if root.is_dir():
        for project in root.iterdir():
            if project.is_dir():
                ids.update(f.stem for f in project.glob("*.jsonl"))
    trash = config_mod.data_dir() / "trash"
    if trash.is_dir():
        for path in trash.glob("*.jsonl"):
            found = re.search(UUID, path.stem)
            if found:
                ids.add(found.group(0))
    return ids


def orphans(
    kinds: list[str] | None = None,
    cfg: config_mod.Config | None = None,
    measured: bool = True,
) -> list[Leftover]:
    """The leftovers of sessions whose conversation is gone, newest first."""
    cfg = cfg or config_mod.load()
    known = known_sessions(cfg)
    # A session record is stale once its process is gone, whether or not the conversation survives.
    out = [
        item
        for item in collect(kinds, cfg, measured)
        if item.kind == "session-record" or item.session_id not in known
    ]
    out.sort(key=lambda item: item.mtime, reverse=True)
    return out


def summary(items: list[Leftover]) -> dict[str, dict]:
    """One row per kind, for the screens that offer the four of them as options."""
    rows = {}
    for kind, label in KINDS.items():
        chosen = [item for item in items if item.kind == kind]
        rows[kind] = {
            "kind": kind,
            "label": label,
            "help": KIND_HELP[kind],
            "count": len(chosen),
            "files": sum(item.files for item in chosen),
            "size": sum(item.size for item in chosen),
            "size_human": human_size(sum(item.size for item in chosen)),
        }
    return rows


# Acting -------------------------------------------------------------------------------------------


def remove(item: Leftover) -> bool:
    """Delete one leftover outright, and say whether anything went."""
    gone = False
    for raw in item.paths:
        path = Path(raw)
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
            else:
                continue
        except OSError:
            continue
        gone = True
    return gone


def remove_all(items: list[Leftover]) -> tuple[int, int]:
    """Delete a list of leftovers, returning how many went and how many bytes they held."""
    count = 0
    freed = 0
    for item in items:
        if remove(item):
            count += 1
            freed += item.size
    return count, freed


def open_in_file_manager(path: str, cfg: config_mod.Config | None = None) -> None:
    """Show a directory in the desktop's file manager."""
    cfg = cfg or config_mod.load()
    command = (cfg.file_manager or "xdg-open").split() + [str(path)]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def leftover_dict(item: Leftover) -> dict:
    """A leftover as plain data, for the web frontend."""
    return {
        "kind": item.kind,
        "session_id": item.session_id,
        "paths": item.paths,
        "path": item.path,
        "open_path": item.open_path,
        "project_slug": item.project_slug,
        "project_path": item.project_path,
        "size": item.size,
        "size_human": item.size_human,
        "files": item.files,
        "mtime": item.mtime,
        "age": item.age,
        "pid": item.pid,
    }

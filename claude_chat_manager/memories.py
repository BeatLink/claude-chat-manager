"""The memory files Claude keeps: the global workspace and the per-project ones."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .store import human_age, human_size, unslug

INDEX_NAME = "MEMORY.md"

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
INDEX_LINE = re.compile(r"^\s*-\s*\[(?P<title>[^\]]*)\]\((?P<file>[^)]+)\)\s*(?:—|-|–)?\s*(?P<hook>.*)$")
LINK = re.compile(r"\[\[([^\]]+)\]\]")
ABSOLUTE_PATH = re.compile(r"(?<![\w`])(/(?:[\w.@+-]+/)*[\w.@+-]+)")


@dataclass
class Memory:
    """One memory file, with what its frontmatter and its index line say about it."""

    name: str
    path: str
    scope: str
    scope_path: str
    project_path: str = ""
    description: str = ""
    kind: str = ""
    body: str = ""
    links: list[str] = field(default_factory=list)
    indexed: bool = False
    size: int = 0
    mtime: float = 0.0

    @property
    def modified(self) -> datetime:
        """Last write time as a local datetime."""
        return datetime.fromtimestamp(self.mtime)

    @property
    def display_title(self) -> str:
        """Best available one-line name for the memory."""
        return self.name or Path(self.path).stem

    @property
    def age(self) -> str:
        """Short relative age, as the lists show it."""
        return human_age(self.mtime)

    @property
    def size_human(self) -> str:
        """Size in units a person reads at a glance."""
        return human_size(self.size)


@dataclass
class Scope:
    """One memory directory: the global workspace, or one project's."""

    name: str
    slug: str
    path: str
    project_path: str = ""
    memories: list[Memory] = field(default_factory=list)
    orphan_index_lines: list[str] = field(default_factory=list)

    @property
    def index_path(self) -> str:
        """The MEMORY.md that lists this scope's memories."""
        return str(Path(self.path) / INDEX_NAME)

    @property
    def exists(self) -> bool:
        """Whether the project this scope belongs to is still on disk."""
        return not self.project_path or Path(self.project_path).is_dir()

    @property
    def last_active(self) -> float:
        """Modification time of the most recently written memory."""
        return max((m.mtime for m in self.memories), default=0.0)

    @property
    def size(self) -> int:
        """Total bytes of memory stored in this scope."""
        return sum(m.size for m in self.memories)

    @property
    def unindexed(self) -> list[Memory]:
        """Memories with no pointer in MEMORY.md, which are never loaded at session start."""
        return [m for m in self.memories if not m.indexed]

    def dead_links(self, elsewhere: set[str] | None = None) -> dict[str, list[str]]:
        """Links pointing at a memory that exists in no scope at all."""
        known = {m.name for m in self.memories} | (elsewhere or set())
        return {
            m.name: [link for link in m.links if link not in known and link != m.name]
            for m in self.memories
            if any(link not in known and link != m.name for link in m.links)
        }


def parse(path: Path, scope: Scope) -> Memory:
    """Read one memory file into its parts."""
    text = path.read_text(encoding="utf-8", errors="replace")
    stat = path.stat()
    memory = Memory(
        name=path.stem,
        path=str(path),
        scope=scope.name,
        scope_path=scope.path,
        project_path=scope.project_path,
        size=stat.st_size,
        mtime=stat.st_mtime,
    )
    found = FRONTMATTER.match(text)
    body = text
    if found:
        body = text[found.end() :]
        for line in found.group(1).splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "name" and value:
                memory.name = value
            elif key == "description":
                memory.description = value
            elif key == "type":
                memory.kind = value
    memory.body = body.strip()
    memory.links = list(dict.fromkeys(LINK.findall(text)))
    return memory


def index_entries(index: Path) -> list[dict]:
    """The pointer lines in a MEMORY.md, as the index writes them."""
    if not index.is_file():
        return []
    out = []
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        found = INDEX_LINE.match(line)
        if found:
            out.append(
                {
                    "title": found.group("title").strip(),
                    "file": found.group("file").strip(),
                    "hook": found.group("hook").strip(),
                    "line": line,
                }
            )
    return out


def global_dir(cfg: config_mod.Config | None = None) -> Path:
    """The workspace holding memories that are true everywhere."""
    cfg = cfg or config_mod.load()
    return Path(cfg.global_memory_dir).expanduser()


def load_scopes(cfg: config_mod.Config | None = None) -> list[Scope]:
    """Every memory directory: the global one first, then one per project that has any."""
    cfg = cfg or config_mod.load()
    scopes: list[Scope] = []

    candidates: list[tuple[str, str, Path, str]] = []
    everywhere = global_dir(cfg)
    if everywhere.is_dir():
        candidates.append(("Global", "global", everywhere, ""))
    root = cfg.projects_path
    if root.is_dir():
        for project_dir in sorted(root.iterdir()):
            memory_dir = project_dir / "memory"
            if memory_dir.is_dir():
                candidates.append(
                    (
                        Path(unslug(project_dir.name)).name or project_dir.name,
                        project_dir.name,
                        memory_dir,
                        unslug(project_dir.name),
                    )
                )

    for name, slug, path, project_path in candidates:
        scope = Scope(name=name, slug=slug, path=str(path), project_path=project_path)
        entries = index_entries(path / INDEX_NAME)
        listed = {entry["file"] for entry in entries}
        for file in sorted(path.glob("*.md")):
            if file.name == INDEX_NAME:
                continue
            memory = parse(file, scope)
            memory.indexed = file.name in listed
            scope.memories.append(memory)
        present = {Path(m.path).name for m in scope.memories}
        scope.orphan_index_lines = [e["line"] for e in entries if e["file"] not in present]
        if not scope.memories and not scope.orphan_index_lines:
            continue
        scope.memories.sort(key=lambda m: m.mtime, reverse=True)
        scopes.append(scope)
    return scopes


def find(scopes: list[Scope], name: str) -> Memory | None:
    """Look a memory up by name, or by a unique prefix of one."""
    everything = [m for s in scopes for m in s.memories]
    for memory in everything:
        if memory.name == name:
            return memory
    matches = [m for m in everything if m.name.startswith(name)]
    return matches[0] if len(matches) == 1 else None


def unindex(memory: Memory) -> bool:
    """Drop a memory's pointer line from its MEMORY.md, so nothing points at a missing file."""
    index = Path(memory.scope_path) / INDEX_NAME
    if not index.is_file():
        return False
    filename = Path(memory.path).name
    lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [
        line
        for line in lines
        if not (
            (found := INDEX_LINE.match(line)) is not None and found.group("file").strip() == filename
        )
    ]
    if len(kept) == len(lines):
        return False
    index.write_text("\n".join(kept) + ("\n" if kept else ""))
    return True


def delete(memory: Memory, cfg: config_mod.Config | None = None, purge: bool = False) -> str:
    """Remove a memory and its index line, trashing the file unless a purge is asked for."""
    cfg = cfg or config_mod.load()
    path = Path(memory.path)
    unindex(memory)
    if purge or not cfg.trash_on_delete:
        path.unlink(missing_ok=True)
        return "deleted"
    trash = config_mod.data_dir() / "memory-trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{memory.scope}-{path.name}"
    shutil.move(str(path), target)
    return str(target)


def mentioned_dirs(memory: Memory, limit: int = 5) -> list[str]:
    """Directories a memory names, which is where a check has to look for its claims."""
    out: list[str] = []
    if memory.project_path and Path(memory.project_path).is_dir():
        out.append(memory.project_path)
    for match in ABSOLUTE_PATH.findall(memory.body):
        path = Path(match)
        directory = path if path.is_dir() else path.parent
        if not directory.is_dir():
            continue
        text = str(directory)
        if text not in out and text not in ("/", "/nix/store"):
            out.append(text)
        if len(out) >= limit:
            break
    return out


def all_names(scopes: list[Scope]) -> set[str]:
    """Every memory name there is; links may cross between the global workspace and a project."""
    return {m.name for s in scopes for m in s.memories}


def stats(scopes: list[Scope]) -> dict:
    """Totals across every scope, for the footer of each frontend."""
    memories = [m for s in scopes for m in s.memories]
    return {
        "scopes": len(scopes),
        "memories": len(memories),
        "bytes": sum(m.size for m in memories),
        "unindexed": sum(len(s.unindexed) for s in scopes),
        "orphans": sum(len(s.orphan_index_lines) for s in scopes),
    }


def scope_dict(scope: Scope) -> dict:
    """A scope as plain data, with its memories."""
    return {
        "name": scope.name,
        "slug": scope.slug,
        "path": scope.path,
        "project_path": scope.project_path,
        "exists": scope.exists,
        "size_human": human_size(scope.size),
        "unindexed": len(scope.unindexed),
        "orphans": len(scope.orphan_index_lines),
        "memories": [memory_dict(m) for m in scope.memories],
    }


def memory_dict(memory: Memory) -> dict:
    """A memory as plain data, including the fields the views derive."""
    return {
        "name": memory.name,
        "path": memory.path,
        "scope": memory.scope,
        "scope_path": memory.scope_path,
        "project_path": memory.project_path,
        "description": memory.description,
        "kind": memory.kind,
        "body": memory.body,
        "links": memory.links,
        "indexed": memory.indexed,
        "size": memory.size,
        "size_human": memory.size_human,
        "mtime": memory.mtime,
        "age": memory.age,
        "modified": memory.modified.strftime("%Y-%m-%d %H:%M"),
    }

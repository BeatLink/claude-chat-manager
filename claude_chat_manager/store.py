"""Discovery and parsing of the Claude Code transcripts under ~/.claude/projects."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod

INDEX_VERSION = 3

# Records that carry conversation content rather than editor or session bookkeeping.
CONTENT_TYPES = {"user", "assistant"}


@dataclass
class Conversation:
    """One transcript file, summarized down to what a list view needs."""

    session_id: str
    path: str
    project_slug: str
    project_path: str
    title: str = ""
    last_prompt: str = ""
    git_branch: str = ""
    size: int = 0
    mtime: float = 0.0
    first_time: str = ""
    last_time: str = ""
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    sidechain_messages: int = 0
    corrupt_lines: int = 0
    models: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def messages(self) -> int:
        """Total content messages in the conversation."""
        return self.user_messages + self.assistant_messages

    @property
    def modified(self) -> datetime:
        """Last write time as a local datetime."""
        return datetime.fromtimestamp(self.mtime)

    @property
    def display_title(self) -> str:
        """Best available one-line name for the conversation."""
        for candidate in (self.title, self.last_prompt):
            text = " ".join(candidate.split())
            if text:
                return text[:120]
        return f"(untitled {self.session_id[:8]})"


@dataclass
class Project:
    """One project directory, holding the conversations recorded for a working tree."""

    slug: str
    path: str
    conversations: list[Conversation] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Short label for the project, normally the directory name."""
        return Path(self.path).name or self.path

    @property
    def exists(self) -> bool:
        """Whether the working tree the conversations refer to is still present."""
        return Path(self.path).is_dir()

    @property
    def last_active(self) -> float:
        """Modification time of the most recently touched conversation."""
        return max((c.mtime for c in self.conversations), default=0.0)

    @property
    def size(self) -> int:
        """Total bytes of transcript stored for this project."""
        return sum(c.size for c in self.conversations)


def unslug(slug: str) -> str:
    """Guess the working tree path from a project directory name."""
    return "/" + slug.lstrip("-").replace("-", "/")


def _text_of(content) -> str:
    """Flatten a message content field to plain text, ignoring tool traffic."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
    return "\n".join(parts).strip()


def scan(path: Path, project_slug: str) -> Conversation:
    """Read one transcript file and collect everything the list and detail views show."""
    stat = path.stat()
    convo = Conversation(
        session_id=path.stem,
        path=str(path),
        project_slug=project_slug,
        project_path=unslug(project_slug),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )
    models: list[str] = []
    first_user_text = ""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                convo.corrupt_lines += 1
                continue
            if not isinstance(record, dict):
                convo.corrupt_lines += 1
                continue
            kind = record.get("type")
            if kind == "ai-title":
                convo.title = record.get("aiTitle") or convo.title
                continue
            if kind == "summary":
                convo.title = convo.title or (record.get("summary") or "")
                continue
            if kind == "last-prompt":
                convo.last_prompt = record.get("lastPrompt") or convo.last_prompt
                continue
            if kind not in CONTENT_TYPES:
                continue

            stamp = record.get("timestamp") or ""
            if stamp:
                convo.first_time = convo.first_time or stamp
                convo.last_time = stamp
            if record.get("cwd"):
                convo.project_path = record["cwd"]
            if record.get("gitBranch"):
                convo.git_branch = record["gitBranch"]
            if record.get("isSidechain"):
                convo.sidechain_messages += 1

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if kind == "user":
                convo.user_messages += 1
                if not first_user_text:
                    first_user_text = _text_of(content)[:200]
            else:
                convo.assistant_messages += 1
                model = message.get("model")
                if model and model not in models:
                    models.append(model)
                usage = message.get("usage") or {}
                convo.input_tokens += int(usage.get("input_tokens") or 0)
                convo.output_tokens += int(usage.get("output_tokens") or 0)
                convo.cached_tokens += int(usage.get("cache_read_input_tokens") or 0)
                if isinstance(content, list):
                    convo.tool_calls += sum(
                        1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                    )
    convo.models = models
    convo.title = convo.title or first_user_text
    convo.last_prompt = convo.last_prompt or first_user_text
    return convo


def _index_path() -> Path:
    """Location of the scan cache."""
    return config_mod.cache_dir() / "index.json"


def _load_index() -> dict:
    """Read the scan cache, discarding it when written by an older version."""
    try:
        data = json.loads(_index_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("version") != INDEX_VERSION:
        return {}
    return data.get("entries") or {}


def _save_index(entries: dict) -> None:
    """Write the scan cache."""
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": INDEX_VERSION, "entries": entries}))
    tmp.replace(path)


def load_projects(cfg: config_mod.Config | None = None, refresh: bool = False) -> list[Project]:
    """Scan every project directory, reusing cached results for unchanged files."""
    cfg = cfg or config_mod.load()
    root = cfg.projects_path
    cached = {} if refresh else _load_index()
    fresh: dict = {}
    projects: list[Project] = []
    if not root.is_dir():
        return projects

    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        project = Project(slug=project_dir.name, path=unslug(project_dir.name))
        for transcript in project_dir.glob("*.jsonl"):
            try:
                stat = transcript.stat()
            except OSError:
                continue
            key = str(transcript)
            entry = cached.get(key)
            if entry and entry.get("mtime") == stat.st_mtime and entry.get("size") == stat.st_size:
                convo = Conversation(**entry["conversation"])
            else:
                convo = scan(transcript, project_dir.name)
            fresh[key] = {
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "conversation": asdict(convo),
            }
            project.conversations.append(convo)
        if not project.conversations:
            continue
        project.conversations.sort(key=lambda c: c.mtime, reverse=True)
        paths = {c.project_path for c in project.conversations if c.project_path}
        if paths:
            project.path = sorted(paths, key=len)[0]
        projects.append(project)

    _save_index(fresh)
    projects.sort(key=lambda p: p.last_active, reverse=True)
    return projects


def conversation_dict(convo: Conversation) -> dict:
    """A conversation as plain data, including the fields the views derive."""
    data = asdict(convo)
    data.update(
        messages=convo.messages,
        display_title=convo.display_title,
        age=human_age(convo.mtime),
        size_human=human_size(convo.size),
        modified=convo.modified.strftime("%Y-%m-%d %H:%M"),
    )
    return data


def project_dict(project: Project) -> dict:
    """A project as plain data, with its conversations."""
    return {
        "slug": project.slug,
        "name": project.name,
        "path": project.path,
        "exists": project.exists,
        "size": project.size,
        "size_human": human_size(project.size),
        "last_active": project.last_active,
        "age": human_age(project.last_active),
        "conversations": [conversation_dict(c) for c in project.conversations],
    }


def find(projects: list[Project], session_id: str) -> Conversation | None:
    """Look a conversation up by session id, or by a unique id prefix."""
    everything = [c for p in projects for c in p.conversations]
    for convo in everything:
        if convo.session_id == session_id:
            return convo
    matches = [c for c in everything if c.session_id.startswith(session_id)]
    return matches[0] if len(matches) == 1 else None


def search(projects: list[Project], needle: str) -> list[Conversation]:
    """Return conversations whose title, prompt, project or id contains the text."""
    needle = needle.strip().lower()
    if not needle:
        return [c for p in projects for c in p.conversations]
    hits = []
    for project in projects:
        for convo in project.conversations:
            haystack = " ".join(
                [convo.title, convo.last_prompt, convo.project_path, convo.session_id]
            ).lower()
            if needle in haystack:
                hits.append(convo)
    return hits


def delete(convo: Conversation, cfg: config_mod.Config | None = None, purge: bool = False) -> str:
    """Remove a conversation, moving it to the trash unless a purge is asked for."""
    cfg = cfg or config_mod.load()
    path = Path(convo.path)
    sidecar = path.with_suffix("")
    if purge or not cfg.trash_on_delete:
        path.unlink(missing_ok=True)
        if sidecar.is_dir():
            shutil.rmtree(sidecar, ignore_errors=True)
        destination = "deleted"
    else:
        trash = config_mod.data_dir() / "trash"
        trash.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = trash / f"{stamp}-{convo.project_slug}-{convo.session_id}.jsonl"
        shutil.move(str(path), target)
        if sidecar.is_dir():
            shutil.move(str(sidecar), target.with_suffix(""))
        destination = str(target)
    index = _load_index()
    index.pop(str(path), None)
    _save_index(index)
    return destination


def empty_project_dirs(cfg: config_mod.Config | None = None) -> list[Path]:
    """Project directories left behind with no transcripts in them."""
    cfg = cfg or config_mod.load()
    root = cfg.projects_path
    if not root.is_dir():
        return []
    return [d for d in sorted(root.iterdir()) if d.is_dir() and not any(d.glob("*.jsonl"))]


def prune_empty(cfg: config_mod.Config | None = None) -> list[Path]:
    """Remove the empty project directories and report which ones went."""
    removed = []
    for directory in empty_project_dirs(cfg):
        try:
            shutil.rmtree(directory)
        except OSError:
            continue
        removed.append(directory)
    return removed


def stats(projects: list[Project]) -> dict:
    """Totals across every project, for the footer of each frontend."""
    conversations = [c for p in projects for c in p.conversations]
    return {
        "projects": len(projects),
        "conversations": len(conversations),
        "messages": sum(c.messages for c in conversations),
        "bytes": sum(c.size for c in conversations),
        "corrupt": sum(1 for c in conversations if c.corrupt_lines),
        "newest": max((c.mtime for c in conversations), default=0.0),
    }


def human_size(count: int) -> str:
    """Format a byte count in units a person reads at a glance."""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def human_age(mtime: float) -> str:
    """Format a timestamp as a short relative age."""
    delta = time.time() - mtime
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 14:
        return f"{int(delta // 86400)}d ago"
    return datetime.fromtimestamp(mtime, timezone.utc).astimezone().strftime("%Y-%m-%d")

"""Settings for the chat manager, stored as JSON under the user's config directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

DEFAULT_SUMMARY_PROMPT = """\
You are reading a transcript of a Claude Code conversation. Summarize it for someone who needs to \
pick the work back up and has not read it.

Answer under exactly these three headings, using bullet points under each:

**What it was about** — the goal and the context, in two or three bullets.

**What was done** — the concrete outcome: files changed, commands run, decisions made, questions \
answered. Name real paths and identifiers rather than describing them in general terms.

**Outstanding items** — anything left unfinished, deferred, blocked, or explicitly promised but not \
delivered. Write "None" if the work is complete.

Be specific and brief. Do not describe the transcript format, and do not invent anything that is \
not in it."""

APP_NAME = "claude-chat-manager"


def _xdg(var: str, fallback: str) -> Path:
    """Return an XDG base directory, honouring the environment variable when set."""
    return Path(os.environ.get(var) or Path.home() / fallback)


def config_dir() -> Path:
    """Directory holding config.json."""
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME


def data_dir() -> Path:
    """Directory holding cached summaries and the trash."""
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME


def cache_dir() -> Path:
    """Directory holding the conversation index."""
    return _xdg("XDG_CACHE_HOME", ".cache") / APP_NAME


def config_path() -> Path:
    """Full path of the config file."""
    return config_dir() / "config.json"


@dataclass
class Config:
    """Everything the user can tune, with defaults that work unconfigured."""

    projects_dir: str = str(Path.home() / ".claude" / "projects")
    claude_bin: str = "claude"
    model: str = ""
    summary_prompt: str = DEFAULT_SUMMARY_PROMPT
    summary_timeout: int = 600
    include_thinking: bool = False
    include_tool_calls: bool = True
    include_tool_results: bool = False
    include_sidechains: bool = False
    max_transcript_chars: int = 120_000
    tool_detail_chars: int = 200
    trash_on_delete: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    extra_claude_args: list[str] = field(default_factory=list)

    @property
    def projects_path(self) -> Path:
        """The Claude projects directory as a Path."""
        return Path(self.projects_dir).expanduser()


def load() -> Config:
    """Read the config file, filling in defaults for anything missing."""
    path = config_path()
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Config()
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})


def save(config: Config) -> Path:
    """Write the config file, creating its directory if needed."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n")
    return path

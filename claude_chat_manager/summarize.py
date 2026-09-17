"""Summarizing a conversation by piping it through the claude CLI in headless mode."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config as config_mod
from . import render
from .store import Conversation


class SummaryError(RuntimeError):
    """Raised when the claude CLI is missing, fails, or returns nothing."""


@dataclass
class Summary:
    """A stored summary and the inputs it was produced from."""

    session_id: str
    text: str
    prompt: str
    model: str
    created: float
    source_mtime: float
    seconds: float = 0.0

    def stale(self, convo: Conversation) -> bool:
        """Whether the conversation has changed since this summary was made."""
        return convo.mtime > self.source_mtime + 1


def _summary_dir() -> Path:
    """Directory holding cached summaries."""
    path = config_mod.data_dir() / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(session_id: str) -> Summary | None:
    """Read a cached summary, or None when there is not one."""
    path = _summary_dir() / f"{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Summary(**data)
    except TypeError:
        return None


def store_summary(summary: Summary) -> Path:
    """Write a summary to the cache."""
    path = _summary_dir() / f"{summary.session_id}.json"
    path.write_text(json.dumps(asdict(summary), indent=2) + "\n")
    return path


def forget(session_id: str) -> None:
    """Drop a cached summary, used when its conversation is deleted."""
    (_summary_dir() / f"{session_id}.json").unlink(missing_ok=True)


def workdir() -> Path:
    """An empty directory to run the summarizer in, away from any project's CLAUDE.md and hooks."""
    path = config_mod.data_dir() / "workdir"
    path.mkdir(parents=True, exist_ok=True)
    return path


def command(cfg: config_mod.Config) -> list[str]:
    """The claude invocation used for summarizing."""
    argv = [cfg.claude_bin, "-p", "--no-session-persistence", "--strict-mcp-config"]
    if cfg.model:
        argv += ["--model", cfg.model]
    return argv + list(cfg.extra_claude_args)


def run(
    convo: Conversation,
    cfg: config_mod.Config | None = None,
    prompt: str | None = None,
    cache: bool = True,
) -> Summary:
    """Summarize one conversation and cache the result."""
    cfg = cfg or config_mod.load()
    prompt = prompt if prompt is not None else cfg.summary_prompt
    if not shutil.which(cfg.claude_bin):
        raise SummaryError(f"{cfg.claude_bin} is not on PATH")

    body = render.for_summary(convo.path, cfg)
    if not body.strip():
        raise SummaryError("this conversation has no text to summarize")

    argv = command(cfg) + [prompt]
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            input=body,
            capture_output=True,
            text=True,
            cwd=workdir(),
            timeout=cfg.summary_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SummaryError(f"claude timed out after {cfg.summary_timeout}s") from exc
    except OSError as exc:
        raise SummaryError(f"could not run {cfg.claude_bin}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise SummaryError(detail[-1] if detail else f"claude exited {result.returncode}")
    text = (result.stdout or "").strip()
    if not text:
        raise SummaryError("claude returned an empty summary")

    summary = Summary(
        session_id=convo.session_id,
        text=text,
        prompt=prompt,
        model=cfg.model or "default",
        created=time.time(),
        source_mtime=convo.mtime,
        seconds=round(time.monotonic() - started, 1),
    )
    if cache:
        store_summary(summary)
    return summary

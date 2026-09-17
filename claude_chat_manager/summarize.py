"""Summarizing a conversation by piping it through the claude CLI in headless mode."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config as config_mod
from . import render
from .store import Conversation


class SummaryError(RuntimeError):
    """Raised when the claude CLI is missing, fails, or returns nothing."""


VERDICT = re.compile(r"(safe-to-delete|keep|unclear)", re.IGNORECASE)

FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

VERDICT_LABELS = {
    "safe-to-delete": "safe to delete",
    "keep": "still open",
    "unclear": "unclear",
}

STATE_LABELS = {"done": "done", "open": "still open", "unknown": "cannot tell"}


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


@dataclass
class Review:
    """A check of one conversation's outstanding items against the project itself."""

    session_id: str
    text: str
    verdict: str
    items: list[dict] = field(default_factory=list)
    note: str = ""
    project_path: str = ""
    prompt: str = ""
    created: float = 0.0
    source_mtime: float = 0.0
    seconds: float = 0.0

    def stale(self, convo: Conversation) -> bool:
        """Whether the conversation has changed since this review was made."""
        return convo.mtime > self.source_mtime + 1

    @property
    def lines(self) -> list[str]:
        """The findings as one readable line each."""
        out = []
        for item in self.items:
            state = STATE_LABELS.get(str(item.get("state", "")).lower(), "cannot tell")
            evidence = str(item.get("evidence") or "").strip()
            line = f"**{item.get('item', 'item')}** — {state}"
            out.append(f"{line}. {evidence}" if evidence else line)
        return out

    @property
    def label(self) -> str:
        """The verdict in words, for a list or a badge."""
        return VERDICT_LABELS.get(self.verdict, "unclear")


def _summary_dir() -> Path:
    """Directory holding cached summaries."""
    path = config_mod.data_dir() / "summaries"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _review_dir() -> Path:
    """Directory holding cached reviews."""
    path = config_mod.data_dir() / "reviews"
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
    """Drop a cached summary and review, used when their conversation is deleted."""
    (_summary_dir() / f"{session_id}.json").unlink(missing_ok=True)
    (_review_dir() / f"{session_id}.json").unlink(missing_ok=True)


def workdir() -> Path:
    """An empty directory to run the summarizer in, away from any project's CLAUDE.md and hooks."""
    path = config_mod.data_dir() / "workdir"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_review(session_id: str) -> Review | None:
    """Read a cached review, or None when there is not one."""
    path = _review_dir() / f"{session_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Review(**data)
    except TypeError:
        return None


def store_review(review: Review) -> Path:
    """Write a review to the cache."""
    path = _review_dir() / f"{review.session_id}.json"
    path.write_text(json.dumps(asdict(review), indent=2) + "\n")
    return path


def command(cfg: config_mod.Config, prompt: str) -> list[str]:
    """The claude invocation used for summarizing."""
    # The prompt goes first: several of the CLI's options take a list, and a trailing prompt is swallowed into one.
    argv = [cfg.claude_bin, "-p", prompt, "--no-session-persistence", "--strict-mcp-config"]
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

    argv = command(cfg, prompt)
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


def review_command(cfg: config_mod.Config, prompt: str, project: Path) -> list[str]:
    """The claude invocation used for checking outstanding items."""
    argv = [cfg.claude_bin, "-p", prompt, "--no-session-persistence", "--strict-mcp-config"]
    if cfg.model:
        argv += ["--model", cfg.model]
    if cfg.review_system_prompt:
        argv += ["--append-system-prompt", cfg.review_system_prompt]
    argv += list(cfg.extra_claude_args)
    # Both of these take a list and would otherwise swallow whatever followed them, so they come last.
    argv += ["--add-dir", str(project)]
    if cfg.review_tools:
        argv += ["--allowedTools", *cfg.review_tools]
    return argv


def review_body(convo: Conversation, summary: Summary) -> str:
    """What the reviewer is given: where to look, and what was left outstanding."""
    return (
        f"PROJECT DIRECTORY: {convo.project_path}\n"
        f"CONVERSATION: {convo.display_title}\n"
        f"LAST ACTIVE: {convo.modified:%Y-%m-%d %H:%M}\n\n"
        "SUMMARY OF THAT CONVERSATION\n"
        "----------------------------\n"
        f"{summary.text}\n"
        "----------------------------\n"
    )


def parse_review(text: str) -> tuple[str, list[dict], str]:
    """Pull the verdict, the findings and the note out of the model's reply."""
    cleaned = FENCE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            verdict = str(data.get("verdict", "")).strip().lower()
            items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
            note = str(data.get("note") or "").strip()
            if verdict in VERDICT_LABELS:
                return verdict, items, note
            return "unclear", items, note
    # A reply that is not the JSON asked for still has a usable verdict most of the time.
    found = VERDICT.search(cleaned)
    return (found.group(1).lower() if found else "unclear"), [], ""


def review(
    convo: Conversation,
    cfg: config_mod.Config | None = None,
    prompt: str | None = None,
    summary: Summary | None = None,
    cache: bool = True,
) -> Review:
    """Check a conversation's outstanding items against the project it worked in."""
    cfg = cfg or config_mod.load()
    prompt = prompt if prompt is not None else cfg.review_prompt
    if not shutil.which(cfg.claude_bin):
        raise SummaryError(f"{cfg.claude_bin} is not on PATH")

    project = Path(convo.project_path)
    if not project.is_dir():
        raise SummaryError(f"the project directory {project} is gone")

    summary = summary or load(convo.session_id)
    if summary is None:
        summary = run(convo, cfg, cache=cache)

    body = review_body(convo, summary)
    # Runs from an empty directory with the project added, so the project's own CLAUDE.md and hooks stay out of the audit.
    argv = review_command(cfg, prompt, project)
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            input=body,
            capture_output=True,
            text=True,
            cwd=workdir(),
            timeout=cfg.review_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SummaryError(f"claude timed out after {cfg.review_timeout}s") from exc
    except OSError as exc:
        raise SummaryError(f"could not run {cfg.claude_bin}: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise SummaryError(detail[-1] if detail else f"claude exited {result.returncode}")
    text = (result.stdout or "").strip()
    if not text:
        raise SummaryError("claude returned an empty review")

    verdict, items, note = parse_review(text)
    checked = Review(
        session_id=convo.session_id,
        text=text,
        verdict=verdict,
        items=items,
        note=note,
        project_path=str(project),
        prompt=prompt,
        created=time.time(),
        source_mtime=convo.mtime,
        seconds=round(time.monotonic() - started, 1),
    )
    if cache:
        store_review(checked)
    return checked

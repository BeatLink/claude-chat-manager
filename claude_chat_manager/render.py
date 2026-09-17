"""Turning a transcript file into the plain text that gets summarized."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config as config_mod

SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# The caveat is boilerplate Claude Code wraps around a slash command, and it is dropped whole; an
# unclosed tag runs to the end of the message.
CAVEAT = re.compile(r"<local-command-caveat>.*?(?:</local-command-caveat>|\Z)", re.DOTALL)
COMMAND_TAGS = re.compile(r"</?(command-name|command-message|command-args|local-command-\w+)>")

TOOL_SUMMARY_KEYS = ("command", "file_path", "pattern", "path", "url", "prompt", "query")


def clean(text: str) -> str:
    """Strip the wrappers Claude Code injects around user text."""
    text = SYSTEM_REMINDER.sub("", text)
    text = CAVEAT.sub("", text)
    # A space keeps two adjacent tags from running their contents together.
    text = COMMAND_TAGS.sub(" ", text)
    return text.strip()


_clean = clean


def _tool_line(block: dict, limit: int) -> str:
    """One line naming a tool call and its most identifying argument."""
    name = block.get("name") or "tool"
    args = block.get("input")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    detail = ""
    if isinstance(args, dict):
        for key in TOOL_SUMMARY_KEYS:
            if args.get(key):
                detail = str(args[key])
                break
        if not detail and args:
            detail = json.dumps(args)[:limit]
    detail = " ".join(detail.split())
    if len(detail) > limit:
        detail = detail[:limit] + "…"
    return f"[tool] {name}: {detail}" if detail else f"[tool] {name}"


def _blocks(content, cfg: config_mod.Config) -> list[str]:
    """Render one message's content into text fragments worth summarizing."""
    if isinstance(content, str):
        cleaned = _clean(content)
        return [cleaned] if cleaned else []
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            cleaned = _clean(block.get("text") or "")
            if cleaned:
                out.append(cleaned)
        elif kind == "thinking" and cfg.include_thinking:
            cleaned = _clean(block.get("thinking") or "")
            if cleaned:
                out.append(f"[thinking] {cleaned}")
        elif kind == "tool_use" and cfg.include_tool_calls:
            out.append(_tool_line(block, cfg.tool_detail_chars))
        elif kind == "tool_result" and cfg.include_tool_results:
            result = block.get("content")
            if isinstance(result, list):
                result = " ".join(
                    b.get("text", "") for b in result if isinstance(b, dict)
                )
            text = " ".join(str(result or "").split())[: cfg.tool_detail_chars]
            if text:
                out.append(f"[result] {text}")
    return out


def transcript(path: str | Path, cfg: config_mod.Config | None = None) -> str:
    """Render a whole conversation as an alternating User/Claude script."""
    cfg = cfg or config_mod.load()
    turns: list[str] = []
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            if record.get("isSidechain") and not cfg.include_sidechains:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            parts = _blocks(message.get("content"), cfg)
            if not parts:
                continue
            speaker = "User" if record.get("type") == "user" else "Claude"
            turns.append(f"### {speaker}\n" + "\n\n".join(parts))
    return "\n\n".join(turns)


def clamp(text: str, limit: int) -> str:
    """Keep a transcript inside the size limit by dropping out of the middle."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.4)
    tail = limit - head
    dropped = len(text) - limit
    marker = f"\n\n[… {dropped:,} characters of the middle of this conversation omitted …]\n\n"
    return text[:head] + marker + text[-tail:]


def for_summary(path: str | Path, cfg: config_mod.Config | None = None) -> str:
    """Render and clamp a conversation, ready to hand to the model."""
    cfg = cfg or config_mod.load()
    return clamp(transcript(path, cfg), cfg.max_transcript_chars)

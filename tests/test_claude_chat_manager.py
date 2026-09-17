"""Tests for scanning, rendering, configuration and deletion, on synthetic transcripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_chat_manager import config as config_mod
from claude_chat_manager import render, store, summarize


def record(kind: str, **fields) -> str:
    """One transcript line."""
    return json.dumps({"type": kind, **fields})


def write_transcript(directory: Path, session_id: str, title: str = "Example session") -> Path:
    """Write a small but realistic transcript file."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    lines = [
        record("ai-title", aiTitle=title, sessionId=session_id),
        record(
            "user",
            sessionId=session_id,
            timestamp="2026-09-01T10:00:00.000Z",
            cwd="/tmp/example",
            gitBranch="main",
            message={"role": "user", "content": [{"type": "text", "text": "hello <system-reminder>noise</system-reminder>"}]},
        ),
        record(
            "assistant",
            sessionId=session_id,
            timestamp="2026-09-01T10:00:05.000Z",
            message={
                "role": "assistant",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2},
                "content": [
                    {"type": "thinking", "thinking": "pondering"},
                    {"type": "text", "text": "hi there"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                ],
            },
        ),
        record(
            "user",
            sessionId=session_id,
            isSidechain=True,
            timestamp="2026-09-01T10:00:06.000Z",
            message={"role": "user", "content": [{"type": "text", "text": "subagent chatter"}]},
        ),
        record("last-prompt", lastPrompt="hello", sessionId=session_id),
        "{ not json at all",
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated projects directory with XDG paths pointed at the temp tree."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    projects = tmp_path / "projects"
    write_transcript(projects / "-tmp-example", "11111111-2222-3333-4444-555555555555")
    return projects


@pytest.fixture
def cfg(workspace):
    """A config pointing at the temporary projects directory."""
    return config_mod.Config(projects_dir=str(workspace))


def test_scan_reads_metadata(cfg):
    """A scan picks up the title, counts, branch and token usage, and survives a bad line."""
    projects = store.load_projects(cfg, refresh=True)
    assert len(projects) == 1
    convo = projects[0].conversations[0]
    assert convo.title == "Example session"
    assert convo.user_messages == 2 and convo.assistant_messages == 1
    assert convo.tool_calls == 1
    assert convo.sidechain_messages == 1
    assert convo.git_branch == "main"
    assert convo.input_tokens == 10 and convo.output_tokens == 4
    assert convo.corrupt_lines == 1
    assert projects[0].path == "/tmp/example"


def test_index_cache_is_reused(cfg):
    """A second scan of an unchanged file comes from the cache."""
    first = store.load_projects(cfg, refresh=True)[0].conversations[0]
    calls = []
    original = store.scan

    def counting_scan(path, slug):
        calls.append(path)
        return original(path, slug)

    store.scan = counting_scan
    try:
        second = store.load_projects(cfg)[0].conversations[0]
    finally:
        store.scan = original
    assert not calls
    assert second.session_id == first.session_id


def test_render_drops_noise_and_keeps_tools(cfg):
    """The rendered transcript strips reminders and sidechains but keeps tool calls."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    text = render.transcript(convo.path, cfg)
    assert "system-reminder" not in text and "noise" not in text
    assert "subagent chatter" not in text
    assert "[tool] Bash: ls -la" in text
    assert "pondering" not in text


def test_render_includes_optional_parts(cfg):
    """Thinking and sidechains appear when the config asks for them."""
    cfg.include_thinking = True
    cfg.include_sidechains = True
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    text = render.transcript(convo.path, cfg)
    assert "[thinking] pondering" in text
    assert "subagent chatter" in text


def test_clamp_keeps_both_ends():
    """Clamping keeps the start and the end and says how much went."""
    text = "a" * 500 + "b" * 500
    clamped = render.clamp(text, 200)
    assert clamped.startswith("a") and clamped.endswith("b")
    assert "omitted" in clamped
    assert len(clamped) < len(text)


def test_delete_moves_to_trash(cfg):
    """A delete trashes the file by default and forgets its summary."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    summarize.store_summary(
        summarize.Summary(
            session_id=convo.session_id, text="old", prompt="p", model="m",
            created=0.0, source_mtime=convo.mtime,
        )
    )
    where = store.delete(convo, cfg)
    summarize.forget(convo.session_id)
    assert not Path(convo.path).exists()
    assert Path(where).exists()
    assert summarize.load(convo.session_id) is None
    assert store.load_projects(cfg, refresh=True) == []


def test_delete_purges(cfg):
    """A purge removes the file outright."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    assert store.delete(convo, cfg, purge=True) == "deleted"
    assert not Path(convo.path).exists()


def test_find_accepts_a_prefix(cfg):
    """Conversations can be found by a unique id prefix."""
    projects = store.load_projects(cfg, refresh=True)
    assert store.find(projects, "11111111") is not None
    assert store.find(projects, "nope") is None


def test_config_round_trip(workspace):
    """Saving and loading the config preserves an edited prompt."""
    cfg = config_mod.Config(summary_prompt="just the outstanding items")
    config_mod.save(cfg)
    assert config_mod.load().summary_prompt == "just the outstanding items"


def test_summary_goes_stale(cfg):
    """A summary made before the newest message is reported as stale."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    summary = summarize.Summary(
        session_id=convo.session_id, text="t", prompt="p", model="m",
        created=0.0, source_mtime=convo.mtime - 10,
    )
    assert summary.stale(convo)
    assert not summarize.Summary(
        session_id=convo.session_id, text="t", prompt="p", model="m",
        created=0.0, source_mtime=convo.mtime,
    ).stale(convo)


def test_empty_project_dirs_are_pruned(cfg, workspace):
    """Directories with no transcripts are found and removed."""
    (workspace / "-tmp-empty").mkdir()
    assert [d.name for d in store.empty_project_dirs(cfg)] == ["-tmp-empty"]
    assert len(store.prune_empty(cfg)) == 1
    assert not (workspace / "-tmp-empty").exists()


def test_summarize_requires_the_cli(cfg, monkeypatch):
    """A missing claude binary is reported rather than crashing."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    monkeypatch.setattr(summarize.shutil, "which", lambda _: None)
    with pytest.raises(summarize.SummaryError, match="not on PATH"):
        summarize.run(convo, cfg)


def test_review_parses_the_json_reply():
    """A well-formed reply gives the verdict, the findings and the note."""
    reply = """```json
    {"items": [{"item": "add the gitignore entry", "state": "open",
                "evidence": ".gitignore has no startup-metrics line"}],
     "verdict": "keep", "note": "one thing left"}
    ```"""
    verdict, items, note = summarize.parse_review(reply)
    assert verdict == "keep"
    assert items[0]["state"] == "open"
    assert note == "one thing left"


def test_review_falls_back_to_a_loose_verdict():
    """A reply that is not JSON still yields a verdict when one is written in it."""
    verdict, items, note = summarize.parse_review("I checked everything.\nVERDICT: safe-to-delete")
    assert verdict == "safe-to-delete"
    assert items == [] and note == ""


def test_review_of_prose_is_unclear():
    """A reply with no verdict at all is reported as unclear rather than guessed."""
    assert summarize.parse_review("here is a chatty answer")[0] == "unclear"


def test_review_lines_read_as_sentences():
    """Findings render with their state in words and their evidence."""
    review = summarize.Review(
        session_id="x",
        text="{}",
        verdict="keep",
        items=[{"item": "the gitignore entry", "state": "open", "evidence": "not present"}],
    )
    assert review.lines == ["**the gitignore entry** — still open. not present"]
    assert review.label == "still open"


def test_review_command_puts_the_prompt_before_the_list_options(cfg):
    """The prompt comes before --add-dir and --allowedTools, which would swallow it."""
    argv = summarize.review_command(cfg, "PROMPT", Path("/tmp/example"))
    assert argv[2] == "PROMPT"
    assert argv.index("PROMPT") < argv.index("--add-dir") < argv.index("--allowedTools")


def test_review_needs_the_project_to_still_exist(cfg, monkeypatch):
    """Checking a conversation whose project is gone is refused, not attempted."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    convo.project_path = "/tmp/definitely-not-here"
    monkeypatch.setattr(summarize.shutil, "which", lambda _: "/usr/bin/claude")
    with pytest.raises(summarize.SummaryError, match="is gone"):
        summarize.review(convo, cfg)


def test_trash_names_survive_the_session_id_dashes(cfg):
    """A trashed file is read back with its project and session id intact."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    store.delete(convo, cfg)
    entries = store.trashed(cfg)
    assert len(entries) == 1
    assert entries[0]["session_id"] == convo.session_id
    assert entries[0]["project_slug"] == convo.project_slug


def test_trash_restores_a_conversation(cfg):
    """Restoring puts the transcript back in its project directory."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    store.delete(convo, cfg)
    assert store.load_projects(cfg, refresh=True) == []
    target = store.restore(store.trashed(cfg)[0], cfg)
    assert target.exists()
    assert len(store.load_projects(cfg, refresh=True)[0].conversations) == 1


def test_emptying_the_trash_reports_what_went(cfg):
    """Emptying removes every trashed file and counts them."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    store.delete(convo, cfg)
    assert store.empty_trash(cfg) == 1
    assert store.trashed(cfg) == []


def test_live_conversations_are_flagged(cfg):
    """A transcript written to moments ago is marked as possibly still open."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    assert convo.live
    convo.mtime -= 600
    assert not convo.live

"""Tests for scanning, rendering, configuration and deletion, on synthetic transcripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_chat_manager import config as config_mod
from claude_chat_manager import leftovers
from claude_chat_manager import memories
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


def _write_state_db(path: Path, hidden: list[str]) -> Path:
    """Write a state database shaped like the editor's own."""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    db.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("Anthropic.claude-code", json.dumps({"hiddenSessionIds": hidden})),
    )
    db.commit()
    db.close()
    return path


def test_archived_ids_come_from_the_editor_state(cfg, tmp_path):
    """Archived conversations are read out of the editor's own state database."""
    from claude_chat_manager import vscode

    session = "11111111-2222-3333-4444-555555555555"
    cfg.vscode_state_db = str(_write_state_db(tmp_path / "state.vscdb", [session]))
    assert vscode.archived_ids(cfg) == {session}
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    assert convo.archived


def test_missing_state_db_is_not_an_error(cfg, tmp_path):
    """A missing or unreadable editor database simply means nothing is archived."""
    from claude_chat_manager import vscode

    cfg.vscode_state_db = str(tmp_path / "nowhere.vscdb")
    assert vscode.archived_ids(cfg) == set()


def test_marks_show_state_last(cfg, tmp_path):
    """The glyphs read summary, check, then what the editor is doing with the conversation."""
    convo = store.load_projects(cfg, refresh=True)[0].conversations[0]
    assert store.marks(convo)[2] == "●"
    convo.mtime -= 600
    assert store.marks(convo)[2] == " "
    convo.archived = True
    assert store.marks(convo) == "  ▣"
    assert convo.state == "archived"


def write_memory(directory: Path, name: str, body: str = "A fact about [[something-else]].",
                 kind: str = "project", indexed: bool = True) -> Path:
    """Write one memory file, and its index line when it is meant to be listed."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: what {name} is about\nmetadata:\n  type: {kind}\n---\n\n{body}\n"
    )
    index = directory / "MEMORY.md"
    lines = index.read_text().splitlines() if index.exists() else []
    if indexed:
        lines.append(f"- [{name}]({name}.md) — a hook")
    index.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def memory_workspace(workspace, tmp_path, monkeypatch):
    """A global memory workspace and one project scope, both with a MEMORY.md."""
    from claude_chat_manager import config as config_module

    everywhere = tmp_path / "global-memory"
    write_memory(everywhere, "something-else", kind="feedback")
    write_memory(workspace / "-tmp-example" / "memory", "a-project-fact")
    cfg = config_module.Config(
        projects_dir=str(workspace), global_memory_dir=str(everywhere)
    )
    return cfg


def test_memories_are_grouped_by_scope(memory_workspace):
    """The global workspace and each project's memory directory are separate scopes."""
    scopes = memories.load_scopes(memory_workspace)
    assert [s.slug for s in scopes] == ["global", "-tmp-example"]
    assert scopes[0].memories[0].kind == "feedback"
    assert scopes[1].memories[0].description == "what a-project-fact is about"
    assert scopes[1].memories[0].project_path == "/tmp/example"


def test_links_resolve_across_scopes(memory_workspace):
    """A project memory may link to a global one without being called broken."""
    scopes = memories.load_scopes(memory_workspace)
    project = next(s for s in scopes if s.slug == "-tmp-example")
    assert project.dead_links() != {}
    assert project.dead_links(memories.all_names(scopes)) == {}


def test_unindexed_memories_are_reported(memory_workspace):
    """A memory with no pointer in MEMORY.md is flagged, because sessions never load it."""
    scope_dir = Path(memory_workspace.global_memory_dir)
    write_memory(scope_dir, "never-listed", indexed=False)
    scope = memories.load_scopes(memory_workspace)[0]
    assert [m.name for m in scope.unindexed] == ["never-listed"]


def test_orphan_index_lines_are_reported(memory_workspace):
    """A pointer whose file is gone is reported rather than silently ignored."""
    index = Path(memory_workspace.global_memory_dir) / "MEMORY.md"
    index.write_text(index.read_text() + "- [ghost](ghost.md) — not there\n")
    scope = memories.load_scopes(memory_workspace)[0]
    assert scope.orphan_index_lines and "ghost.md" in scope.orphan_index_lines[0]


def test_deleting_a_memory_takes_its_index_line(memory_workspace):
    """Deleting removes the file and the MEMORY.md line that pointed at it."""
    scopes = memories.load_scopes(memory_workspace)
    memory = memories.find(scopes, "something-else")
    index = Path(memory.scope_path) / "MEMORY.md"
    assert "something-else.md" in index.read_text()
    where = memories.delete(memory, memory_workspace)
    assert not Path(memory.path).exists()
    assert Path(where).exists()
    assert "something-else.md" not in index.read_text()


def test_mentioned_dirs_come_from_the_body(memory_workspace, tmp_path):
    """A check looks in the project and in any directory the memory names."""
    named = tmp_path / "somewhere"
    named.mkdir()
    write_memory(
        Path(memory_workspace.global_memory_dir),
        "names-a-path",
        body=f"The file at {named}/thing.conf holds it.",
    )
    memory = memories.find(memories.load_scopes(memory_workspace), "names-a-path")
    assert str(named) in memories.mentioned_dirs(memory)


def test_memory_verdicts_read_as_words():
    """A memory check reports its verdict in the same words the views show."""
    check = summarize.Review(session_id="x", text="{}", verdict="stale")
    assert check.label == "out of date"
    assert summarize.Review(session_id="x", text="{}", verdict="current").label == "still true"


@pytest.fixture
def leftover_workspace(tmp_path, workspace, monkeypatch):
    """A Claude directory holding leftovers for one live session and one that is gone."""
    live = "11111111-2222-3333-4444-555555555555"
    gone = "99999999-8888-7777-6666-555555555555"
    home = tmp_path / "claude"
    for session in (live, gone):
        pad = home / "tmp" / "claude-1000" / "-tmp-example" / session / "scratchpad"
        pad.mkdir(parents=True)
        (pad / "notes.md").write_text("x" * 100)
        (home / "session-env" / session).mkdir(parents=True)
        history = home / "file-history" / session
        history.mkdir(parents=True)
        (history / "before.txt").write_text("y" * 50)
    (home / "tmp" / "claude-1000" / "bundled-skills").mkdir(parents=True)
    records = home / "sessions"
    records.mkdir(parents=True)
    (records / "999999.json").write_text(json.dumps({"pid": 999999, "sessionId": gone}))
    (records / "999999.abc.key").write_text("k")
    return config_mod.Config(projects_dir=str(workspace), claude_dir=str(home))


def test_leftovers_are_found_for_every_kind(leftover_workspace):
    found = leftovers.collect(cfg=leftover_workspace)
    assert {item.kind for item in found} == {
        "scratchpad",
        "session-env",
        "file-history",
        "session-record",
    }
    assert sum(1 for item in found if item.kind == "scratchpad") == 2


def test_only_sessions_with_no_conversation_are_orphans(leftover_workspace):
    found = leftovers.orphans(["scratchpad", "file-history"], leftover_workspace)
    assert [item.session_id for item in found] == [
        "99999999-8888-7777-6666-555555555555"
    ] * 2


def test_a_trashed_conversation_keeps_its_leftovers(cfg, leftover_workspace):
    convo = store.find(store.load_projects(cfg), "11111111")
    store.delete(convo, cfg)
    assert "11111111-2222-3333-4444-555555555555" in leftovers.known_sessions(leftover_workspace)


def test_the_scratchpad_of_one_conversation_is_measured(leftover_workspace):
    pad = leftovers.scratchpad_for("11111111-2222-3333-4444-555555555555", leftover_workspace)
    assert pad and pad.files == 1 and pad.size == 100
    assert pad.open_path.endswith("/scratchpad")


def test_hard_links_are_counted_once(tmp_path):
    directory = tmp_path / "pad"
    directory.mkdir()
    (directory / "one").write_text("z" * 80)
    (directory / "two").hardlink_to(directory / "one")
    size, files, _mtime = leftovers.measure(directory)
    assert files == 2 and size == 80


def test_a_dead_session_record_takes_its_key_file(leftover_workspace):
    found = [i for i in leftovers.collect(["session-record"], leftover_workspace) if i.pid == 999999]
    assert len(found) == 1 and len(found[0].paths) == 2
    assert leftovers.remove(found[0])
    assert not found[0].exists


def test_removing_leftovers_reports_what_was_freed(leftover_workspace):
    found = leftovers.orphans(["scratchpad"], leftover_workspace)
    count, freed = leftovers.remove_all(found)
    assert count == 1 and freed == 100
    assert not leftovers.orphans(["scratchpad"], leftover_workspace)


def test_the_sweep_summary_has_a_row_per_kind(leftover_workspace):
    rows = leftovers.summary(leftovers.orphans(cfg=leftover_workspace))
    assert list(rows) == list(leftovers.KINDS)
    assert rows["scratchpad"]["count"] == 1
    assert rows["session-env"]["count"] == 1


def write_editor_state(root: Path, folder: str, payload: dict) -> Path:
    """Write a workspace storage directory the way the editor lays one out."""
    import sqlite3 as sql

    directory = root / "workspaceStorage" / f"hash-{abs(hash(folder)) % 9999}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "workspace.json").write_text(json.dumps({"folder": f"file://{folder}"}))
    db = directory / "state.vscdb"
    con = sql.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute(
        "INSERT OR REPLACE INTO ItemTable VALUES (?, ?)",
        ("Anthropic.claude-code", json.dumps(payload)),
    )
    con.commit()
    con.close()
    return db


@pytest.fixture
def editor(tmp_path, monkeypatch):
    """A fake editor state tree with one project holding two open conversation tabs."""
    from claude_chat_manager import vscode as vscode_mod

    monkeypatch.setattr(vscode_mod, "_WORKSPACE_CACHE", (0.0, {}))
    root = tmp_path / "editor" / "User"
    globalstorage = root / "globalStorage"
    globalstorage.mkdir(parents=True)
    write_editor_state(
        root,
        "/tmp/example",
        {
            "panelTabSessions": [
                {"sessionId": "11111111-2222-3333-4444-555555555555", "title": "A short title"},
                {"sessionId": "99999999-8888-7777-6666-555555555555", "title": "A long one that w…"},
            ]
        },
    )
    state = globalstorage / "state.vscdb"
    import sqlite3 as sql

    con = sql.connect(state)
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("Anthropic.claude-code", json.dumps({"hiddenSessionIds": ["deadbeef"]})),
    )
    con.commit()
    con.close()
    return config_mod.Config(vscode_state_db=str(state))


def test_tab_labels_come_from_the_workspace_state(editor):
    from claude_chat_manager import vscode as vscode_mod

    labels = vscode_mod.tab_labels("/tmp/example", editor)
    assert labels["11111111-2222-3333-4444-555555555555"] == "A short title"
    assert labels["99999999-8888-7777-6666-555555555555"].endswith("…")


def test_a_truncated_label_is_used_as_the_editor_wrote_it(editor):
    from claude_chat_manager import vscode as vscode_mod

    # The editor cuts a long title short, so the label must be read rather than derived from it.
    label = vscode_mod.tab_label("99999999-8888-7777-6666-555555555555", "/tmp/example", editor)
    assert label == "A long one that w…"


def test_a_conversation_with_no_tab_has_no_label(editor):
    from claude_chat_manager import vscode as vscode_mod

    assert vscode_mod.tab_label("00000000-0000-0000-0000-000000000000", "/tmp/example", editor) == ""
    assert vscode_mod.tab_labels("/tmp/nowhere", editor) == {}


def test_archived_ids_still_read_from_global_state(editor):
    from claude_chat_manager import vscode as vscode_mod

    assert vscode_mod.archived_ids(editor) == {"deadbeef"}


def test_bridges_skip_dead_editors_and_empty_tokens(tmp_path):
    from claude_chat_manager import ide as ide_mod

    locks = tmp_path / "ide"
    locks.mkdir()
    (locks / "4001.lock").write_text(
        json.dumps({"pid": 1, "authToken": "t", "ideName": "X", "workspaceFolders": ["/tmp/example"]})
    )
    (locks / "4002.lock").write_text(
        json.dumps({"pid": 999999, "authToken": "t", "workspaceFolders": ["/tmp/example"]})
    )
    (locks / "4003.lock").write_text(
        json.dumps({"pid": 1, "authToken": "", "workspaceFolders": ["/tmp/example"]})
    )
    (locks / "notalock.txt").write_text("{}")
    cfg = config_mod.Config(claude_dir=str(tmp_path))
    found = ide_mod.bridges(cfg)
    assert [b.port for b in found] == [4001]
    assert found[0].covers("/tmp/example/deeper")
    assert not found[0].covers("/tmp/elsewhere")


def test_closing_a_tab_needs_a_running_editor(editor, tmp_path):
    from claude_chat_manager import ide as ide_mod

    cfg = config_mod.Config(vscode_state_db=editor.vscode_state_db, claude_dir=str(tmp_path))
    with pytest.raises(ide_mod.BridgeError):
        ide_mod.close_conversation_tab(
            "11111111-2222-3333-4444-555555555555", "/tmp/example", cfg
        )
    assert "could not close its tab" in ide_mod.close_tab_quietly(
        "11111111-2222-3333-4444-555555555555", "/tmp/example", cfg
    )


def test_closing_a_tab_that_is_not_open_is_not_an_error(editor, tmp_path):
    from claude_chat_manager import ide as ide_mod

    cfg = config_mod.Config(vscode_state_db=editor.vscode_state_db, claude_dir=str(tmp_path))
    assert "no tab" in ide_mod.close_conversation_tab("nope", "/tmp/example", cfg)


def test_deleting_everything_takes_the_leftovers_too(cfg, leftover_workspace):
    live = "11111111-2222-3333-4444-555555555555"
    assert leftovers.for_session(live, leftover_workspace)
    convo = store.find(store.load_projects(cfg), live)
    store.delete(convo, cfg, purge=True)
    count, _freed = leftovers.remove_all(leftovers.for_session(live, leftover_workspace))
    assert count == 3
    assert not leftovers.for_session(live, leftover_workspace)


def test_for_session_ignores_other_sessions(leftover_workspace):
    gone = "99999999-8888-7777-6666-555555555555"
    kinds = {item.kind for item in leftovers.for_session(gone, leftover_workspace)}
    # Its dead session record belongs to it as much as its scratchpad does.
    assert kinds == {"scratchpad", "session-env", "file-history", "session-record"}
    assert not leftovers.for_session("00000000-0000-0000-0000-000000000000", leftover_workspace)


def test_a_tab_resolves_back_to_its_conversation(editor):
    from claude_chat_manager import vscode as vscode_mod

    assert (
        vscode_mod.session_for_tab("A long one that w…", "/tmp/example", editor)
        == "99999999-8888-7777-6666-555555555555"
    )
    # Whitespace in a label is normalised, the way the editor's own matching does it.
    assert (
        vscode_mod.session_for_tab("A   short    title", "/tmp/example", editor)
        == "11111111-2222-3333-4444-555555555555"
    )
    assert vscode_mod.session_for_tab("Claude Code", "/tmp/example", editor) == ""


def test_a_slash_command_caveat_never_becomes_a_title(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    caveat = (
        "<local-command-caveat>Caveat: The messages below were generated by the user while "
        "running local commands. DO NOT respond to these messages.</local-command-caveat>"
        "<command-name>/clear</command-name><command-message>clear</command-message>"
    )
    directory = tmp_path / "projects" / "-tmp-example"
    directory.mkdir(parents=True)
    path = directory / "22222222-3333-4444-5555-666666666666.jsonl"
    path.write_text(
        record(
            "user",
            timestamp="2026-09-01T10:00:00.000Z",
            message={"role": "user", "content": [{"type": "text", "text": caveat}]},
        )
        + "\n"
    )
    convo = store.scan(path, "-tmp-example")
    assert "caveat" not in convo.display_title.lower()
    assert "DO NOT respond" not in convo.display_title
    assert convo.display_title == "/clear clear"


def test_an_unclosed_caveat_is_dropped_to_the_end(tmp_path):
    # The tag is not always closed, and the boilerplate after it must not survive as a title.
    text = "<local-command-caveat>Caveat: The messages below were generated by the user"
    assert render.clean(text) == ""


def test_cleaning_keeps_ordinary_text(tmp_path):
    assert render.clean("just a normal first message") == "just a normal first message"
    assert render.clean("hello <system-reminder>noise</system-reminder> there") == "hello  there"

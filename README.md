# claude-chat-manager

A manager for the Claude Code conversations stored on your own machine. It lists every
transcript under `~/.claude/projects`, grouped by the project it belongs to, summarizes any of them
with Claude, and deletes the ones you no longer want.

It ships three frontends over one shared core, so the terminal, the desktop and the browser all show
the same data and share the same settings:

| Command | Frontend |
| --- | --- |
| `claude-chat-manager` (or `ccm`) | Textual terminal interface |
| `claude-chat-manager gtk` | GTK4 / libadwaita window |
| `claude-chat-manager web` | local web interface at `http://127.0.0.1:8765` |
| `ccm list`, `ccm summarize`, `ccm review`, `ccm delete`, `ccm trash`, `ccm prune`, `ccm sweep` | plain command line |
| `ccm memory list\|show\|check\|delete\|health` | the same for memory files |

## Summarizing

The summarize button renders the transcript to plain text — dropping tool output, system reminders
and subagent chatter, keeping what was said and which tools were run — and pipes it into
`claude -p`. It uses the Claude Code CLI already on your PATH, so there is no API key to set up and
nothing leaves your machine except the text of that one conversation.

The prompt is yours to change, from the `Prompt` button in any frontend or by editing
`summary_prompt` in the config file. The default asks for three bulleted sections: what the
conversation was about, what was done, and what is still outstanding.

Summaries are cached in `~/.local/share/claude-chat-manager/summaries/`. A cached summary is marked `✓` in the
list, or `~` when the conversation has grown since it was written.

## Memories

Claude also leaves memory files behind: the global workspace at `~/.claude/memory/` and one directory
per project under `~/.claude/projects/<project>/memory/`. Each frontend shows them in the same three
panes as conversations — the sidebar lists the scopes under a **Memories** heading — with the same
two actions.

**Is it still true?** takes one memory and checks it against the project it describes: it reads the
files and paths the memory names, greps for the options and identifiers it mentions, and reports each
claim as **still true**, **no longer true** or **cannot tell**, with a verdict of *still true*, *out
of date* or *unclear*. Preferences and reasons, which no file can confirm, come back as "cannot
tell" rather than as a guess.

**Delete** removes the memory file *and* its pointer line from that scope's `MEMORY.md`, so nothing
is left pointing at a file that is gone.

`ccm memory health` reports the three ways a memory workspace rots: memories with no pointer in
`MEMORY.md` (which sessions therefore never load), pointers whose file has been deleted, and
`[[links]]` that resolve to no memory in any scope.

## Checking the outstanding items

The other button takes the outstanding items from a summary and asks whether they were ever dealt
with. It runs `claude -p` again — this time with the conversation's own project directory added and
only read-only tools allowed (`Read`, `Grep`, `Glob` and a few `git` commands) — and gets back one
finding per item: **done**, **still open** or **cannot tell**, each with the path, line, commit or
setting it found. It ends with a verdict of *safe to delete*, *still open* or *unclear*, shown
beside the conversation in the list as `✔`, `!` or `?`.

The check never edits anything. It runs from an empty working directory with the project added, so
the project's own hooks do not fire, and it asks for its answer as JSON so the tool can read the
verdict rather than guess at it.

## Scratchpads and what else a session leaves behind

A conversation is not the only thing a session writes. It also gets a **scratchpad** — a temporary
directory outside the project, for intermediate files that do not belong in the working tree — plus
an environment directory, snapshots of every file it edited, and an entry in Claude Code's session
registry. None of that is cleaned up when the conversation goes, and the scratchpads in particular
add up to gigabytes.

Every frontend shows the open conversation's scratchpad under its metadata — how many files and how
large — with **Open scratchpad**, which hands the directory to your desktop file manager, and
**Delete scratchpad**, which removes it outright rather than trashing it. In the terminal those are
`o` and `x`. On the command line:

```sh
ccm scratchpad 3f8a2c1b            # where it is and what it holds
ccm scratchpad 3f8a2c1b --open     # show it in the file manager
ccm scratchpad 3f8a2c1b --delete   # remove it
```

The **Leftovers** button — `l` in the terminal — measures everything left behind by sessions whose
conversation no longer exists, and offers the four kinds separately:

| Kind | What it is |
| --- | --- |
| Scratchpads | the temporary files a session wrote outside the project |
| Session environments | the per-session environment directory |
| File history | the snapshots taken before a session edited a file |
| Session records | the registry entry for a session whose process has gone |

A conversation sitting in the trash still counts as existing, so restoring it gets its scratchpad
back. The same sweep on the command line, which reports before it removes anything:

```sh
ccm sweep                                    # what is there, per kind
ccm sweep --scratchpads --session-env        # remove those two kinds
ccm sweep --all -y                           # remove every kind without asking
```

## Deleting

Deleting moves the transcript to `~/.local/share/claude-chat-manager/trash/` rather than destroying it, so a
mistake is recoverable. Set `trash_on_delete` to `false`, or pass `--purge` on the command line, to
delete outright. `ccm prune` removes project directories that no longer hold any conversations. Scratchpads are never
trashed — there is no undo for those.

## Installing

With Nix:

```sh
nix run github:BeatLink/claude-chat-manager          # terminal interface
nix run github:BeatLink/claude-chat-manager -- web   # web interface
nix profile install github:BeatLink/claude-chat-manager
```

Without Nix, from a checkout:

```sh
pip install -e '.[tui,gtk]'
```

The GTK frontend additionally needs GTK 4 and libadwaita with their GObject introspection data. The
terminal frontend needs Textual; the web frontend and the command line need nothing beyond the
standard library.

## Configuration

`~/.config/claude-chat-manager/config.json`, created by `ccm config --init`:

| Key | Default | Meaning |
| --- | --- | --- |
| `projects_dir` | `~/.claude/projects` | where transcripts are read from |
| `claude_dir` | `~/.claude` | where scratchpads and the other leftovers are looked for |
| `claude_bin` | `claude` | the CLI used for summaries |
| `file_manager` | *(xdg-open)* | the command a scratchpad is opened with |
| `model` | *(empty)* | model for summaries, empty for the CLI default |
| `summary_prompt` | see above | the prompt sent with every transcript |
| `review_prompt` | see above | the prompt for the outstanding-items check |
| `review_system_prompt` | see above | keeps the check read-only and machine-readable |
| `review_tools` | `Read`, `Grep`, `Glob`, `git` | the tools the check is allowed to use |
| `review_timeout` | `900` | seconds before a check is given up on |
| `memory_prompt` | see above | the prompt for checking whether a memory is still true |
| `global_memory_dir` | `~/.claude/memory` | the memory workspace that is not tied to a project |
| `vscode_state_db` | *(auto)* | where to read the editor's archived-session list from |
| `include_thinking` | `false` | include thinking blocks in the rendered transcript |
| `include_tool_calls` | `true` | include a one-line note per tool call |
| `include_tool_results` | `false` | include truncated tool output |
| `include_sidechains` | `false` | include subagent messages |
| `max_transcript_chars` | `120000` | transcripts longer than this lose their middle |
| `trash_on_delete` | `true` | deleted conversations go to the trash |
| `web_host`, `web_port` | `127.0.0.1`, `8765` | where the web frontend listens |

The web frontend has no authentication. Bind it to localhost unless you have put something in front
of it.

## Development

```sh
nix develop          # shell with textual, pygobject, gtk4 and pytest
pytest tests/        # unit tests, on synthetic transcripts
```

`claude_chat_manager/store.py` scans and caches the transcripts, `render.py` turns one into summarizable
text, `summarize.py` runs the CLI, and `tui.py`, `gtkapp.py` and `web.py` are the frontends.

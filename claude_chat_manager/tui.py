"""Terminal frontend: projects on the left, conversations in the middle, the open one on the right."""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
    TextArea,
)

from . import config as config_mod
from . import ide
from . import leftovers
from . import memories as memories_mod
from . import store, summarize

class Confirm(ModalScreen[bool]):
    """Yes or no dialog used before a conversation is deleted."""

    BINDINGS = [("escape", "dismiss(False)", "Cancel")]

    def __init__(self, question: str, detail: str) -> None:
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, id="question")
            yield Static(self.detail, id="detail")
            with Horizontal(id="buttons"):
                yield Button("Delete", variant="error", id="yes")
                yield Button("Cancel", variant="primary", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        """Close the dialog with the pressed answer."""
        self.dismiss(event.button.id == "yes")


class Leftovers(ModalScreen[list[str] | None]):
    """The four kinds of leftover, each offered for deletion once it has been measured."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel")]

    def __init__(self, cfg: config_mod.Config) -> None:
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("What sessions with no conversation left behind", id="question")
            yield Static("Measuring…", id="detail")
            yield Vertical(id="kinds")
            with Horizontal(id="buttons"):
                yield Button("Delete selected", variant="error", id="yes", disabled=True)
                yield Button("Cancel", variant="primary", id="no")

    def on_mount(self) -> None:
        """Start measuring as soon as the screen is up."""
        self.measure()

    @work(thread=True)
    def measure(self) -> None:
        """Walk the leftovers off the UI thread, which takes a moment on a full disk."""
        items = leftovers.orphans(cfg=self.cfg)
        self.app.call_from_thread(
            self.measured, leftovers.summary(items), len(items), sum(i.size for i in items)
        )

    def measured(self, rows: dict, count: int, total: int) -> None:
        """Show one checkbox per kind, disabled where there is nothing to delete."""
        self.query_one("#detail", Static).update(
            f"{count} left behind · {store.human_size(total)}" if count else "Nothing was left behind."
        )
        container = self.query_one("#kinds", Vertical)
        for kind, row in rows.items():
            container.mount(
                Checkbox(
                    f"{row['label']} — {row['count']}, {row['size_human']} — {row['help']}",
                    name=kind,
                    disabled=not row["count"],
                )
            )
        self.query_one("#yes", Button).disabled = not count

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        """Hand back the ticked kinds, or nothing at all."""
        if event.button.id != "yes":
            self.dismiss(None)
            return
        self.dismiss([box.name for box in self.query(Checkbox) if box.value and box.name])


class PromptEditor(ModalScreen[str | None]):
    """Editor for a prompt, saved back to the config file."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, title: str, prompt: str, default: str) -> None:
        super().__init__()
        self.title_text = title
        self.prompt = prompt
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"{self.title_text} — ctrl+s saves, escape cancels", id="question")
            yield TextArea(self.prompt, id="prompt")
            with Horizontal(id="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Restore default", id="default")
                yield Button("Cancel", id="cancel")

    def action_save(self) -> None:
        """Hand the edited prompt back to the app."""
        self.dismiss(self.query_one("#prompt", TextArea).text)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        """Save, reset to the default, or cancel."""
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "default":
            self.query_one("#prompt", TextArea).text = self.default
        else:
            self.dismiss(None)


class ChatManager(App):
    """The terminal application."""

    TITLE = "Claude Chat Manager"
    SUB_TITLE = "Claude Code conversations"

    CSS = """
    #panes { height: 1fr; }
    #sidebar { width: 30; border-right: solid $panel; }
    #middle { width: 5fr; }
    #detail { width: 4fr; border-left: solid $panel; padding: 0 1; }
    #projects { height: 1fr; }
    #conversations { height: 1fr; }
    #filter { display: none; }
    #filter.visible { display: block; }
    .pane-title { background: $panel; color: $text; padding: 0 1; text-style: bold; }
    #meta { color: $text-muted; padding: 0 1; }
    #legend { color: $text-muted; padding: 0 1; }
    #body { height: 1fr; }
    MarkdownUnorderedListItem { margin-bottom: 1; }
    MarkdownOrderedListItem { margin-bottom: 1; }
    MarkdownParagraph { margin-bottom: 1; }
    MarkdownH2, MarkdownH3 { margin-top: 1; }
    #dialog { background: $surface; border: thick $primary; padding: 1 2; width: 80%; height: auto;
              max-height: 80%; margin: 2 4; }
    #question { text-style: bold; padding-bottom: 1; }
    #prompt { height: 20; margin-bottom: 1; }
    #kinds { height: auto; margin-bottom: 1; }
    #buttons { height: auto; align: right middle; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        ("s", "summarize", "Summarize"),
        ("c", "check", "Check items"),
        ("d", "delete", "Delete"),
        ("o", "scratchpad", "Scratchpad"),
        ("x", "delete_scratchpad", "Del scratchpad"),
        ("l", "leftovers", "Leftovers"),
        ("t", "close_tab", "Close tab"),
        ("p", "prompt", "Prompt"),
        ("slash", "filter", "Filter"),
        ("r", "refresh", "Rescan"),
        ("e", "export", "Export"),
        ("S", "resummarize", "Redo summary"),
        ("C", "recheck", "Redo check"),
        ("escape", "clear_filter", "Clear filter"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: config_mod.Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.projects: list[store.Project] = []
        self.scopes: list[memories_mod.Scope] = []
        self.sidebar: list[tuple[str, int]] = []
        self.shown: list[store.Conversation] = []
        self.shown_memories: list[memories_mod.Memory] = []
        self.open_id: str | None = None
        self.open_memory: str | None = None
        self.scratchpad: leftovers.Leftover | None = None
        self.scratchpad_id: str | None = None
        self.mode = "conversations"
        self.filter_text = ""
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            with Vertical(id="sidebar"):
                yield Static("Browse", classes="pane-title")
                yield ListView(id="projects")
            with Vertical(id="middle"):
                yield Static("Conversations", id="middle-title", classes="pane-title")
                yield Static(store.LEGEND, id="legend")
                yield Input(placeholder="filter…", id="filter")
                yield DataTable(id="conversations", cursor_type="row", zebra_stripes=True)
            with Vertical(id="detail"):
                yield Static("Open conversation", id="detail-title", classes="pane-title")
                yield Static("", id="meta")
                yield Markdown("", id="body")
        yield Footer()

    def on_mount(self) -> None:
        """Load the conversations and focus the list."""
        self.load()
        self.query_one("#conversations", DataTable).focus()

    # Loading ------------------------------------------------------------------------------------

    def load(self, refresh: bool = False) -> None:
        """Scan the transcripts and the memories, and fill the sidebar with both."""
        self.projects = store.load_projects(self.cfg, refresh=refresh)
        self.scopes = memories_mod.load_scopes(self.cfg)
        listing = self.query_one("#projects", ListView)
        keep = listing.index or 0
        listing.clear()
        self.sidebar = []
        totals = store.stats(self.projects)

        listing.append(ListItem(Label("[b]CONVERSATIONS[/b]")))
        self.sidebar.append(("heading", 0))
        listing.append(ListItem(Label(f"  All projects  ({totals['conversations']})")))
        self.sidebar.append(("conversations", -1))
        for index, project in enumerate(self.projects):
            mark = "" if project.exists else " ×"
            listing.append(
                ListItem(Label(f"  {project.name}{mark}  ({len(project.conversations)})"))
            )
            self.sidebar.append(("conversations", index))

        memory_totals = memories_mod.stats(self.scopes)
        listing.append(ListItem(Label("[b]MEMORIES[/b]")))
        self.sidebar.append(("heading", 0))
        for index, scope in enumerate(self.scopes):
            listing.append(ListItem(Label(f"  {scope.name}  ({len(scope.memories)})")))
            self.sidebar.append(("memories", index))

        listing.index = min(keep, len(self.sidebar) - 1) if keep else 1
        self.show_middle()
        self.sub_title = (
            f"{totals['conversations']} conversations · {memory_totals['memories']} memories · "
            f"{store.human_size(totals['bytes'])}"
        )

    def sidebar_choice(self) -> tuple[str, int]:
        """What the sidebar is pointing at: a kind, and which project or scope."""
        index = self.query_one("#projects", ListView).index or 0
        if 0 <= index < len(self.sidebar):
            return self.sidebar[index]
        return ("conversations", -1)

    def current_project(self) -> store.Project | None:
        """The project selected in the sidebar, or None for the combined view."""
        kind, index = self.sidebar_choice()
        if kind != "conversations" or index < 0:
            return None
        try:
            return self.projects[index]
        except IndexError:
            return None

    def current_scope(self) -> memories_mod.Scope | None:
        """The memory scope selected in the sidebar."""
        kind, index = self.sidebar_choice()
        if kind != "memories":
            return None
        try:
            return self.scopes[index]
        except IndexError:
            return None

    def show_middle(self) -> None:
        """Fill the middle pane with whichever kind the sidebar points at."""
        kind, _ = self.sidebar_choice()
        if kind == "heading":
            return
        self.mode = "memories" if kind == "memories" else "conversations"
        memories = self.mode == "memories"
        self.query_one("#middle-title", Static).update("Memories" if memories else "Conversations")
        self.query_one("#detail-title", Static).update(
            "Open memory" if memories else "Open conversation"
        )
        self.query_one("#legend", Static).update(
            "✓ still true  ! out of date  ? unclear  u not in MEMORY.md" if memories else store.LEGEND
        )
        if memories:
            self.show_memories()
        else:
            self.show_conversations()

    def show_conversations(self) -> None:
        """Fill the table, keeping the open conversation selected where it still appears."""
        project = self.current_project()
        pool = (
            list(project.conversations)
            if project
            else [c for p in self.projects for c in p.conversations]
        )
        if self.filter_text:
            needle = self.filter_text.lower()
            pool = [
                c
                for c in pool
                if needle in (c.display_title + c.last_prompt + c.project_path).lower()
            ]
        pool.sort(key=lambda c: c.mtime, reverse=True)
        self.shown = pool

        table = self.query_one("#conversations", DataTable)
        table.clear(columns=True)
        columns = [" ", "when", "msgs"]
        if project is None:
            columns.append("project")
        columns.append("title")
        table.add_columns(*columns)
        for convo in pool:
            row = [self.marks(convo), store.human_age(convo.mtime), str(convo.messages)]
            if project is None:
                row.append(Path(convo.project_path).name[:16])
            row.append(convo.display_title)
            table.add_row(*row, key=convo.session_id)

        if not any(c.session_id == self.open_id for c in pool):
            self.open_id = pool[0].session_id if pool else None
        if self.open_id:
            index = next(i for i, c in enumerate(pool) if c.session_id == self.open_id)
            table.move_cursor(row=index)
        self.show_detail()

    def marks(self, convo: store.Conversation) -> str:
        """The status glyphs for one row."""
        return store.marks(
            convo, summarize.load(convo.session_id), summarize.load_review(convo.session_id)
        )

    def show_memories(self) -> None:
        """Fill the table with the selected scope's memories."""
        scope = self.current_scope()
        pool = list(scope.memories) if scope else []
        if self.filter_text:
            needle = self.filter_text.lower()
            pool = [
                m
                for m in pool
                if needle in (m.name + m.description + m.kind + m.body).lower()
            ]
        self.shown_memories = pool

        table = self.query_one("#conversations", DataTable)
        table.clear(columns=True)
        table.add_columns(" ", "when", "kind", "name", "description")
        for memory in pool:
            check = summarize.load_memory_check(memory)
            mark = {"current": "✓", "stale": "!"}.get(check.verdict, "?") if check else " "
            table.add_row(
                f"{mark}{'' if memory.indexed else 'u'}",
                memory.age,
                memory.kind,
                memory.name,
                memory.description,
                key=memory.name,
            )
        if not any(m.name == self.open_memory for m in pool):
            self.open_memory = pool[0].name if pool else None
        if self.open_memory:
            index = next(i for i, m in enumerate(pool) if m.name == self.open_memory)
            table.move_cursor(row=index)
        self.show_detail()

    def opened_memory(self) -> memories_mod.Memory | None:
        """The memory shown on the right."""
        return next((m for m in self.shown_memories if m.name == self.open_memory), None)

    def memory_detail(self) -> None:
        """Update the right hand pane for the open memory."""
        memory = self.opened_memory()
        meta = self.query_one("#meta", Static)
        body = self.query_one("#body", Markdown)
        if not memory:
            meta.update("")
            body.update("*No memory selected.*")
            return
        bits = [
            memory.scope,
            memory.kind or "no type",
            f"{memory.modified:%Y-%m-%d %H:%M}",
            memory.size_human,
        ]
        if not memory.indexed:
            bits.append("not in MEMORY.md")
        if memory.links:
            bits.append(f"links: {', '.join(memory.links)}")
        meta.update(f"{memory.name}\n{memory.path}\n" + " · ".join(bits))
        if self.busy:
            return

        parts = [f"*{memory.description}*" if memory.description else "", memory.body]
        check = summarize.load_memory_check(memory)
        parts.append("## Is it still true?")
        if not check:
            parts.append("*Not checked — press `c` to check it against the project.*")
        else:
            parts.append(f"**Verdict: {check.label}**")
            parts.extend(f"- {line}" for line in check.lines)
            if check.note:
                parts.append(check.note)
        body.update("\n\n".join(part for part in parts if part))

    def opened(self) -> store.Conversation | None:
        """The conversation shown on the right, which every action works on."""
        return next((c for c in self.shown if c.session_id == self.open_id), None)

    def show_detail(self) -> None:
        """Update the right hand pane for whatever is open."""
        if self.mode == "memories":
            self.memory_detail()
            return
        convo = self.opened()
        meta = self.query_one("#meta", Static)
        body = self.query_one("#body", Markdown)
        if not convo:
            meta.update("")
            body.update("*No conversation selected.*")
            return
        tokens = convo.input_tokens + convo.output_tokens
        lines = [
            convo.display_title,
            convo.project_path,
            f"{convo.modified:%Y-%m-%d %H:%M} · {convo.messages} messages · "
            f"{convo.tool_calls} tool calls · {store.human_size(convo.size)}"
            + (f" · {tokens:,} tokens" if tokens else "")
            + (f" · branch {convo.git_branch}" if convo.git_branch else ""),
        ]
        if convo.state:
            lines.append(f"{store.STATE_WORDS[convo.state].capitalize()}.")
        pad = self.scratchpad
        if pad and pad.session_id == convo.session_id:
            lines.append(f"Scratchpad: {pad.files} files, {pad.size_human} — o opens it, x deletes it")
        label = ide.tab_is_open(convo.session_id, convo.project_path, self.cfg)
        if label:
            lines.append(f"Open in the editor as {label!r} — t closes that tab")
        meta.update("\n".join(lines))
        self.measure_scratchpad(convo.session_id)
        if self.busy:
            return
        body.update(self.detail_markdown(convo))

    def detail_markdown(self, convo: store.Conversation) -> str:
        """The summary and the outstanding-items check, as markdown."""
        parts = []
        summary = summarize.load(convo.session_id)
        if summary:
            parts.append(summary.text)
            if summary.stale(convo):
                parts.append("*This summary predates the newest messages — press `S` to redo it.*")
        else:
            parts.append("*No summary yet — press `s` to write one.*")

        review = summarize.load_review(convo.session_id)
        parts.append("## Outstanding items")
        if not review:
            parts.append("*Not checked — press `c` to check them against the project.*")
        else:
            parts.append(f"**Verdict: {review.label}**")
            parts.extend(f"- {line}" for line in review.lines)
            if review.note:
                parts.append(review.note)
            if review.stale(convo):
                parts.append("*This check predates the newest messages — press `C` to redo it.*")
        return "\n\n".join(parts)

    # Events -------------------------------------------------------------------------------------

    @on(ListView.Highlighted, "#projects")
    def project_changed(self) -> None:
        """Show whatever the newly highlighted sidebar entry holds."""
        self.show_middle()

    @on(DataTable.RowHighlighted, "#conversations")
    def row_changed(self, event: DataTable.RowHighlighted) -> None:
        """Open whichever row the cursor moved to."""
        key = event.row_key.value if event.row_key else None
        if not key:
            return
        if self.mode == "memories":
            if key != self.open_memory:
                self.open_memory = key
                self.show_detail()
        elif key != self.open_id:
            self.open_id = key
            self.scratchpad = None
            self.scratchpad_id = None
            self.show_detail()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        """Apply the filter as it is typed."""
        self.filter_text = event.value
        self.show_middle()

    @on(Input.Submitted, "#filter")
    def filter_done(self) -> None:
        """Leave the filter box but keep the filter."""
        self.query_one("#conversations", DataTable).focus()

    # Actions ------------------------------------------------------------------------------------

    def action_filter(self) -> None:
        """Reveal the filter box and focus it."""
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.focus()

    def action_clear_filter(self) -> None:
        """Drop the filter and hide its box."""
        box = self.query_one("#filter", Input)
        box.value = ""
        box.remove_class("visible")
        self.filter_text = ""
        self.show_middle()
        self.query_one("#conversations", DataTable).focus()

    def action_refresh(self) -> None:
        """Rescan every transcript from disk."""
        self.notify("Rescanning transcripts…")
        self.load(refresh=True)

    def action_summarize(self) -> None:
        """Summarize the open conversation unless a current summary exists."""
        if self.mode == "memories":
            self.notify("Memories are not summarized — press c to check one.")
            return
        convo = self.opened()
        if not convo:
            return
        cached = summarize.load(convo.session_id)
        if cached and not cached.stale(convo):
            self.notify("Already summarized — press S to redo it.")
            return
        self.start("summary", convo)

    def action_resummarize(self) -> None:
        """Summarize the open conversation again."""
        if convo := self.opened():
            self.start("summary", convo)

    def action_check(self) -> None:
        """Check the open item, unless that was already done."""
        if self.mode == "memories":
            memory = self.opened_memory()
            if not memory:
                return
            if summarize.load_memory_check(memory):
                self.notify("Already checked — press C to check again.")
                return
            self.start("memory", memory)
            return
        convo = self.opened()
        if not convo:
            return
        cached = summarize.load_review(convo.session_id)
        if cached and not cached.stale(convo):
            self.notify("Already checked — press C to check again.")
            return
        self.start("review", convo)

    def action_recheck(self) -> None:
        """Check the open item again."""
        if self.mode == "memories":
            if memory := self.opened_memory():
                self.start("memory", memory)
            return
        if convo := self.opened():
            self.start("review", convo)

    def start(self, kind: str, subject) -> None:
        """Run a summary or a check in a worker thread."""
        if self.busy:
            self.notify("Already working on one.")
            return
        self.busy = True
        waiting = {
            "summary": "*Summarizing the conversation…*",
            "review": f"*Checking the outstanding items against `{getattr(subject, 'project_path', '')}`…*",
            "memory": "*Checking whether this memory is still true…*",
        }[kind]
        self.query_one("#body", Markdown).update(waiting)
        self.worker(kind, subject)

    @work(thread=True)
    def worker(self, kind: str, subject) -> None:
        """Run the CLI off the UI thread and show the result."""
        try:
            if kind == "summary":
                result = summarize.run(subject, self.cfg)
                message = f"Summarized in {result.seconds}s"
            elif kind == "review":
                result = summarize.review(subject, self.cfg)
                message = f"Checked in {result.seconds}s — {result.label}"
            else:
                result = summarize.check_memory(subject, self.cfg)
                message = f"Checked in {result.seconds}s — {result.label}"
        except summarize.SummaryError as exc:
            self.call_from_thread(self.finished, f"Failed: {exc}", True)
            return
        self.call_from_thread(self.finished, message, False)

    def finished(self, message: str, failed: bool) -> None:
        """Clear the busy state and redraw."""
        self.busy = False
        self.notify(message, severity="error" if failed else "information")
        self.show_middle()

    def action_delete(self) -> None:
        """Ask before deleting whatever is open."""
        if self.mode == "memories":
            memory = self.opened_memory()
            if not memory:
                return
            detail = (
                f"{memory.name}\n\n{memory.path}\n{memory.description}\n\n"
                "Its pointer line in MEMORY.md goes with it."
            )
            self.push_screen(Confirm("Delete this memory?", detail), self.delete_memory_answered)
            return
        convo = self.opened()
        if not convo:
            return
        where = (
            f"It moves to {store.trash_dir(self.cfg)}, and `ccm trash --restore` puts it back."
            if self.cfg.trash_on_delete
            else "It is deleted outright."
        )
        live = "\n\nThis conversation was written to moments ago — a session may still have it open."
        label = ide.tab_is_open(convo.session_id, convo.project_path, self.cfg)
        tab = f"\n\nIts editor tab {label!r} is closed first." if label else ""
        pid = leftovers.running_session(convo.session_id, self.cfg)
        running = f"\n\nA session is still running it (pid {pid}), and is stopped first."
        detail = (
            f"{convo.display_title}\n\n{convo.path}\n"
            f"{convo.messages} messages · {store.human_size(convo.size)}\n\n{where}"
            + tab
            + (running if pid else live if convo.live else "")
        )
        self.push_screen(Confirm("Delete this conversation?", detail), self.delete_answered)

    def delete_answered(self, confirmed: bool | None) -> None:
        """Carry out a confirmed deletion of the conversation that was open."""
        convo = self.opened()
        if not confirmed or not convo:
            return
        if ide.tab_is_open(convo.session_id, convo.project_path, self.cfg):
            self.notify(ide.close_tab_quietly(convo.session_id, convo.project_path, self.cfg))
        self.open_id = None
        self.delete_conversation(convo)

    @work(thread=True)
    def delete_conversation(self, convo) -> None:
        """Stop the session still running it, delete the transcript, then remove anything written back."""
        ended = leftovers.end_session(convo.session_id, self.cfg)
        where = store.delete(convo, self.cfg)
        summarize.forget(convo.session_id)
        gone = "Deleted" if where == "deleted" else f"Moved to {Path(where).name}"
        self.call_from_thread(self.notify, "; ".join(part for part in (gone, ended) if part))
        self.call_from_thread(self.load)
        message = store.purge_rebirth(convo)
        if message:
            self.call_from_thread(self.notify, message.capitalize())
            self.call_from_thread(self.load)

    def delete_memory_answered(self, confirmed: bool | None) -> None:
        """Carry out a confirmed deletion of the memory that was open."""
        memory = self.opened_memory()
        if not confirmed or not memory:
            return
        where = memories_mod.delete(memory, self.cfg)
        summarize.forget_memory_check(memory)
        self.open_memory = None
        self.notify("Deleted" if where == "deleted" else f"Moved to {Path(where).name}")
        self.load()

    @work(thread=True, exclusive=True, group="scratchpad")
    def measure_scratchpad(self, session_id: str) -> None:
        """Measure one conversation's scratchpad off the UI thread, since walking it can be slow."""
        if self.scratchpad_id == session_id:
            return
        pad = leftovers.scratchpad_for(session_id, self.cfg)
        self.call_from_thread(self.scratchpad_measured, session_id, pad)

    def scratchpad_measured(self, session_id: str, pad) -> None:
        """Redraw with the measured scratchpad, unless the cursor has moved on."""
        convo = self.opened()
        if not convo or convo.session_id != session_id:
            return
        self.scratchpad = pad
        self.scratchpad_id = session_id
        if pad:
            self.show_detail()

    def action_scratchpad(self) -> None:
        """Show the open conversation's scratchpad in the desktop file manager."""
        if not self.scratchpad:
            self.notify("This session left no scratchpad.")
            return
        try:
            leftovers.open_in_file_manager(self.scratchpad.open_path, self.cfg)
        except OSError as exc:
            self.notify(f"Could not open it: {exc}", severity="error")
            return
        self.notify(f"Opened {self.scratchpad.open_path}")

    def action_delete_scratchpad(self) -> None:
        """Ask before deleting the open conversation's scratchpad."""
        pad = self.scratchpad
        if not pad:
            self.notify("This session left no scratchpad.")
            return
        detail = (
            f"{pad.open_path}\n\n{pad.files} files · {pad.size_human}\n\n"
            "It is deleted outright rather than moved to the trash."
        )
        self.push_screen(Confirm("Delete this scratchpad?", detail), self.delete_scratchpad_answered)

    def delete_scratchpad_answered(self, confirmed: bool | None) -> None:
        """Carry out a confirmed deletion of the scratchpad that was shown."""
        pad = self.scratchpad
        if not confirmed or not pad:
            return
        gone = leftovers.remove(pad)
        self.notify(f"Deleted, freeing {pad.size_human}" if gone else "Nothing could be removed")
        self.scratchpad = None
        self.show_detail()

    def action_close_tab(self) -> None:
        """Close the open conversation's tab in the editor, leaving the conversation alone."""
        convo = self.opened() if self.mode == "conversations" else None
        if not convo:
            return
        try:
            message = ide.close_conversation_tab(convo.session_id, convo.project_path, self.cfg)
        except ide.BridgeError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(message)
        self.show_detail()

    def action_leftovers(self) -> None:
        """Open the sweep screen for what sessions with no conversation left behind."""
        self.push_screen(Leftovers(self.cfg), self.leftovers_chosen)

    def leftovers_chosen(self, kinds: list[str] | None) -> None:
        """Delete the kinds that were ticked, off the UI thread."""
        if not kinds:
            return
        self.notify("Deleting…")
        self.sweep(kinds)

    @work(thread=True)
    def sweep(self, kinds: list[str]) -> None:
        """Remove every orphaned leftover of the chosen kinds."""
        count, freed = leftovers.remove_all(leftovers.orphans(kinds, self.cfg))
        self.call_from_thread(
            self.notify, f"Removed {count}, freeing {store.human_size(freed)}"
        )

    def action_prompt(self) -> None:
        """Edit the summary prompt, then the review prompt."""
        self.push_screen(
            PromptEditor("Summary prompt", self.cfg.summary_prompt, config_mod.DEFAULT_SUMMARY_PROMPT),
            self.summary_prompt_edited,
        )

    def summary_prompt_edited(self, prompt: str | None) -> None:
        """Save the summary prompt, then offer the review prompt."""
        if prompt is not None:
            self.cfg.summary_prompt = prompt.strip()
            config_mod.save(self.cfg)
            self.notify("Summary prompt saved")
        self.push_screen(
            PromptEditor("Outstanding items prompt", self.cfg.review_prompt, config_mod.DEFAULT_REVIEW_PROMPT),
            self.review_prompt_edited,
        )

    def review_prompt_edited(self, prompt: str | None) -> None:
        """Save the review prompt."""
        if prompt is None:
            return
        self.cfg.review_prompt = prompt.strip()
        path = config_mod.save(self.cfg)
        self.notify(f"Prompts saved to {path}")

    def action_export(self) -> None:
        """Write the open conversation's summary, check and transcript to the current directory."""
        from . import render

        convo = self.opened() if self.mode == "conversations" else None
        if not convo:
            return
        summary = summarize.load(convo.session_id)
        review = summarize.load_review(convo.session_id)
        target = (
            Path.cwd() / f"{convo.session_id[:8]}-{convo.display_title[:40].replace('/', '-')}.md"
        )
        parts = [
            f"# {convo.display_title}",
            "",
            f"- Session: `{convo.session_id}`",
            f"- Project: `{convo.project_path}`",
            f"- Modified: {convo.modified:%Y-%m-%d %H:%M}",
            "",
        ]
        if summary:
            parts += ["## Summary", "", summary.text, ""]
        if review:
            parts += ["## Outstanding items", "", f"Verdict: {review.label}", ""]
            parts += [f"- {line}\n" for line in review.lines]
        parts += ["## Transcript", "", render.transcript(convo.path, self.cfg)]
        target.write_text("\n".join(parts))
        self.notify(f"Wrote {target}")


def main(cfg: config_mod.Config | None = None) -> int:
    """Run the terminal frontend."""
    ChatManager(cfg or config_mod.load()).run()
    return 0

"""Terminal frontend: projects on the left, conversations in the middle, the open one on the right."""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
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
from . import store, summarize

VERDICT_MARKS = {"safe-to-delete": "✔", "keep": "!", "unclear": "?"}


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
    #body { height: 1fr; }
    MarkdownUnorderedListItem { margin-bottom: 1; }
    MarkdownOrderedListItem { margin-bottom: 1; }
    MarkdownParagraph { margin-bottom: 1; }
    MarkdownH2, MarkdownH3 { margin-top: 1; }
    #dialog { background: $surface; border: thick $primary; padding: 1 2; width: 80%; height: auto;
              max-height: 80%; margin: 2 4; }
    #question { text-style: bold; padding-bottom: 1; }
    #prompt { height: 20; margin-bottom: 1; }
    #buttons { height: auto; align: right middle; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        ("s", "summarize", "Summarize"),
        ("c", "check", "Check items"),
        ("d", "delete", "Delete"),
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
        self.shown: list[store.Conversation] = []
        self.open_id: str | None = None
        self.filter_text = ""
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="panes"):
            with Vertical(id="sidebar"):
                yield Static("Projects", classes="pane-title")
                yield ListView(id="projects")
            with Vertical(id="middle"):
                yield Static("Conversations", classes="pane-title")
                yield Input(placeholder="filter…", id="filter")
                yield DataTable(id="conversations", cursor_type="row", zebra_stripes=True)
            with Vertical(id="detail"):
                yield Static("Open conversation", classes="pane-title")
                yield Static("", id="meta")
                yield Markdown("", id="body")
        yield Footer()

    def on_mount(self) -> None:
        """Load the conversations and focus the list."""
        self.load()
        self.query_one("#conversations", DataTable).focus()

    # Loading ------------------------------------------------------------------------------------

    def load(self, refresh: bool = False) -> None:
        """Scan the transcripts and fill the project list."""
        self.projects = store.load_projects(self.cfg, refresh=refresh)
        listing = self.query_one("#projects", ListView)
        listing.clear()
        totals = store.stats(self.projects)
        listing.append(ListItem(Label(f"All projects  ({totals['conversations']})")))
        for project in self.projects:
            mark = "" if project.exists else " ×"
            listing.append(
                ListItem(Label(f"{project.name}{mark}  ({len(project.conversations)})"))
            )
        listing.index = 0
        self.show_conversations()
        self.sub_title = (
            f"{totals['conversations']} conversations · {totals['projects']} projects · "
            f"{store.human_size(totals['bytes'])}"
        )

    def current_project(self) -> store.Project | None:
        """The project the sidebar is pointing at, or None for the combined view."""
        index = self.query_one("#projects", ListView).index or 0
        if index == 0:
            return None
        try:
            return self.projects[index - 1]
        except IndexError:
            return None

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
        """The summary and verdict marks for one row."""
        summary = summarize.load(convo.session_id)
        review = summarize.load_review(convo.session_id)
        first = "✓" if summary and not summary.stale(convo) else ("~" if summary else " ")
        second = VERDICT_MARKS.get(review.verdict, " ") if review else " "
        return f"{first}{second}"

    def opened(self) -> store.Conversation | None:
        """The conversation shown on the right, which every action works on."""
        return next((c for c in self.shown if c.session_id == self.open_id), None)

    def show_detail(self) -> None:
        """Update the right hand pane for the open conversation."""
        convo = self.opened()
        meta = self.query_one("#meta", Static)
        body = self.query_one("#body", Markdown)
        if not convo:
            meta.update("")
            body.update("*No conversation selected.*")
            return
        tokens = convo.input_tokens + convo.output_tokens
        meta.update(
            f"{convo.display_title}\n{convo.project_path}\n"
            f"{convo.modified:%Y-%m-%d %H:%M} · {convo.messages} messages · "
            f"{convo.tool_calls} tool calls · {store.human_size(convo.size)}"
            + (f" · {tokens:,} tokens" if tokens else "")
            + (f" · branch {convo.git_branch}" if convo.git_branch else "")
            + ("\nStill being written to — a session may have it open." if convo.live else "")
        )
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
        """Show the conversations of the newly highlighted project."""
        self.show_conversations()

    @on(DataTable.RowHighlighted, "#conversations")
    def row_changed(self, event: DataTable.RowHighlighted) -> None:
        """Open whichever conversation the cursor moved to."""
        key = event.row_key.value if event.row_key else None
        if key and key != self.open_id:
            self.open_id = key
            self.show_detail()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        """Apply the filter as it is typed."""
        self.filter_text = event.value
        self.show_conversations()

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
        self.show_conversations()
        self.query_one("#conversations", DataTable).focus()

    def action_refresh(self) -> None:
        """Rescan every transcript from disk."""
        self.notify("Rescanning transcripts…")
        self.load(refresh=True)

    def action_summarize(self) -> None:
        """Summarize the open conversation unless a current summary exists."""
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
        """Check the open conversation's outstanding items unless that was already done."""
        convo = self.opened()
        if not convo:
            return
        cached = summarize.load_review(convo.session_id)
        if cached and not cached.stale(convo):
            self.notify("Already checked — press C to check again.")
            return
        self.start("review", convo)

    def action_recheck(self) -> None:
        """Check the open conversation's outstanding items again."""
        if convo := self.opened():
            self.start("review", convo)

    def start(self, kind: str, convo: store.Conversation) -> None:
        """Run a summary or a check in a worker thread."""
        if self.busy:
            self.notify("Already working on one.")
            return
        self.busy = True
        waiting = (
            "*Summarizing the conversation…*"
            if kind == "summary"
            else f"*Checking the outstanding items against `{convo.project_path}`…*"
        )
        self.query_one("#body", Markdown).update(waiting)
        self.worker(kind, convo)

    @work(thread=True)
    def worker(self, kind: str, convo: store.Conversation) -> None:
        """Run the CLI off the UI thread and show the result."""
        try:
            if kind == "summary":
                result = summarize.run(convo, self.cfg)
                message = f"Summarized in {result.seconds}s"
            else:
                result = summarize.review(convo, self.cfg)
                message = f"Checked in {result.seconds}s — {result.label}"
        except summarize.SummaryError as exc:
            self.call_from_thread(self.finished, f"Failed: {exc}", True)
            return
        self.call_from_thread(self.finished, message, False)

    def finished(self, message: str, failed: bool) -> None:
        """Clear the busy state and redraw."""
        self.busy = False
        self.notify(message, severity="error" if failed else "information")
        self.show_conversations()

    def action_delete(self) -> None:
        """Ask before deleting the open conversation."""
        convo = self.opened()
        if not convo:
            return
        where = (
            f"It moves to {store.trash_dir(self.cfg)}, and `ccm trash --restore` puts it back."
            if self.cfg.trash_on_delete
            else "It is deleted outright."
        )
        live = "\n\nThis conversation was written to moments ago — a session may still have it open."
        detail = (
            f"{convo.display_title}\n\n{convo.path}\n"
            f"{convo.messages} messages · {store.human_size(convo.size)}\n\n{where}"
            + (live if convo.live else "")
        )
        self.push_screen(Confirm("Delete this conversation?", detail), self.delete_answered)

    def delete_answered(self, confirmed: bool | None) -> None:
        """Carry out a confirmed deletion of the conversation that was open."""
        convo = self.opened()
        if not confirmed or not convo:
            return
        where = store.delete(convo, self.cfg)
        summarize.forget(convo.session_id)
        self.open_id = None
        self.notify("Deleted" if where == "deleted" else f"Moved to {Path(where).name}")
        self.load()

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

        convo = self.opened()
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

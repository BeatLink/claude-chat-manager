"""Terminal frontend: projects on the left, conversations in the middle, summary on the right."""

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
    """Editor for the summarization prompt, saved back to the config file."""

    BINDINGS = [("escape", "dismiss(None)", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Summarization prompt — ctrl+s saves, escape cancels", id="question")
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
            self.query_one("#prompt", TextArea).text = config_mod.DEFAULT_SUMMARY_PROMPT
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
    #dialog { background: $surface; border: thick $primary; padding: 1 2; width: 80%; height: auto;
              max-height: 80%; margin: 2 4; }
    #question { text-style: bold; padding-bottom: 1; }
    #detailtext { padding-bottom: 1; }
    #prompt { height: 20; margin-bottom: 1; }
    #buttons { height: auto; align: right middle; }
    #buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        ("s", "summarize", "Summarize"),
        ("S", "resummarize", "Re-summarize"),
        ("d", "delete", "Delete"),
        ("p", "prompt", "Prompt"),
        ("slash", "filter", "Filter"),
        ("r", "refresh", "Rescan"),
        ("e", "export", "Export"),
        ("escape", "clear_filter", "Clear filter"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: config_mod.Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.projects: list[store.Project] = []
        self.shown: list[store.Conversation] = []
        self.filter_text = ""

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
                yield Static("Summary", classes="pane-title")
                yield Static("", id="meta")
                yield Markdown("", id="summary")
        yield Footer()

    def on_mount(self) -> None:
        """Set up the table and load the conversations."""
        self.load()
        self.query_one("#conversations", DataTable).focus()

    # Loading ------------------------------------------------------------------------------------

    def load(self, refresh: bool = False) -> None:
        """Scan the transcripts and fill the project list."""
        self.projects = store.load_projects(self.cfg, refresh=refresh)
        listing = self.query_one("#projects", ListView)
        listing.clear()
        totals = store.stats(self.projects)
        listing.append(
            ListItem(Label(f"All projects  ({totals['conversations']})"), id="project-all")
        )
        for index, project in enumerate(self.projects):
            mark = "" if project.exists else " ×"
            listing.append(
                ListItem(
                    Label(f"{project.name}{mark}  ({len(project.conversations)})"),
                    id=f"project-{index}",
                )
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
        """Fill the table from the selected project and the active filter."""
        project = self.current_project()
        pool = project.conversations if project else [
            c for p in self.projects for c in p.conversations
        ]
        if self.filter_text:
            needle = self.filter_text.lower()
            pool = [
                c
                for c in pool
                if needle in (c.display_title + c.last_prompt + c.project_path).lower()
            ]
        else:
            pool = list(pool)
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
            cached = summarize.load(convo.session_id)
            mark = "✓" if cached and not cached.stale(convo) else ("~" if cached else " ")
            row = [mark, store.human_age(convo.mtime), str(convo.messages)]
            if project is None:
                row.append(Path(convo.project_path).name[:16])
            row.append(convo.display_title)
            table.add_row(*row, key=convo.session_id)
        self.show_detail()

    def selected(self) -> store.Conversation | None:
        """The conversation under the table cursor."""
        table = self.query_one("#conversations", DataTable)
        if not self.shown or table.cursor_row is None or table.cursor_row >= len(self.shown):
            return None
        return self.shown[table.cursor_row]

    def show_detail(self) -> None:
        """Update the right hand pane for the selected conversation."""
        convo = self.selected()
        meta = self.query_one("#meta", Static)
        body = self.query_one("#summary", Markdown)
        if not convo:
            meta.update("")
            body.update("*No conversation selected.*")
            return
        tokens = convo.input_tokens + convo.output_tokens
        meta.update(
            f"{convo.session_id}\n{convo.project_path}\n"
            f"{convo.modified:%Y-%m-%d %H:%M} · {convo.messages} messages · "
            f"{convo.tool_calls} tool calls · {store.human_size(convo.size)}"
            + (f" · {tokens:,} tokens" if tokens else "")
            + (f" · branch {convo.git_branch}" if convo.git_branch else "")
        )
        cached = summarize.load(convo.session_id)
        if not cached:
            body.update("*No summary yet — press `s` to write one.*")
            return
        stale = "\n\n> This summary predates the newest messages. Press `S` to redo it." if cached.stale(convo) else ""
        body.update(f"{cached.text}{stale}")

    # Events -------------------------------------------------------------------------------------

    @on(ListView.Highlighted, "#projects")
    def project_changed(self) -> None:
        """Show the conversations of the newly highlighted project."""
        self.show_conversations()

    @on(DataTable.RowHighlighted, "#conversations")
    def row_changed(self) -> None:
        """Show the details of the newly highlighted conversation."""
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
        """Summarize the selected conversation unless a current summary exists."""
        convo = self.selected()
        if not convo:
            return
        cached = summarize.load(convo.session_id)
        if cached and not cached.stale(convo):
            self.notify("Already summarized — press S to redo it.")
            return
        self.start_summary(convo)

    def action_resummarize(self) -> None:
        """Summarize the selected conversation again, ignoring any cached summary."""
        convo = self.selected()
        if convo:
            self.start_summary(convo)

    def start_summary(self, convo: store.Conversation) -> None:
        """Kick off a summary in a worker thread."""
        self.query_one("#summary", Markdown).update(
            f"*Summarizing with `{self.cfg.claude_bin}`… this takes a few seconds.*"
        )
        self.summarize_worker(convo)

    @work(thread=True)
    def summarize_worker(self, convo: store.Conversation) -> None:
        """Run the summarizer off the UI thread and show the result."""
        try:
            result = summarize.run(convo, self.cfg)
        except summarize.SummaryError as exc:
            self.call_from_thread(self.notify, f"Summary failed: {exc}", severity="error")
            self.call_from_thread(self.show_detail)
            return
        self.call_from_thread(self.notify, f"Summarized in {result.seconds}s")
        self.call_from_thread(self.show_conversations)

    def action_delete(self) -> None:
        """Ask before deleting the selected conversation."""
        convo = self.selected()
        if not convo:
            return
        detail = (
            f"{convo.display_title}\n\n{convo.path}\n"
            f"{convo.messages} messages · {store.human_size(convo.size)}\n\n"
            + ("Moved to the trash." if self.cfg.trash_on_delete else "Deleted outright.")
        )
        self.push_screen(Confirm("Delete this conversation?", detail), self.delete_answered)

    def delete_answered(self, confirmed: bool | None) -> None:
        """Carry out a confirmed deletion."""
        convo = self.selected()
        if not confirmed or not convo:
            return
        where = store.delete(convo, self.cfg)
        summarize.forget(convo.session_id)
        self.notify("Deleted" if where == "deleted" else f"Moved to {where}")
        self.load()

    def action_prompt(self) -> None:
        """Edit the summarization prompt."""
        self.push_screen(PromptEditor(self.cfg.summary_prompt), self.prompt_edited)

    def prompt_edited(self, prompt: str | None) -> None:
        """Save an edited prompt to the config file."""
        if prompt is None:
            return
        self.cfg.summary_prompt = prompt.strip()
        path = config_mod.save(self.cfg)
        self.notify(f"Prompt saved to {path}")

    def action_export(self) -> None:
        """Write the selected conversation's summary and transcript to the current directory."""
        from . import render

        convo = self.selected()
        if not convo:
            return
        cached = summarize.load(convo.session_id)
        target = Path.cwd() / f"{convo.session_id[:8]}-{convo.display_title[:40].replace('/', '-')}.md"
        parts = [f"# {convo.display_title}", "", f"- Session: `{convo.session_id}`",
                 f"- Project: `{convo.project_path}`", f"- Modified: {convo.modified:%Y-%m-%d %H:%M}", ""]
        if cached:
            parts += ["## Summary", "", cached.text, ""]
        parts += ["## Transcript", "", render.transcript(convo.path, self.cfg)]
        target.write_text("\n".join(parts))
        self.notify(f"Wrote {target}")


def main(cfg: config_mod.Config | None = None) -> int:
    """Run the terminal frontend."""
    ChatManager(cfg or config_mod.load()).run()
    return 0

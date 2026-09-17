"""GTK4 frontend: a three pane window over the same store, summarizer and config."""

from __future__ import annotations

import html
import re
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import config as config_mod  # noqa: E402
from . import store, summarize  # noqa: E402

BOLD = re.compile(r"\*\*(.+?)\*\*")
CODE = re.compile(r"`([^`]+)`")
ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")

VERDICT_MARKS = {"safe-to-delete": "✔", "keep": "!", "unclear": "?"}


def to_pango(text: str) -> str:
    """Convert the small slice of markdown a summary uses into Pango markup."""
    lines: list[str] = []
    for line in html.escape(text).splitlines():
        stripped = line.strip()
        bullet = stripped.startswith(("- ", "* "))
        if bullet:
            line = "  • " + stripped[2:]
        elif stripped.startswith("#"):
            line = f"<span size='large' weight='bold'>{stripped.lstrip('# ')}</span>"
        line = BOLD.sub(r"<b>\1</b>", line)
        line = ITALIC.sub(r"<i>\1</i>", line)
        line = CODE.sub(r"<tt>\1</tt>", line)
        lines.append(line)
        # A blank line after each bullet, so a list of findings is not a wall of text.
        if bullet:
            lines.append("")
    return "\n".join(lines).strip()


class Window(Adw.ApplicationWindow):
    """Main window: projects, conversations, and the one that is open."""

    def __init__(self, app: Adw.Application, cfg: config_mod.Config) -> None:
        super().__init__(
            application=app, title="Claude Chat Manager", default_width=1300, default_height=800
        )
        self.cfg = cfg
        self.projects: list[store.Project] = []
        self.shown: list[store.Conversation] = []
        self.project_index = 0
        self.open_id: str | None = None
        self.filter_text = ""
        self.busy = False
        self.selecting = False

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self.build_header())
        toolbar.set_content(self.build_body())
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)
        self.reload()

    # Construction -------------------------------------------------------------------------------

    def build_header(self) -> Adw.HeaderBar:
        """Header bar holding the search box and the action buttons."""
        header = Adw.HeaderBar()
        self.search = Gtk.SearchEntry(placeholder_text="Filter conversations", width_chars=24)
        self.search.connect("search-changed", self.on_search)
        header.pack_start(self.search)

        self.summary_button = Gtk.Button(label="Summarize", tooltip_text="Summarize with Claude")
        self.summary_button.add_css_class("suggested-action")
        self.summary_button.connect("clicked", lambda *_: self.start("summary", force=False))
        header.pack_end(self.summary_button)

        self.check_button = Gtk.Button(
            label="Check items", tooltip_text="Check the outstanding items against the project"
        )
        self.check_button.connect("clicked", lambda *_: self.start("review", force=False))
        header.pack_end(self.check_button)

        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete this conversation")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self.confirm_delete())
        header.pack_end(delete)

        prompts = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit the prompts")
        prompts.connect("clicked", lambda *_: self.edit_prompts())
        header.pack_end(prompts)

        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan transcripts")
        rescan.connect("clicked", lambda *_: self.reload(refresh=True))
        header.pack_end(rescan)
        return header

    def build_body(self) -> Gtk.Widget:
        """The three panes, split by draggable dividers."""
        self.project_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.project_list.add_css_class("navigation-sidebar")
        self.project_list.connect("row-selected", self.on_project_selected)
        projects = Gtk.ScrolledWindow(child=self.project_list, width_request=260)

        self.convo_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.convo_list.add_css_class("navigation-sidebar")
        self.convo_list.connect("row-selected", self.on_conversation_selected)
        conversations = Gtk.ScrolledWindow(child=self.convo_list, width_request=380)

        self.title_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.title_label.add_css_class("title-2")
        self.meta_label = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.meta_label.add_css_class("dim-label")
        self.summary_label = Gtk.Label(
            xalign=0, yalign=0, wrap=True, selectable=True, use_markup=True
        )
        self.verdict_label = Gtk.Label(xalign=0, wrap=True, use_markup=True)
        self.review_label = Gtk.Label(
            xalign=0, yalign=0, wrap=True, selectable=True, use_markup=True
        )
        heading = Gtk.Label(xalign=0, label="Outstanding items check")
        heading.add_css_class("heading")

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        for widget in (
            self.title_label,
            self.meta_label,
            self.summary_label,
            Gtk.Separator(),
            heading,
            self.verdict_label,
            self.review_label,
        ):
            box.append(widget)
        detail = Gtk.ScrolledWindow(child=box, hexpand=True)

        inner = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=430)
        inner.set_start_child(conversations)
        inner.set_end_child(detail)
        outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=260)
        outer.set_start_child(projects)
        outer.set_end_child(inner)
        return outer

    # Data ---------------------------------------------------------------------------------------

    def reload(self, refresh: bool = False) -> None:
        """Rescan and rebuild both lists."""
        self.projects = store.load_projects(self.cfg, refresh=refresh)
        while row := self.project_list.get_row_at_index(0):
            self.project_list.remove(row)
        totals = store.stats(self.projects)
        self.project_list.append(self.project_row("All projects", "", totals["conversations"]))
        for project in self.projects:
            subtitle = project.path if project.exists else f"{project.path}  (gone)"
            self.project_list.append(
                self.project_row(project.name, subtitle, len(project.conversations))
            )
        self.project_list.select_row(self.project_list.get_row_at_index(self.project_index))
        self.fill_conversations()

    def project_row(self, title: str, subtitle: str, count: int) -> Adw.ActionRow:
        """One row in the project sidebar."""
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle)
        )
        row.add_suffix(Gtk.Label(label=str(count), css_classes=["dim-label"]))
        return row

    def fill_conversations(self) -> None:
        """Rebuild the middle list, keeping the open conversation selected."""
        pool = (
            [c for p in self.projects for c in p.conversations]
            if self.project_index == 0
            else list(self.projects[self.project_index - 1].conversations)
            if self.project_index - 1 < len(self.projects)
            else []
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

        self.selecting = True
        while row := self.convo_list.get_row_at_index(0):
            self.convo_list.remove(row)
        for convo in pool:
            summary = summarize.load(convo.session_id)
            review = summarize.load_review(convo.session_id)
            marks = []
            if summary:
                marks.append("✓" if not summary.stale(convo) else "~")
            if review:
                marks.append(VERDICT_MARKS.get(review.verdict, "?"))
            subtitle = (
                f"{store.human_age(convo.mtime)} · {convo.messages} messages · "
                f"{store.human_size(convo.size)}"
            )
            if self.project_index == 0:
                subtitle = f"{Path(convo.project_path).name} · {subtitle}"
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(convo.display_title),
                subtitle=GLib.markup_escape_text(subtitle),
            )
            row.set_title_lines(1)
            if marks:
                row.add_prefix(Gtk.Label(label="".join(marks), css_classes=["accent"]))
            self.convo_list.append(row)

        if not any(c.session_id == self.open_id for c in pool):
            self.open_id = pool[0].session_id if pool else None
        index = next(
            (i for i, c in enumerate(pool) if c.session_id == self.open_id), -1
        )
        self.selecting = False
        if index >= 0:
            row = self.convo_list.get_row_at_index(index)
            self.convo_list.select_row(row)
            # Focusing the row is what scrolls it into view inside the scrolled window.
            row.grab_focus()
        self.show_detail()

    def opened(self) -> store.Conversation | None:
        """The conversation shown on the right, which every action works on."""
        return next((c for c in self.shown if c.session_id == self.open_id), None)

    def show_detail(self) -> None:
        """Update the right hand pane from the open conversation."""
        convo = self.opened()
        if convo is None:
            self.title_label.set_text("")
            self.meta_label.set_text("")
            self.summary_label.set_markup("<i>No conversation selected.</i>")
            self.verdict_label.set_markup("")
            self.review_label.set_markup("")
            return
        tokens = convo.input_tokens + convo.output_tokens
        bits = [
            convo.project_path,
            f"{convo.modified:%Y-%m-%d %H:%M}",
            f"{convo.messages} messages",
            f"{convo.tool_calls} tool calls",
            store.human_size(convo.size),
        ]
        if tokens:
            bits.append(f"{tokens:,} tokens")
        if convo.git_branch:
            bits.append(convo.git_branch)
        if convo.live:
            bits.append("still being written to")
        self.title_label.set_text(convo.display_title)
        self.meta_label.set_text(" · ".join(bits) + f"\n{convo.session_id}")
        if self.busy:
            return

        summary = summarize.load(convo.session_id)
        if not summary:
            self.summary_label.set_markup("<i>No summary yet — press Summarize.</i>")
        else:
            text = to_pango(summary.text)
            if summary.stale(convo):
                text += "\n\n<i>This summary predates the newest messages.</i>"
            self.summary_label.set_markup(text)

        review = summarize.load_review(convo.session_id)
        if not review:
            self.verdict_label.set_markup("")
            self.review_label.set_markup(
                "<i>Not checked — press Check items to test them against the project.</i>"
            )
            return
        self.verdict_label.set_markup(f"<b>Verdict: {review.label}</b>")
        body = to_pango("\n".join(f"- {line}" for line in review.lines) or "No outstanding items.")
        if review.note:
            body += f"\n{to_pango(review.note)}"
        if review.stale(convo):
            body += "\n\n<i>This check predates the newest messages.</i>"
        self.review_label.set_markup(body)

    # Events -------------------------------------------------------------------------------------

    def on_project_selected(self, _list, row) -> None:
        """Switch the middle list to another project."""
        if row is None:
            return
        self.project_index = row.get_index()
        self.fill_conversations()

    def on_conversation_selected(self, _list, row) -> None:
        """Open whichever conversation was clicked."""
        if row is None or self.selecting:
            return
        index = row.get_index()
        if 0 <= index < len(self.shown):
            self.open_id = self.shown[index].session_id
            self.show_detail()

    def on_search(self, entry) -> None:
        """Filter as the user types."""
        self.filter_text = entry.get_text()
        self.fill_conversations()

    def toast(self, message: str) -> None:
        """Show a transient message."""
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    # Actions ------------------------------------------------------------------------------------

    def start(self, kind: str, force: bool) -> None:
        """Summarize or check the open conversation in a background thread."""
        convo = self.opened()
        if not convo or self.busy:
            return
        cached = (
            summarize.load(convo.session_id)
            if kind == "summary"
            else summarize.load_review(convo.session_id)
        )
        if cached and not cached.stale(convo) and not force:
            self.toast("Already done — hold shift on the button to run it again.")
        self.busy = True
        self.summary_button.set_sensitive(False)
        self.check_button.set_sensitive(False)
        if kind == "summary":
            self.summary_label.set_markup(f"<i>Summarizing with {self.cfg.claude_bin}…</i>")
        else:
            self.review_label.set_markup(
                f"<i>Checking the outstanding items against {GLib.markup_escape_text(convo.project_path)}…</i>"
            )

        def worker() -> None:
            try:
                if kind == "summary":
                    result = summarize.run(convo, self.cfg)
                    message = f"Summarized in {result.seconds}s"
                else:
                    result = summarize.review(convo, self.cfg)
                    message = f"Checked in {result.seconds}s — {result.label}"
            except summarize.SummaryError as exc:
                GLib.idle_add(self.finished, f"Failed: {exc}")
                return
            GLib.idle_add(self.finished, message)

        threading.Thread(target=worker, daemon=True).start()

    def finished(self, message: str) -> bool:
        """Clear the busy state and redraw."""
        self.busy = False
        self.summary_button.set_sensitive(True)
        self.check_button.set_sensitive(True)
        self.toast(message)
        self.fill_conversations()
        return False

    def confirm_delete(self) -> None:
        """Ask before deleting the open conversation."""
        convo = self.opened()
        if not convo:
            return
        where = (
            f"It moves to {store.trash_dir(self.cfg)} and can be restored with "
            "ccm trash --restore."
            if self.cfg.trash_on_delete
            else "It is deleted outright."
        )
        body = f"{convo.display_title}\n\n{convo.path}\n\n{where}"
        if convo.live:
            body += "\n\nThis conversation was written to moments ago — a session may still have it open."
        dialog = Adw.AlertDialog(heading="Delete this conversation?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self.delete_answered, convo)
        dialog.present(self)

    def delete_answered(self, _dialog, response: str, convo) -> None:
        """Carry out a confirmed deletion of the conversation that was open."""
        if response != "delete":
            return
        where = store.delete(convo, self.cfg)
        summarize.forget(convo.session_id)
        self.open_id = None
        self.toast("Deleted" if where == "deleted" else f"Moved to {Path(where).name}")
        self.reload()

    def edit_prompts(self) -> None:
        """Open both prompts for editing."""
        window = Adw.Window(
            title="Prompts", default_width=820, default_height=620, transient_for=self, modal=True
        )
        notebook = Gtk.Notebook(vexpand=True)
        views = {}
        for key, label, default in (
            ("summary_prompt", "Summary", config_mod.DEFAULT_SUMMARY_PROMPT),
            ("review_prompt", "Outstanding items", config_mod.DEFAULT_REVIEW_PROMPT),
        ):
            view = Gtk.TextView(
                wrap_mode=Gtk.WrapMode.WORD,
                top_margin=12,
                bottom_margin=12,
                left_margin=12,
                right_margin=12,
            )
            view.get_buffer().set_text(getattr(self.cfg, key))
            views[key] = (view, default)
            notebook.append_page(Gtk.ScrolledWindow(child=view), Gtk.Label(label=label))

        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        default_button = Gtk.Button(label="Default for this tab")
        header.pack_end(save)
        header.pack_start(default_button)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(notebook)
        window.set_content(toolbar)

        def on_default(*_):
            key = list(views)[notebook.get_current_page()]
            view, default = views[key]
            view.get_buffer().set_text(default)

        def on_save(*_):
            for key, (view, _default) in views.items():
                buffer = view.get_buffer()
                text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
                setattr(self.cfg, key, text.strip())
            path = config_mod.save(self.cfg)
            self.toast(f"Prompts saved to {path}")
            window.close()

        default_button.connect("clicked", on_default)
        save.connect("clicked", on_save)
        window.present()


class Application(Adw.Application):
    """Wraps the window so it can be launched as a desktop application."""

    def __init__(self, cfg: config_mod.Config) -> None:
        super().__init__(application_id="dev.beatlink.ClaudeChatManager")
        self.cfg = cfg

    def do_activate(self) -> None:
        """Show the window, reusing it if it already exists."""
        window = self.props.active_window or Window(self, self.cfg)
        window.present()


def main(cfg: config_mod.Config | None = None) -> int:
    """Run the GTK frontend."""
    return Application(cfg or config_mod.load()).run([])


def run() -> None:
    """Entry point for the desktop launcher."""
    sys.exit(main())

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
from . import render, store, summarize  # noqa: E402

BOLD = re.compile(r"\*\*(.+?)\*\*")
CODE = re.compile(r"`([^`]+)`")
ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")


def to_pango(text: str) -> str:
    """Convert the small slice of markdown a summary uses into Pango markup."""
    lines = []
    for line in html.escape(text).splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            line = "  • " + stripped[2:]
        elif stripped.startswith("#"):
            line = f"<span size='large' weight='bold'>{stripped.lstrip('# ')}</span>"
        line = BOLD.sub(r"<b>\1</b>", line)
        line = ITALIC.sub(r"<i>\1</i>", line)
        line = CODE.sub(r"<tt>\1</tt>", line)
        lines.append(line)
    return "\n".join(lines)


class Window(Adw.ApplicationWindow):
    """Main window: projects, conversations, and the summary of the selected one."""

    def __init__(self, app: Adw.Application, cfg: config_mod.Config) -> None:
        super().__init__(application=app, title="Claude Chat Manager", default_width=1300, default_height=800)
        self.cfg = cfg
        self.projects: list[store.Project] = []
        self.shown: list[store.Conversation] = []
        self.project_index = 0
        self.filter_text = ""
        self.busy = False

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
        self.summary_button.connect("clicked", lambda *_: self.summarize(force=False))
        header.pack_end(self.summary_button)

        redo = Gtk.Button(icon_name="media-playlist-repeat-symbolic", tooltip_text="Summarize again")
        redo.connect("clicked", lambda *_: self.summarize(force=True))
        header.pack_end(redo)

        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete conversation")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda *_: self.confirm_delete())
        header.pack_end(delete)

        prompt = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Edit summary prompt")
        prompt.connect("clicked", lambda *_: self.edit_prompt())
        header.pack_end(prompt)

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
        self.summary_label = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True, use_markup=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=16,
                      margin_bottom=16, margin_start=16, margin_end=16)
        box.append(self.title_label)
        box.append(self.meta_label)
        box.append(self.summary_label)
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
        row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle))
        row.add_suffix(Gtk.Label(label=str(count), css_classes=["dim-label"]))
        return row

    def fill_conversations(self) -> None:
        """Rebuild the middle list from the selected project and the search text."""
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
                c for c in pool
                if needle in (c.display_title + c.last_prompt + c.project_path).lower()
            ]
        pool.sort(key=lambda c: c.mtime, reverse=True)
        self.shown = pool

        while row := self.convo_list.get_row_at_index(0):
            self.convo_list.remove(row)
        for convo in pool:
            cached = summarize.load(convo.session_id)
            mark = "✓" if cached and not cached.stale(convo) else ("~" if cached else "")
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
            if mark:
                row.add_prefix(Gtk.Label(label=mark, css_classes=["accent"]))
            self.convo_list.append(row)
        if pool:
            self.convo_list.select_row(self.convo_list.get_row_at_index(0))
        else:
            self.show_detail(None)

    def selected(self) -> store.Conversation | None:
        """The conversation selected in the middle list."""
        row = self.convo_list.get_selected_row()
        if row is None:
            return None
        index = row.get_index()
        return self.shown[index] if 0 <= index < len(self.shown) else None

    def show_detail(self, convo: store.Conversation | None) -> None:
        """Update the right hand pane."""
        if convo is None:
            self.title_label.set_text("")
            self.meta_label.set_text("")
            self.summary_label.set_markup("<i>No conversation selected.</i>")
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
        self.title_label.set_text(convo.display_title)
        self.meta_label.set_text(" · ".join(bits) + f"\n{convo.session_id}")
        cached = summarize.load(convo.session_id)
        if self.busy:
            return
        if not cached:
            self.summary_label.set_markup("<i>No summary yet — press Summarize.</i>")
            return
        text = to_pango(cached.text)
        if cached.stale(convo):
            text += "\n\n<i>This summary predates the newest messages.</i>"
        self.summary_label.set_markup(text)

    # Events -------------------------------------------------------------------------------------

    def on_project_selected(self, _list, row) -> None:
        """Switch the middle list to another project."""
        if row is None:
            return
        self.project_index = row.get_index()
        self.fill_conversations()

    def on_conversation_selected(self, _list, row) -> None:
        """Show the newly selected conversation."""
        self.show_detail(self.selected())

    def on_search(self, entry) -> None:
        """Filter as the user types."""
        self.filter_text = entry.get_text()
        self.fill_conversations()

    def toast(self, message: str) -> None:
        """Show a transient message."""
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    # Actions ------------------------------------------------------------------------------------

    def summarize(self, force: bool) -> None:
        """Summarize the selected conversation in a background thread."""
        convo = self.selected()
        if not convo or self.busy:
            return
        cached = summarize.load(convo.session_id)
        if cached and not cached.stale(convo) and not force:
            self.toast("Already summarized — use the redo button to run it again.")
            return
        self.busy = True
        self.summary_button.set_sensitive(False)
        self.summary_label.set_markup(f"<i>Summarizing with {self.cfg.claude_bin}…</i>")

        def worker() -> None:
            try:
                result = summarize.run(convo, self.cfg)
            except summarize.SummaryError as exc:
                GLib.idle_add(self.summary_done, convo, None, str(exc))
                return
            GLib.idle_add(self.summary_done, convo, result, None)

        threading.Thread(target=worker, daemon=True).start()

    def summary_done(self, convo, result, error) -> bool:
        """Put a finished summary on screen."""
        self.busy = False
        self.summary_button.set_sensitive(True)
        if error:
            self.toast(f"Summary failed: {error}")
        else:
            self.toast(f"Summarized in {result.seconds}s")
        selected = self.selected()
        self.fill_conversations()
        if selected and selected.session_id == convo.session_id:
            self.show_detail(convo)
        return False

    def confirm_delete(self) -> None:
        """Ask before deleting the selected conversation."""
        convo = self.selected()
        if not convo:
            return
        where = "moved to the trash" if self.cfg.trash_on_delete else "deleted outright"
        body = f"{convo.display_title}\n\n{convo.path}\n\nIt will be {where}."
        dialog = Adw.AlertDialog(heading="Delete this conversation?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self.delete_answered, convo)
        dialog.present(self)

    def delete_answered(self, _dialog, response: str, convo) -> None:
        """Carry out a confirmed deletion."""
        if response != "delete":
            return
        where = store.delete(convo, self.cfg)
        summarize.forget(convo.session_id)
        self.toast("Deleted" if where == "deleted" else f"Moved to {Path(where).name}")
        self.reload()

    def edit_prompt(self) -> None:
        """Open the summarization prompt for editing."""
        window = Adw.Window(title="Summary prompt", default_width=760, default_height=520,
                            transient_for=self, modal=True)
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD, top_margin=12, bottom_margin=12,
                            left_margin=12, right_margin=12)
        view.get_buffer().set_text(self.cfg.summary_prompt)
        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save", css_classes=["suggested-action"])
        default = Gtk.Button(label="Default")
        header.pack_end(save)
        header.pack_start(default)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(Gtk.ScrolledWindow(child=view, vexpand=True))
        window.set_content(toolbar)

        def on_default(*_):
            view.get_buffer().set_text(config_mod.DEFAULT_SUMMARY_PROMPT)

        def on_save(*_):
            buffer = view.get_buffer()
            self.cfg.summary_prompt = buffer.get_text(
                buffer.get_start_iter(), buffer.get_end_iter(), False
            ).strip()
            path = config_mod.save(self.cfg)
            self.toast(f"Prompt saved to {path}")
            window.close()

        default.connect("clicked", on_default)
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

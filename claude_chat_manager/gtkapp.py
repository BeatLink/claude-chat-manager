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

from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from . import config as config_mod  # noqa: E402
from . import ide  # noqa: E402
from . import leftovers  # noqa: E402
from . import memories as memories_mod  # noqa: E402
from . import store, summarize  # noqa: E402

BOLD = re.compile(r"\*\*(.+?)\*\*")
CODE = re.compile(r"`([^`]+)`")
ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")

def to_pango(text: str) -> str:
    """Convert the small slice of markdown a summary or a memory uses into Pango markup."""
    blocks: list[str] = []
    paragraph: list[str] = []
    in_list = False

    def flush() -> None:
        """Emit the paragraph built so far as one wrapped line."""
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    for raw in html.escape(text).splitlines():
        stripped = raw.strip()
        if not stripped:
            flush()
            in_list = False
            continue
        if stripped.startswith(("- ", "* ")):
            flush()
            blocks.append("  • " + stripped[2:])
            in_list = True
        elif in_list and not paragraph:
            # A wrapped continuation of the bullet above belongs to that bullet.
            blocks[-1] += " " + stripped
        elif stripped.startswith("#"):
            flush()
            in_list = False
            blocks.append(f"<span size='large' weight='bold'>{stripped.lstrip('# ')}</span>")
        else:
            # Source lines are hard wrapped, so a paragraph is rejoined and left to the label to wrap.
            paragraph.append(stripped)
    flush()

    out = []
    for block in blocks:
        block = BOLD.sub(r"<b>\1</b>", block)
        block = ITALIC.sub(r"<i>\1</i>", block)
        block = CODE.sub(r"<tt>\1</tt>", block)
        out.append(block)
    return "\n\n".join(out).strip()


class Window(Adw.ApplicationWindow):
    """Main window: projects, conversations, and the one that is open."""

    def __init__(self, app: Adw.Application, cfg: config_mod.Config) -> None:
        super().__init__(
            application=app, title="Claude Chat Manager", default_width=1300, default_height=800
        )
        self.cfg = cfg
        self.projects: list[store.Project] = []
        self.scopes: list[memories_mod.Scope] = []
        self.sidebar: list[tuple[str, int]] = []
        self.shown: list[store.Conversation] = []
        self.shown_memories: list[memories_mod.Memory] = []
        self.sidebar_index = 1
        self.mode = "conversations"
        self.open_id: str | None = None
        self.open_memory: str | None = None
        self.scratchpad: leftovers.Leftover | None = None
        self.scratchpad_id: str | None = None
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
        self.search.set_tooltip_text(store.LEGEND)
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

        sweep = Gtk.Button(
            icon_name="edit-clear-all-symbolic",
            tooltip_text="What sessions with no conversation left behind",
        )
        sweep.connect("clicked", lambda *_: self.show_leftovers())
        header.pack_end(sweep)

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
        self.scratchpad_label = Gtk.Label(
            xalign=0, hexpand=True, selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE
        )
        self.scratchpad_label.add_css_class("dim-label")
        self.scratchpad_open = Gtk.Button(label="Open scratchpad", valign=Gtk.Align.CENTER)
        self.scratchpad_open.connect("clicked", lambda *_: self.open_scratchpad())
        self.scratchpad_delete = Gtk.Button(
            label="Delete scratchpad", valign=Gtk.Align.CENTER, css_classes=["destructive-action"]
        )
        self.scratchpad_delete.connect("clicked", lambda *_: self.confirm_delete_scratchpad())
        self.scratchpad_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for widget in (self.scratchpad_label, self.scratchpad_open, self.scratchpad_delete):
            self.scratchpad_box.append(widget)

        self.tab_label_text = Gtk.Label(
            xalign=0, hexpand=True, selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE
        )
        self.tab_label_text.add_css_class("dim-label")
        self.tab_close = Gtk.Button(label="Close tab", valign=Gtk.Align.CENTER)
        self.tab_close.connect("clicked", lambda *_: self.close_editor_tab())
        self.tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.tab_box.append(self.tab_label_text)
        self.tab_box.append(self.tab_close)

        self.verdict_label = Gtk.Label(xalign=0, wrap=True, use_markup=True)
        self.review_label = Gtk.Label(
            xalign=0, yalign=0, wrap=True, selectable=True, use_markup=True
        )
        self.check_heading = Gtk.Label(xalign=0, label="Outstanding items check")
        self.check_heading.add_css_class("heading")

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
            self.tab_box,
            self.scratchpad_box,
            self.summary_label,
            Gtk.Separator(),
            self.check_heading,
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
        """Rescan the transcripts and the memories, and rebuild the sidebar."""
        self.projects = store.load_projects(self.cfg, refresh=refresh)
        self.scopes = memories_mod.load_scopes(self.cfg)
        self.selecting = True
        while row := self.project_list.get_row_at_index(0):
            self.project_list.remove(row)
        self.sidebar = []
        totals = store.stats(self.projects)

        self.project_list.append(self.heading_row("Conversations"))
        self.sidebar.append(("heading", 0))
        self.project_list.append(self.project_row("All projects", "", totals["conversations"]))
        self.sidebar.append(("conversations", -1))
        for index, project in enumerate(self.projects):
            subtitle = project.path if project.exists else f"{project.path}  (gone)"
            self.project_list.append(
                self.project_row(project.name, subtitle, len(project.conversations))
            )
            self.sidebar.append(("conversations", index))

        self.project_list.append(self.heading_row("Memories"))
        self.sidebar.append(("heading", 0))
        for index, scope in enumerate(self.scopes):
            self.project_list.append(
                self.project_row(scope.name, scope.path, len(scope.memories))
            )
            self.sidebar.append(("memories", index))

        self.sidebar_index = min(self.sidebar_index, len(self.sidebar) - 1)
        self.selecting = False
        chosen = self.project_list.get_row_at_index(self.sidebar_index)
        self.project_list.select_row(chosen)
        chosen.grab_focus()
        self.fill_middle()

    def heading_row(self, text: str) -> Gtk.ListBoxRow:
        """A non-selectable divider between the two halves of the sidebar."""
        label = Gtk.Label(label=text, xalign=0, margin_top=10, margin_start=6, margin_bottom=2)
        label.add_css_class("heading")
        label.add_css_class("dim-label")
        row = Gtk.ListBoxRow(child=label, selectable=False, activatable=False)
        return row

    def fill_middle(self) -> None:
        """Fill the middle pane with whichever kind the sidebar points at."""
        kind, _ = self.sidebar[self.sidebar_index] if self.sidebar else ("conversations", -1)
        if kind == "heading":
            return
        self.mode = "memories" if kind == "memories" else "conversations"
        self.summary_button.set_sensitive(self.mode == "conversations")
        self.check_button.set_label("Check memory" if self.mode == "memories" else "Check items")
        self.check_heading.set_label(
            "Is it still true?" if self.mode == "memories" else "Outstanding items check"
        )
        self.search.set_placeholder_text(
            "Filter memories" if self.mode == "memories" else "Filter conversations"
        )
        if self.mode == "memories":
            self.fill_memories()
        else:
            self.fill_conversations()

    def project_row(self, title: str, subtitle: str, count: int) -> Adw.ActionRow:
        """One row in the project sidebar."""
        row = Adw.ActionRow(
            title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(subtitle)
        )
        row.add_suffix(Gtk.Label(label=str(count), css_classes=["dim-label"]))
        return row

    def current_index(self) -> int:
        """Which project or scope the sidebar points at."""
        return self.sidebar[self.sidebar_index][1] if self.sidebar else -1

    def fill_memories(self) -> None:
        """Rebuild the middle list from the selected memory scope."""
        index = self.current_index()
        pool = list(self.scopes[index].memories) if 0 <= index < len(self.scopes) else []
        if self.filter_text:
            needle = self.filter_text.lower()
            pool = [
                m for m in pool if needle in (m.name + m.description + m.kind + m.body).lower()
            ]
        self.shown_memories = pool

        self.selecting = True
        while row := self.convo_list.get_row_at_index(0):
            self.convo_list.remove(row)
        for memory in pool:
            check = summarize.load_memory_check(memory)
            mark = {"current": "✓", "stale": "!"}.get(check.verdict, "?") if check else ""
            subtitle = f"{memory.kind or 'no type'} · {memory.age} · {memory.size_human}"
            if not memory.indexed:
                subtitle += " · not in MEMORY.md"
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(memory.name),
                subtitle=GLib.markup_escape_text(subtitle),
            )
            row.set_title_lines(1)
            if mark:
                row.add_prefix(Gtk.Label(label=mark, css_classes=["accent"]))
            self.convo_list.append(row)

        if not any(m.name == self.open_memory for m in pool):
            self.open_memory = pool[0].name if pool else None
        index = next((i for i, m in enumerate(pool) if m.name == self.open_memory), -1)
        self.selecting = False
        if index >= 0:
            row = self.convo_list.get_row_at_index(index)
            self.convo_list.select_row(row)
            row.grab_focus()
        self.show_detail()

    def opened_memory(self) -> memories_mod.Memory | None:
        """The memory shown on the right."""
        return next((m for m in self.shown_memories if m.name == self.open_memory), None)

    def fill_conversations(self) -> None:
        """Rebuild the middle list, keeping the open conversation selected."""
        index = self.current_index()
        pool = (
            [c for p in self.projects for c in p.conversations]
            if index < 0
            else list(self.projects[index].conversations)
            if index < len(self.projects)
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
            marks = store.marks(
                convo, summarize.load(convo.session_id), summarize.load_review(convo.session_id)
            ).strip()
            subtitle = (
                f"{store.human_age(convo.mtime)} · {convo.messages} messages · "
                f"{store.human_size(convo.size)}"
            )
            if index < 0:
                subtitle = f"{Path(convo.project_path).name} · {subtitle}"
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(convo.display_title),
                subtitle=GLib.markup_escape_text(subtitle),
            )
            row.set_title_lines(1)
            if marks:
                row.add_prefix(Gtk.Label(label=marks, css_classes=["accent"]))
            if convo.state:
                row.set_tooltip_text(store.STATE_WORDS[convo.state])
            self.convo_list.append(row)

        if not any(c.session_id == self.open_id for c in pool):
            self.open_id = pool[0].session_id if pool else None
        index = next((i for i, c in enumerate(pool) if c.session_id == self.open_id), -1)
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
        """Update the right hand pane from whatever is open."""
        if self.mode == "memories":
            self.show_memory_detail()
            return
        convo = self.opened()
        if convo is None:
            self.scratchpad_box.set_visible(False)
            self.tab_box.set_visible(False)
            self.title_label.set_text("")
            self.meta_label.set_text("")
            self.summary_label.set_markup("<i>No conversation selected.</i>")
            self.verdict_label.set_markup("")
            self.review_label.set_markup("")
            return
        self.show_tab(convo)
        self.show_scratchpad(convo)
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
        if convo.state:
            bits.append(store.STATE_WORDS[convo.state])
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

    def show_memory_detail(self) -> None:
        """Update the right hand pane for the open memory."""
        self.scratchpad_box.set_visible(False)
        self.tab_box.set_visible(False)
        memory = self.opened_memory()
        if memory is None:
            self.title_label.set_text("")
            self.meta_label.set_text("")
            self.summary_label.set_markup("<i>No memory selected.</i>")
            self.verdict_label.set_markup("")
            self.review_label.set_markup("")
            return
        bits = [memory.scope, memory.kind or "no type", f"{memory.modified:%Y-%m-%d %H:%M}",
                memory.size_human]
        if not memory.indexed:
            bits.append("not in MEMORY.md")
        if memory.links:
            bits.append("links: " + ", ".join(memory.links))
        self.title_label.set_text(memory.name)
        self.meta_label.set_text(" · ".join(bits) + f"\n{memory.path}")
        if self.busy:
            return
        description = f"<i>{GLib.markup_escape_text(memory.description)}</i>\n\n" if memory.description else ""
        self.summary_label.set_markup(description + to_pango(memory.body))
        check = summarize.load_memory_check(memory)
        if not check:
            self.verdict_label.set_markup("")
            self.review_label.set_markup(
                "<i>Not checked — press Check memory to test it against the project.</i>"
            )
            return
        self.verdict_label.set_markup(f"<b>Verdict: {check.label}</b>")
        body = to_pango("\n".join(f"- {line}" for line in check.lines) or "Nothing to check.")
        if check.note:
            body += f"\n{to_pango(check.note)}"
        self.review_label.set_markup(body)

    # Events -------------------------------------------------------------------------------------

    def on_project_selected(self, _list, row) -> None:
        """Switch the middle list to another project or memory scope."""
        if row is None or self.selecting:
            return
        self.sidebar_index = row.get_index()
        self.fill_middle()

    def on_conversation_selected(self, _list, row) -> None:
        """Open whichever row was clicked, in either mode."""
        if row is None or self.selecting:
            return
        index = row.get_index()
        if self.mode == "memories":
            if 0 <= index < len(self.shown_memories):
                self.open_memory = self.shown_memories[index].name
                self.show_detail()
        elif 0 <= index < len(self.shown):
            self.open_id = self.shown[index].session_id
            self.show_detail()

    def on_search(self, entry) -> None:
        """Filter as the user types."""
        self.filter_text = entry.get_text()
        self.fill_middle()

    def toast(self, message: str) -> None:
        """Show a transient message."""
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))

    # Actions ------------------------------------------------------------------------------------

    def start(self, kind: str, force: bool) -> None:
        """Summarize or check whatever is open, in a background thread."""
        if self.busy:
            return
        if self.mode == "memories":
            memory = self.opened_memory()
            if not memory:
                return
            self.busy = True
            self.check_button.set_sensitive(False)
            self.review_label.set_markup("<i>Checking whether this memory is still true…</i>")

            def memory_worker() -> None:
                try:
                    result = summarize.check_memory(memory, self.cfg)
                    message = f"Checked in {result.seconds}s — {result.label}"
                except summarize.SummaryError as exc:
                    GLib.idle_add(self.finished, f"Failed: {exc}")
                    return
                GLib.idle_add(self.finished, message)

            threading.Thread(target=memory_worker, daemon=True).start()
            return
        convo = self.opened()
        if not convo:
            return
        cached = (
            summarize.load(convo.session_id)
            if kind == "summary"
            else summarize.load_review(convo.session_id)
        )
        if cached and not cached.stale(convo) and not force:
            self.toast("Already done — the button runs it again.")
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
        self.fill_middle()
        return False

    def show_tab(self, convo) -> None:
        """Say whether the editor still has this conversation open, and offer to close it."""
        label = ide.tab_is_open(convo.session_id, convo.project_path, self.cfg)
        self.tab_box.set_visible(bool(label))
        if label:
            self.tab_label_text.set_text(f"Open in the editor as “{label}”")

    def close_editor_tab(self) -> None:
        """Close the open conversation's tab, leaving the conversation itself alone."""
        convo = self.opened()
        if not convo:
            return
        try:
            message = ide.close_conversation_tab(convo.session_id, convo.project_path, self.cfg)
        except ide.BridgeError as exc:
            self.toast(str(exc))
            return
        self.toast(message)
        self.show_tab(convo)

    def show_scratchpad(self, convo) -> None:
        """Measure the open conversation's scratchpad off the UI thread and show what it holds."""
        session_id = convo.session_id
        if self.scratchpad_id == session_id:
            self.scratchpad_box.set_visible(bool(self.scratchpad))
            return
        self.scratchpad_box.set_visible(False)
        self.scratchpad = None

        def worker() -> None:
            pad = leftovers.scratchpad_for(session_id, self.cfg)
            GLib.idle_add(self.scratchpad_measured, session_id, pad)

        threading.Thread(target=worker, daemon=True).start()

    def scratchpad_measured(self, session_id: str, pad) -> bool:
        """Show a measured scratchpad, unless the selection moved on while it was being counted."""
        convo = self.opened()
        if not convo or convo.session_id != session_id:
            return False
        self.scratchpad_id = session_id
        self.scratchpad = pad
        if not pad:
            return False
        self.scratchpad_label.set_text(f"Scratchpad · {pad.files} files · {pad.size_human}")
        self.scratchpad_label.set_tooltip_text(pad.open_path)
        self.scratchpad_box.set_visible(True)
        return False

    def open_scratchpad(self) -> None:
        """Show the open conversation's scratchpad in the desktop file manager."""
        pad = self.scratchpad
        if not pad:
            return
        try:
            leftovers.open_in_file_manager(pad.open_path, self.cfg)
        except OSError as exc:
            self.toast(f"Could not open it: {exc}")
            return
        self.toast("Opened in the file manager")

    def confirm_delete_scratchpad(self) -> None:
        """Ask before deleting the open conversation's scratchpad, which is not trashed."""
        pad = self.scratchpad
        if not pad:
            return
        dialog = Adw.AlertDialog(
            heading="Delete this scratchpad?",
            body=f"{pad.files} files, {pad.size_human}\n\n{pad.open_path}\n\n"
            "It is deleted outright rather than moved to the trash.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect("response", self.delete_scratchpad_answered, pad)
        dialog.present(self)

    def delete_scratchpad_answered(self, _dialog, response: str, pad) -> None:
        """Carry out a confirmed deletion of the scratchpad that was shown."""
        if response != "delete":
            return
        gone = leftovers.remove(pad)
        self.toast(f"Deleted, freeing {pad.size_human}" if gone else "Nothing could be removed")
        self.scratchpad_box.set_visible(False)
        self.scratchpad = None
        self.scratchpad_id = None

    def show_leftovers(self) -> None:
        """Offer the four kinds of leftover for deletion, once they have been measured."""
        dialog = Adw.Dialog(title="Leftovers", content_width=640, content_height=520)
        hint = Gtk.Label(
            label="Measuring what sessions with no conversation left behind…",
            xalign=0,
            wrap=True,
            css_classes=["dim-label"],
        )
        group = Adw.PreferencesGroup()
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=16,
            margin_start=16,
            margin_end=16,
        )
        box.append(hint)
        box.append(group)

        header = Adw.HeaderBar()
        remove = Gtk.Button(label="Delete selected", css_classes=["destructive-action"], sensitive=False)
        header.pack_end(remove)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(Gtk.ScrolledWindow(child=box))
        dialog.set_child(toolbar)
        dialog.present(self)

        boxes: dict[str, Gtk.CheckButton] = {}

        def measured(rows: dict, count: int, total: int) -> bool:
            hint.set_text(
                f"{count} left behind by sessions with no conversation · {store.human_size(total)}"
                if count
                else "Nothing was left behind."
            )
            for kind, row in rows.items():
                check = Gtk.CheckButton(valign=Gtk.Align.CENTER, sensitive=bool(row["count"]))
                check.connect("toggled", lambda *_: remove.set_sensitive(
                    any(b.get_active() for b in boxes.values())
                ))
                boxes[kind] = check
                item = Adw.ActionRow(
                    title=GLib.markup_escape_text(row["label"]),
                    subtitle=GLib.markup_escape_text(f"{row['count']} · {row['help']}"),
                    activatable_widget=check,
                )
                item.add_prefix(check)
                item.add_suffix(Gtk.Label(label=row["size_human"], css_classes=["dim-label"]))
                group.add(item)
            return False

        def worker() -> None:
            items = leftovers.orphans(cfg=self.cfg)
            rows = leftovers.summary(items)
            GLib.idle_add(measured, rows, len(items), sum(item.size for item in items))

        threading.Thread(target=worker, daemon=True).start()

        def on_remove(*_args) -> None:
            kinds = [kind for kind, check in boxes.items() if check.get_active()]
            remove.set_sensitive(False)
            remove.set_label("Deleting…")

            def sweeper() -> None:
                count, freed = leftovers.remove_all(leftovers.orphans(kinds, self.cfg))
                GLib.idle_add(swept, count, freed)

            def swept(count: int, freed: int) -> bool:
                self.toast(f"Removed {count}, freeing {store.human_size(freed)}")
                dialog.close()
                return False

            threading.Thread(target=sweeper, daemon=True).start()

        remove.connect("clicked", on_remove)

    def confirm_delete(self) -> None:
        """Ask before deleting whatever is open."""
        if self.mode == "memories":
            memory = self.opened_memory()
            if not memory:
                return
            dialog = Adw.AlertDialog(
                heading="Delete this memory?",
                body=f"{memory.name}\n\n{memory.path}\n\n{memory.description}\n\n"
                "Its pointer line in MEMORY.md goes with it.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("delete", "Delete")
            dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.connect("response", self.delete_memory_answered, memory)
            dialog.present(self)
            return
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
        label = ide.tab_is_open(convo.session_id, convo.project_path, self.cfg)
        if label:
            body += f"\n\nIts editor tab “{label}” is closed first."
        pid = leftovers.running_session(convo.session_id, self.cfg)
        if pid:
            body += f"\n\nA session is still running it (pid {pid}), and is stopped first."
        elif convo.live:
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
        if ide.tab_is_open(convo.session_id, convo.project_path, self.cfg):
            self.toast(ide.close_tab_quietly(convo.session_id, convo.project_path, self.cfg))
        self.open_id = None

        def worker() -> None:
            ended = leftovers.end_session(convo.session_id, self.cfg)
            where = store.delete(convo, self.cfg)
            summarize.forget(convo.session_id)
            GLib.idle_add(self.delete_finished, where, ended)
            message = store.purge_rebirth(convo)
            if message:
                GLib.idle_add(self.delete_finished, "", message)

        threading.Thread(target=worker, daemon=True).start()

    def delete_finished(self, where: str, note: str) -> None:
        """Say how a deletion went and show the list without what it removed."""
        gone = "" if not where else ("Deleted" if where == "deleted" else f"Moved to {Path(where).name}")
        said = "; ".join(part for part in (gone, note) if part)
        self.toast(said[:1].upper() + said[1:])
        self.reload()

    def delete_memory_answered(self, _dialog, response: str, memory) -> None:
        """Carry out a confirmed deletion of the memory that was open."""
        if response != "delete":
            return
        where = memories_mod.delete(memory, self.cfg)
        summarize.forget_memory_check(memory)
        self.open_memory = None
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
            ("memory_prompt", "Memory check", config_mod.DEFAULT_MEMORY_PROMPT),
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

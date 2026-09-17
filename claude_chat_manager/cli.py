"""Command line entry point: launches a frontend or does the same work headlessly."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from dataclasses import asdict

from . import config as config_mod
from . import ide
from . import leftovers
from . import memories as memories_mod
from . import store, summarize
from . import vscode


def _fail(message: str) -> int:
    """Print an error and return the exit status for it."""
    print(f"claude-chat-manager: {message}", file=sys.stderr)
    return 1


def cmd_list(args, cfg) -> int:
    """Print conversations grouped by project."""
    projects = store.load_projects(cfg, refresh=args.refresh)
    if args.project:
        needle = args.project.lower()
        projects = [p for p in projects if needle in p.name.lower() or needle in p.path.lower()]
    if args.json:
        print(json.dumps([store.project_dict(p) for p in projects], indent=2, default=str))
        return 0
    for project in projects:
        marker = "" if project.exists else "  (working tree gone)"
        print(f"\n{project.name}  —  {project.path}{marker}")
        print(f"  {len(project.conversations)} conversations, {store.human_size(project.size)}")
        for convo in project.conversations:
            if args.search and args.search.lower() not in (
                convo.display_title + convo.last_prompt
            ).lower():
                continue
            flag = "*" if summarize.load(convo.session_id) else " "
            print(
                f"  {flag} {convo.session_id[:8]}  {store.human_age(convo.mtime):>10}"
                f"  {convo.messages:5} msg  {store.human_size(convo.size):>8}  {convo.display_title}"
            )
    totals = store.stats(projects)
    print(
        f"\n{totals['conversations']} conversations in {totals['projects']} projects, "
        f"{store.human_size(totals['bytes'])}"
    )
    return 0


def cmd_summarize(args, cfg) -> int:
    """Summarize one conversation and print the result."""
    projects = store.load_projects(cfg)
    convo = store.find(projects, args.session)
    if not convo:
        return _fail(f"no conversation matching {args.session!r}")
    if not args.force:
        cached = summarize.load(convo.session_id)
        if cached and not cached.stale(convo):
            print(cached.text)
            return 0
    prompt = cfg.summary_prompt
    if args.prompt:
        prompt = args.prompt
    elif args.prompt_file:
        prompt = open(args.prompt_file).read()
    try:
        result = summarize.run(convo, cfg, prompt=prompt)
    except summarize.SummaryError as exc:
        return _fail(str(exc))
    print(result.text)
    return 0


def cmd_review(args, cfg) -> int:
    """Check whether a conversation's outstanding items have since been dealt with."""
    projects = store.load_projects(cfg)
    convo = store.find(projects, args.session)
    if not convo:
        return _fail(f"no conversation matching {args.session!r}")
    if not args.force:
        cached = summarize.load_review(convo.session_id)
        if cached and not cached.stale(convo):
            _print_review(cached)
            return 0
    try:
        result = summarize.review(convo, cfg, prompt=args.prompt or None)
    except summarize.SummaryError as exc:
        return _fail(str(exc))
    _print_review(result)
    return 0


def _print_review(review) -> None:
    """Print a review's findings and its verdict."""
    for line in review.lines:
        print(f"- {line.replace('**', '')}\n")
    if review.note:
        print(f"{review.note}\n")
    print(f"Verdict: {review.label}")


def cmd_trash(args, cfg) -> int:
    """List, restore or empty the trash."""
    entries = store.trashed(cfg)
    if args.empty:
        if not args.yes and entries:
            if input(f"delete {len(entries)} trashed conversations for good? [y/N] ").strip().lower() not in ("y", "yes"):
                print("left alone")
                return 0
        print(f"removed {store.empty_trash(cfg)}")
        return 0
    if args.restore:
        matches = [e for e in entries if e["session_id"].startswith(args.restore)]
        if len(matches) != 1:
            return _fail(f"{len(matches)} trashed conversations match {args.restore!r}")
        try:
            target = store.restore(matches[0], cfg)
        except (FileNotFoundError, FileExistsError) as exc:
            return _fail(f"could not restore: {exc}")
        print(f"restored to {target}")
        return 0
    if not entries:
        print(f"the trash is empty ({store.trash_dir(cfg)})")
        return 0
    for entry in entries:
        print(
            f"  {entry['session_id'][:8]}  {entry['age']:>10}  {entry['size_human']:>8}  "
            f"{Path(entry['project_path']).name}"
        )
    print(f"\n{len(entries)} conversations in {store.trash_dir(cfg)}")
    return 0


def cmd_delete(args, cfg) -> int:
    """Delete one conversation, asking first unless told not to."""
    projects = store.load_projects(cfg)
    convo = store.find(projects, args.session)
    if not convo:
        return _fail(f"no conversation matching {args.session!r}")
    label = ide.tab_is_open(convo.session_id, convo.project_path, cfg)
    print(f"{convo.display_title}\n  {convo.path}\n  {convo.messages} messages, {store.human_size(convo.size)}")
    if label and not args.keep_tab:
        print(f"  its tab {label!r} is open in the editor and will be closed first")
    if not args.yes:
        if input("delete this conversation? [y/N] ").strip().lower() not in ("y", "yes"):
            print("left alone")
            return 0
    if label and not args.keep_tab:
        print(ide.close_tab_quietly(convo.session_id, convo.project_path, cfg))
    destination = store.delete(convo, cfg, purge=args.purge)
    summarize.forget(convo.session_id)
    print("purged" if destination == "deleted" else f"moved to {destination}")
    if args.everything:
        count, freed = leftovers.remove_all(leftovers.for_session(convo.session_id, cfg))
        print(f"removed {count} leftovers, freeing {store.human_size(freed)}")
    return 0


def cmd_prune(args, cfg) -> int:
    """Remove project directories that hold no conversations."""
    empties = store.empty_project_dirs(cfg)
    if not empties:
        print("no empty project directories")
        return 0
    for directory in empties:
        print(directory.name)
    if not args.yes:
        if input(f"remove {len(empties)} empty directories? [y/N] ").strip().lower() not in ("y", "yes"):
            print("left alone")
            return 0
    print(f"removed {len(store.prune_empty(cfg))}")
    return 0


def cmd_resolve_tab(args, cfg) -> int:
    """Print the session id behind an editor tab, which is what the editor button needs."""
    session_id = vscode.session_for_tab(args.label, args.project, cfg)
    if not session_id:
        return _fail(f"no conversation is open in a tab named {args.label!r}")
    if not args.json:
        print(session_id)
        return 0
    convo = store.find(store.load_projects(cfg), session_id)
    print(
        json.dumps(
            {
                "session_id": session_id,
                "title": convo.display_title if convo else args.label,
                "messages": convo.messages if convo else 0,
                "size_human": store.human_size(convo.size) if convo else "",
                "path": convo.path if convo else "",
                "leftovers": [
                    {"kind": item.kind, "size_human": item.size_human, "path": item.path}
                    for item in leftovers.for_session(session_id, cfg)
                ],
            }
        )
    )
    return 0


def cmd_close_tab(args, cfg) -> int:
    """Close a conversation's tab in the editor that has its project open."""
    projects = store.load_projects(cfg)
    convo = store.find(projects, args.session)
    if not convo:
        return _fail(f"no conversation matching {args.session!r}")
    print(convo.display_title)
    try:
        print(f"  {ide.close_conversation_tab(convo.session_id, convo.project_path, cfg)}")
    except ide.BridgeError as exc:
        return _fail(str(exc))
    return 0


def cmd_scratchpad(args, cfg) -> int:
    """Show, open or delete the scratchpad one conversation's session wrote."""
    projects = store.load_projects(cfg)
    convo = store.find(projects, args.session)
    if not convo:
        return _fail(f"no conversation matching {args.session!r}")
    pad = leftovers.scratchpad_for(convo.session_id, cfg)
    if not pad:
        print(f"{convo.display_title}\n  no scratchpad was left for this session")
        return 0
    print(f"{convo.display_title}\n  {pad.open_path}\n  {pad.files} files, {pad.size_human}")
    if args.open:
        leftovers.open_in_file_manager(pad.open_path, cfg)
        print("  opened in the file manager")
    if args.delete:
        if not args.yes:
            if input("delete this scratchpad for good? [y/N] ").strip().lower() not in ("y", "yes"):
                print("left alone")
                return 0
        print("deleted" if leftovers.remove(pad) else "nothing could be removed")
    return 0


# The sweep flags, in the order they are listed, and the kind of leftover each one selects.
SWEEP_FLAGS = {
    "scratchpads": "scratchpad",
    "session_env": "session-env",
    "file_history": "file-history",
    "sessions": "session-record",
}


def cmd_sweep(args, cfg) -> int:
    """Report what past sessions left behind, and delete the kinds that were asked for."""
    chosen = [kind for flag, kind in SWEEP_FLAGS.items() if getattr(args, flag, False)]
    if args.all:
        chosen = list(leftovers.KINDS)
    items = leftovers.orphans(chosen or None, cfg)
    rows = leftovers.summary(items)
    for kind in chosen or list(leftovers.KINDS):
        row = rows[kind]
        print(f"  {row['label']:22} {row['count']:5}  {row['size_human']:>9}  {row['help']}")
    total = sum(item.size for item in items)
    print(f"\n{len(items)} left behind by sessions with no conversation, {store.human_size(total)}")
    if not chosen:
        print("name a kind to remove it: --scratchpads --session-env --file-history --sessions --all")
        return 0
    if not items:
        return 0
    if not args.yes:
        if input(f"delete all {len(items)} for good? [y/N] ").strip().lower() not in ("y", "yes"):
            print("left alone")
            return 0
    count, freed = leftovers.remove_all(items)
    print(f"removed {count}, freeing {store.human_size(freed)}")
    return 0


def cmd_memory(args, cfg) -> int:
    """List, show, check or delete the memory files."""
    scopes = memories_mod.load_scopes(cfg)
    action = args.memory_action or "list"

    if action == "list":
        for scope in scopes:
            marker = "" if scope.exists else "  (project gone)"
            print(f"\n{scope.name}  —  {scope.path}{marker}")
            for memory in scope.memories:
                check = summarize.load_memory_check(memory)
                mark = {"current": "✓", "stale": "!"}.get(check.verdict, "?") if check else " "
                flag = " " if memory.indexed else "u"
                print(
                    f"  {mark}{flag} {memory.age:>10}  {memory.kind[:9]:9}  {memory.name[:44]:46}"
                    f"  {memory.description[:60]}"
                )
        totals = memories_mod.stats(scopes)
        print(
            f"\n{totals['memories']} memories in {totals['scopes']} scopes, "
            f"{store.human_size(totals['bytes'])}"
        )
        return 0

    if action == "health":
        everywhere = memories_mod.all_names(scopes)
        for scope in scopes:
            problems = []
            for memory in scope.unindexed:
                problems.append(f"  not in MEMORY.md: {memory.name}")
            for line in scope.orphan_index_lines:
                problems.append(f"  points at a missing file: {line.strip()}")
            for name, links in scope.dead_links(everywhere).items():
                problems.append(f"  {name} links to missing: {', '.join(links)}")
            if problems:
                print(f"\n{scope.name}")
                print("\n".join(problems))
        totals = memories_mod.stats(scopes)
        print(
            f"\n{totals['unindexed']} unindexed, {totals['orphans']} orphaned index lines"
            if totals["unindexed"] or totals["orphans"]
            else "\nevery memory is indexed and every index line has its file"
        )
        return 0

    memory = memories_mod.find(scopes, args.name or "")
    if not memory:
        return _fail(f"no memory matching {args.name!r}")

    if action == "show":
        print(f"{memory.name}  ({memory.kind}, {memory.scope})")
        print(f"{memory.path}\n")
        print(memory.description + "\n")
        print(memory.body)
        return 0

    if action == "check":
        if not args.force:
            cached = summarize.load_memory_check(memory)
            if cached:
                _print_review(cached)
                return 0
        try:
            result = summarize.check_memory(memory, cfg)
        except summarize.SummaryError as exc:
            return _fail(str(exc))
        _print_review(result)
        return 0

    if action == "delete":
        print(f"{memory.name}\n  {memory.path}\n  {memory.description}")
        if not args.yes:
            if input("delete this memory? [y/N] ").strip().lower() not in ("y", "yes"):
                print("left alone")
                return 0
        where = memories_mod.delete(memory, cfg, purge=args.purge)
        summarize.forget_memory_check(memory)
        print("purged" if where == "deleted" else f"moved to {where}")
        print("its line in MEMORY.md went with it")
        return 0

    return _fail(f"unknown memory action {action!r}")


def cmd_config(args, cfg) -> int:
    """Show the settings, or write the default file so it can be edited."""
    if args.init:
        path = config_mod.save(cfg)
        print(f"wrote {path}")
        return 0
    if args.set_prompt_file:
        cfg.summary_prompt = open(args.set_prompt_file).read().strip()
        print(f"wrote {config_mod.save(cfg)}")
        return 0
    print(json.dumps(asdict(cfg), indent=2))
    print(f"\nfile: {config_mod.config_path()}", file=sys.stderr)
    return 0


def cmd_tui(args, cfg) -> int:
    """Launch the terminal frontend."""
    from .tui import main as tui_main

    return tui_main(cfg)


def cmd_gtk(args, cfg) -> int:
    """Launch the GTK frontend."""
    from .gtkapp import main as gtk_main

    return gtk_main(cfg)


def cmd_web(args, cfg) -> int:
    """Launch the web frontend."""
    from .web import main as web_main

    return web_main(cfg, host=args.host or cfg.web_host, port=args.port or cfg.web_port,
                    open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    """Define every subcommand."""
    parser = argparse.ArgumentParser(
        prog="claude_chat_manager", description="Browse, summarize and delete Claude Code conversations."
    )
    subs = parser.add_subparsers(dest="command")

    tui = subs.add_parser("tui", help="terminal interface (default)")
    tui.set_defaults(func=cmd_tui)

    gtk = subs.add_parser("gtk", help="GTK desktop window")
    gtk.set_defaults(func=cmd_gtk)

    web = subs.add_parser("web", help="local web interface")
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    web.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    web.set_defaults(func=cmd_web)

    listing = subs.add_parser("list", help="print conversations grouped by project")
    listing.add_argument("-p", "--project", help="only projects matching this text")
    listing.add_argument("-s", "--search", help="only conversations matching this text")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--refresh", action="store_true", help="rescan instead of using the cache")
    listing.set_defaults(func=cmd_list)

    summary = subs.add_parser("summarize", help="summarize a conversation with claude")
    summary.add_argument("session", help="session id or unique prefix")
    summary.add_argument("--prompt", help="override the configured prompt")
    summary.add_argument("--prompt-file", help="read the prompt from a file")
    summary.add_argument("-f", "--force", action="store_true", help="ignore a cached summary")
    summary.set_defaults(func=cmd_summarize)

    review = subs.add_parser("review", help="check whether the outstanding items were dealt with")
    review.add_argument("session", help="session id or unique prefix")
    review.add_argument("--prompt", help="override the configured review prompt")
    review.add_argument("-f", "--force", action="store_true", help="ignore a cached review")
    review.set_defaults(func=cmd_review)

    trash = subs.add_parser("trash", help="list, restore or empty the deleted conversations")
    trash.add_argument("--restore", metavar="SESSION", help="put a deleted conversation back")
    trash.add_argument("--empty", action="store_true", help="delete the trash for good")
    trash.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    trash.set_defaults(func=cmd_trash)

    delete = subs.add_parser("delete", help="delete a conversation")
    delete.add_argument("session", help="session id or unique prefix")
    delete.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    delete.add_argument("--purge", action="store_true", help="delete outright instead of trashing")
    delete.add_argument("--keep-tab", action="store_true", help="leave its editor tab open")
    delete.add_argument(
        "--everything",
        action="store_true",
        help="also remove its scratchpad, environment and file history",
    )

    resolving = subs.add_parser("resolve-tab", help="the conversation behind an editor tab")
    resolving.add_argument("label", help="the tab's label, exactly as the editor shows it")
    resolving.add_argument("-p", "--project", required=True, help="the project the tab belongs to")
    resolving.add_argument("--json", action="store_true", help="print what the tab holds too")
    resolving.set_defaults(func=cmd_resolve_tab)
    delete.set_defaults(func=cmd_delete)

    prune = subs.add_parser("prune", help="remove project directories with no conversations left")
    prune.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    prune.set_defaults(func=cmd_prune)

    closing = subs.add_parser("close-tab", help="close a conversation's tab in the editor")
    closing.add_argument("session", help="session id or unique prefix")
    closing.set_defaults(func=cmd_close_tab)

    scratchpad = subs.add_parser("scratchpad", help="show, open or delete a conversation's scratchpad")
    scratchpad.add_argument("session", help="session id or unique prefix")
    scratchpad.add_argument("-o", "--open", action="store_true", help="open it in the file manager")
    scratchpad.add_argument("-d", "--delete", action="store_true", help="delete it for good")
    scratchpad.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    scratchpad.set_defaults(func=cmd_scratchpad)

    sweep = subs.add_parser("sweep", help="remove what sessions with no conversation left behind")
    sweep.add_argument("--scratchpads", action="store_true", help="the temporary files they wrote")
    sweep.add_argument("--session-env", action="store_true", help="their environment directories")
    sweep.add_argument("--file-history", action="store_true", help="their file edit snapshots")
    sweep.add_argument("--sessions", action="store_true", help="registry entries for dead processes")
    sweep.add_argument("--all", action="store_true", help="every kind at once")
    sweep.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    sweep.set_defaults(func=cmd_sweep)

    memory = subs.add_parser("memory", help="list, show, check or delete the memory files")
    memory_subs = memory.add_subparsers(dest="memory_action")
    memory_subs.add_parser("list", help="every memory, grouped by scope")
    memory_subs.add_parser("health", help="unindexed memories, orphaned pointers and dead links")
    show = memory_subs.add_parser("show", help="print one memory")
    show.add_argument("name")
    check = memory_subs.add_parser("check", help="check whether a memory is still true")
    check.add_argument("name")
    check.add_argument("-f", "--force", action="store_true", help="ignore a cached check")
    remove = memory_subs.add_parser("delete", help="delete a memory and its index line")
    remove.add_argument("name")
    remove.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    remove.add_argument("--purge", action="store_true", help="delete outright instead of trashing")
    memory.set_defaults(func=cmd_memory, name=None, force=False, yes=False, purge=False)

    conf = subs.add_parser("config", help="show or initialise the settings")
    conf.add_argument("--init", action="store_true", help="write the default config file")
    conf.add_argument("--set-prompt-file", help="replace the summary prompt with a file's contents")
    conf.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the chosen command, defaulting to the terminal interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config_mod.load()
    if not getattr(args, "func", None):
        return cmd_tui(args, cfg)
    return args.func(args, cfg)

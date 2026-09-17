"""Command line entry point: launches a frontend or does the same work headlessly."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import config as config_mod
from . import store, summarize


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


def cmd_delete(args, cfg) -> int:
    """Delete one conversation, asking first unless told not to."""
    projects = store.load_projects(cfg)
    convo = store.find(projects, args.session)
    if not convo:
        return _fail(f"no conversation matching {args.session!r}")
    print(f"{convo.display_title}\n  {convo.path}\n  {convo.messages} messages, {store.human_size(convo.size)}")
    if not args.yes:
        if input("delete this conversation? [y/N] ").strip().lower() not in ("y", "yes"):
            print("left alone")
            return 0
    destination = store.delete(convo, cfg, purge=args.purge)
    summarize.forget(convo.session_id)
    print("purged" if destination == "deleted" else f"moved to {destination}")
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

    delete = subs.add_parser("delete", help="delete a conversation")
    delete.add_argument("session", help="session id or unique prefix")
    delete.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    delete.add_argument("--purge", action="store_true", help="delete outright instead of trashing")
    delete.set_defaults(func=cmd_delete)

    prune = subs.add_parser("prune", help="remove project directories with no conversations left")
    prune.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    prune.set_defaults(func=cmd_prune)

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

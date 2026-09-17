"""Web frontend: a small JSON API over the store, served with a single page in front of it."""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import config as config_mod
from . import leftovers
from . import memories as memories_mod
from . import render, store, summarize

STATIC = Path(__file__).parent / "static"


class State:
    """Cached scan results shared by every request."""

    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.lock = threading.Lock()
        self.projects: list[store.Project] = []
        self.scopes: list[memories_mod.Scope] = []

    def reload(self, refresh: bool = False) -> list[store.Project]:
        """Rescan the transcripts and the memories."""
        with self.lock:
            self.projects = store.load_projects(self.cfg, refresh=refresh)
            self.scopes = memories_mod.load_scopes(self.cfg)
            return self.projects

    def memory_scopes(self) -> list[memories_mod.Scope]:
        """The memory scopes, scanned once."""
        if not getattr(self, "scopes", None):
            self.scopes = memories_mod.load_scopes(self.cfg)
        return self.scopes

    def find_memory(self, name: str):
        """Look a memory up by name."""
        return memories_mod.find(self.memory_scopes(), name)

    def ensure(self) -> list[store.Project]:
        """Scan once, then reuse the result."""
        return self.projects or self.reload()

    def find(self, session_id: str) -> store.Conversation | None:
        """Look up a conversation by id."""
        return store.find(self.ensure(), session_id)


class Handler(BaseHTTPRequestHandler):
    """Serves the page, its assets, and the JSON API."""

    state: State
    server_version = "ClaudeChatManager"

    def log_message(self, fmt: str, *args) -> None:
        """Keep the console quiet except for errors."""

    # Plumbing -----------------------------------------------------------------------------------

    def send_json(self, payload, status: int = 200) -> None:
        """Write a JSON response."""
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, name: str) -> None:
        """Write one of the static assets."""
        path = (STATIC / name).resolve()
        if not path.is_file() or STATIC.resolve() not in path.parents:
            self.send_error(404)
            return
        body = path.read_bytes()
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> dict:
        """Read a JSON request body."""
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    # Routes -------------------------------------------------------------------------------------

    def do_GET(self) -> None:
        """Serve the page, the assets and the read-only API."""
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self.send_file("index.html")
        elif route.startswith("/static/"):
            self.send_file(route[len("/static/"):])
        elif route == "/api/projects":
            projects = self.state.ensure()
            self.send_json(
                {
                    "projects": [store.project_dict(p) for p in projects],
                    "stats": store.stats(projects),
                    "summaries": {
                        c.session_id: {
                            "text": s.text,
                            "created": s.created,
                            "stale": s.stale(c),
                            "model": s.model,
                        }
                        for p in projects
                        for c in p.conversations
                        if (s := summarize.load(c.session_id))
                    },
                    "reviews": {
                        c.session_id: {
                            "lines": r.lines,
                            "verdict": r.verdict,
                            "label": r.label,
                            "note": r.note,
                            "stale": r.stale(c),
                        }
                        for p in projects
                        for c in p.conversations
                        if (r := summarize.load_review(c.session_id))
                    },
                    "trash": len(store.trashed(self.state.cfg)),
                    "trash_dir": str(store.trash_dir(self.state.cfg)),
                }
            )
        elif route == "/api/memories":
            scopes = self.state.memory_scopes()
            self.send_json(
                {
                    "scopes": [memories_mod.scope_dict(s) for s in scopes],
                    "stats": memories_mod.stats(scopes),
                    "checks": {
                        m.name: {
                            "lines": c.lines,
                            "verdict": c.verdict,
                            "label": c.label,
                            "note": c.note,
                        }
                        for s in scopes
                        for m in s.memories
                        if (c := summarize.load_memory_check(m))
                    },
                }
            )
        elif route == "/api/trash":
            self.send_json(
                {
                    "dir": str(store.trash_dir(self.state.cfg)),
                    "entries": store.trashed(self.state.cfg),
                }
            )
        elif route == "/api/config":
            self.send_json(asdict(self.state.cfg))
        elif route == "/api/leftovers":
            items = leftovers.orphans(cfg=self.state.cfg)
            self.send_json(
                {
                    "rows": list(leftovers.summary(items).values()),
                    "count": len(items),
                    "size_human": store.human_size(sum(item.size for item in items)),
                }
            )
        elif route.startswith("/api/scratchpad/"):
            pad = leftovers.scratchpad_for(route.rsplit("/", 1)[-1], self.state.cfg)
            self.send_json(leftovers.leftover_dict(pad) if pad else {"path": ""})
        elif route.startswith("/api/transcript/"):
            convo = self.state.find(route.rsplit("/", 1)[-1])
            if not convo:
                self.send_error(404)
                return
            self.send_json({"text": render.transcript(convo.path, self.state.cfg)})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        """Handle the actions: rescan, summarize, delete and prompt changes."""
        route = urlparse(self.path).path
        payload = self.body()
        if route == "/api/refresh":
            projects = self.state.reload(refresh=True)
            self.send_json({"ok": True, "stats": store.stats(projects)})
        elif route == "/api/summarize":
            convo = self.state.find(payload.get("session_id", ""))
            if not convo:
                self.send_json({"error": "no such conversation"}, 404)
                return
            cached = summarize.load(convo.session_id)
            if cached and not cached.stale(convo) and not payload.get("force"):
                self.send_json({"text": cached.text, "cached": True})
                return
            try:
                result = summarize.run(
                    convo, self.state.cfg, prompt=payload.get("prompt") or None
                )
            except summarize.SummaryError as exc:
                self.send_json({"error": str(exc)}, 500)
                return
            self.send_json({"text": result.text, "seconds": result.seconds, "cached": False})
        elif route == "/api/review":
            convo = self.state.find(payload.get("session_id", ""))
            if not convo:
                self.send_json({"error": "no such conversation"}, 404)
                return
            cached = summarize.load_review(convo.session_id)
            if cached and not cached.stale(convo) and not payload.get("force"):
                self.send_json(
                    {
                        "lines": cached.lines,
                        "verdict": cached.verdict,
                        "label": cached.label,
                        "note": cached.note,
                        "cached": True,
                    }
                )
                return
            try:
                result = summarize.review(convo, self.state.cfg)
            except summarize.SummaryError as exc:
                self.send_json({"error": str(exc)}, 500)
                return
            self.send_json(
                {
                    "lines": result.lines,
                    "verdict": result.verdict,
                    "label": result.label,
                    "note": result.note,
                    "seconds": result.seconds,
                    "cached": False,
                }
            )
        elif route == "/api/memory-check":
            memory = self.state.find_memory(payload.get("name", ""))
            if not memory:
                self.send_json({"error": "no such memory"}, 404)
                return
            cached = summarize.load_memory_check(memory)
            if cached and not payload.get("force"):
                self.send_json(
                    {
                        "lines": cached.lines,
                        "verdict": cached.verdict,
                        "label": cached.label,
                        "note": cached.note,
                        "cached": True,
                    }
                )
                return
            try:
                result = summarize.check_memory(memory, self.state.cfg)
            except summarize.SummaryError as exc:
                self.send_json({"error": str(exc)}, 500)
                return
            self.send_json(
                {
                    "lines": result.lines,
                    "verdict": result.verdict,
                    "label": result.label,
                    "note": result.note,
                    "seconds": result.seconds,
                    "cached": False,
                }
            )
        elif route == "/api/memory-delete":
            memory = self.state.find_memory(payload.get("name", ""))
            if not memory:
                self.send_json({"error": "no such memory"}, 404)
                return
            where = memories_mod.delete(memory, self.state.cfg, purge=bool(payload.get("purge")))
            summarize.forget_memory_check(memory)
            self.state.reload()
            self.send_json({"ok": True, "where": where})
        elif route == "/api/delete":
            convo = self.state.find(payload.get("session_id", ""))
            if not convo:
                self.send_json({"error": "no such conversation"}, 404)
                return
            where = store.delete(convo, self.state.cfg, purge=bool(payload.get("purge")))
            summarize.forget(convo.session_id)
            self.state.reload()
            self.send_json({"ok": True, "where": where})
        elif route in ("/api/scratchpad-open", "/api/scratchpad-delete"):
            convo = self.state.find(payload.get("session_id", ""))
            pad = leftovers.scratchpad_for(convo.session_id, self.state.cfg) if convo else None
            if not pad:
                self.send_json({"error": "no scratchpad for that conversation"}, 404)
                return
            if route.endswith("open"):
                leftovers.open_in_file_manager(pad.open_path, self.state.cfg)
                self.send_json({"ok": True, "path": pad.open_path})
                return
            self.send_json({"ok": leftovers.remove(pad), "size_human": pad.size_human})
        elif route == "/api/sweep":
            kinds = [k for k in (payload.get("kinds") or []) if k in leftovers.KINDS]
            if not kinds:
                self.send_json({"error": "no kinds chosen"}, 400)
                return
            count, freed = leftovers.remove_all(leftovers.orphans(kinds, self.state.cfg))
            self.send_json({"ok": True, "count": count, "freed_human": store.human_size(freed)})
        elif route == "/api/prompt":
            which = payload.get("which") or "summary"
            prompt = (payload.get("prompt") or "").strip()
            if which == "review":
                self.state.cfg.review_prompt = prompt or config_mod.DEFAULT_REVIEW_PROMPT
            else:
                self.state.cfg.summary_prompt = prompt or config_mod.DEFAULT_SUMMARY_PROMPT
            config_mod.save(self.state.cfg)
            self.send_json(
                {
                    "summary_prompt": self.state.cfg.summary_prompt,
                    "review_prompt": self.state.cfg.review_prompt,
                }
            )
        else:
            self.send_error(404)


def main(
    cfg: config_mod.Config | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Serve the web frontend until interrupted."""
    cfg = cfg or config_mod.load()
    handler = type("BoundHandler", (Handler,), {"state": State(cfg)})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"claude-chat-manager is serving {url}  (ctrl-c to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: this address is reachable from the network and there is no authentication.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0

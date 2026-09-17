"use strict";

const state = { projects: [], summaries: {}, reviews: {}, stats: {}, trash: 0, trashDir: "",
                scopes: [], checks: {}, memoryStats: {},
                mode: "conversations", project: null, scope: null, session: null, memory: null,
                filter: "", promptTab: "summary" };

const $ = (id) => document.getElementById(id);

const STATE_WORDS = {
    live: "a session is writing to this conversation now",
    archived: "archived out of the editor's session list",
};

function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(node.timer);
    node.timer = setTimeout(() => node.classList.remove("show"), 3500);
}

function escapeHtml(text) {
    return text.replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

/* Renders the small slice of markdown that summaries and memories use. */
function markdown(text) {
    const out = [];
    let list = false;
    let paragraph = [];
    const inline = (s) => s
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    /* Source lines are hard wrapped, so a paragraph is rejoined and the browser wraps it. */
    const flush = () => {
        if (paragraph.length) out.push(`<p>${inline(paragraph.join(" "))}</p>`);
        paragraph = [];
    };
    const closeList = () => {
        if (list) out.push("</ul>");
        list = false;
    };

    for (const raw of escapeHtml(text).split("\n")) {
        const line = raw.trim();
        if (!line) { flush(); closeList(); continue; }
        if (/^[-*]\s+/.test(line)) {
            flush();
            if (!list) { out.push("<ul>"); list = true; }
            out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
            continue;
        }
        /* A wrapped continuation of the bullet above belongs to that bullet, not to a new paragraph. */
        if (list && !paragraph.length && out.length && out[out.length - 1].endsWith("</li>")) {
            out[out.length - 1] = out[out.length - 1].replace(/<\/li>$/, " " + inline(line) + "</li>");
            continue;
        }
        closeList();
        if (/^#{1,6}\s/.test(line)) { flush(); out.push(`<h3>${inline(line.replace(/^#+\s*/, ""))}</h3>`); }
        else if (/^\*\*[^*]+\*\*$/.test(line)) { flush(); out.push(`<h3>${inline(line).replace(/<\/?strong>/g, "")}</h3>`); }
        else paragraph.push(line);
    }
    flush();
    closeList();
    return out.join("\n");
}

async function api(path, options) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `request failed (${response.status})`);
    return data;
}

async function load(refresh) {
    if (refresh) await api("/api/refresh", { method: "POST" });
    const memoryData = await api("/api/memories");
    state.scopes = memoryData.scopes;
    state.checks = memoryData.checks || {};
    state.memoryStats = memoryData.stats || {};
    const data = await api("/api/projects");
    state.projects = data.projects;
    state.summaries = data.summaries;
    state.reviews = data.reviews || {};
    state.stats = data.stats;
    state.trash = data.trash || 0;
    state.trashDir = data.trash_dir || "";
    $("showtrash").textContent = state.trash ? `Trash (${state.trash})` : "Trash";
    $("stats").textContent =
        `${data.stats.conversations} conversations · ${state.memoryStats.memories || 0} memories`;
    drawSidebar();
    drawMiddle();
}

function conversations() {
    const pool = state.project === null
        ? state.projects.flatMap((p) => p.conversations)
        : (state.projects.find((p) => p.slug === state.project)?.conversations ?? []);
    const needle = state.filter.toLowerCase();
    const hits = needle
        ? pool.filter((c) => `${c.display_title} ${c.last_prompt} ${c.project_path}`.toLowerCase().includes(needle))
        : pool.slice();
    return hits.sort((a, b) => b.mtime - a.mtime);
}

function drawSidebar() {
    const root = $("projects");
    root.innerHTML = "";

    const heading = (text) => {
        const node = document.createElement("div");
        node.className = "group";
        node.textContent = text;
        root.append(node);
    };
    const entry = (title, sub, count, active, onclick) => {
        const row = document.createElement("div");
        row.className = "row" + (active ? " active" : "");
        row.innerHTML = `<div class="name"><span class="title">${escapeHtml(title)}</span>
            <span class="count">${count}</span></div>
            ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}`;
        row.onclick = onclick;
        root.append(row);
    };

    heading("Conversations");
    entry("All projects", "", state.stats.conversations ?? 0,
        state.mode === "conversations" && state.project === null,
        () => { state.mode = "conversations"; state.project = null; drawSidebar(); drawMiddle(); });
    for (const project of state.projects) {
        entry(project.name, project.path + (project.exists ? "" : " · gone"),
            project.conversations.length,
            state.mode === "conversations" && state.project === project.slug,
            () => { state.mode = "conversations"; state.project = project.slug; drawSidebar(); drawMiddle(); });
    }

    heading("Memories");
    for (const scope of state.scopes) {
        entry(scope.name, scope.path, scope.memories.length,
            state.mode === "memories" && state.scope === scope.slug,
            () => { state.mode = "memories"; state.scope = scope.slug; drawSidebar(); drawMiddle(); });
    }
}

function memories() {
    const scope = state.scopes.find((s) => s.slug === state.scope) || state.scopes[0];
    if (!scope) return [];
    const needle = state.filter.toLowerCase();
    return needle
        ? scope.memories.filter((m) =>
            `${m.name} ${m.description} ${m.kind} ${m.body}`.toLowerCase().includes(needle))
        : scope.memories.slice();
}

let drawMiddle = function () {
    $("search").placeholder = state.mode === "memories" ? "Filter memories" : "Filter conversations";
    if (state.mode === "memories") drawMemories(); else drawConversations();
};

function drawMemories() {
    const root = $("conversations");
    root.innerHTML = "";
    const rows = memories();
    if (!rows.length) {
        root.innerHTML = '<p class="empty" style="padding:16px">Nothing here.</p>';
        drawMemoryDetail();
        return;
    }
    if (!rows.some((m) => m.name === state.memory)) state.memory = rows[0].name;
    let openRow = null;
    for (const memory of rows) {
        const check = state.checks[memory.name];
        const mark = (check ? ({ current: "✓", stale: "!" }[check.verdict] || "?") : "")
            + (memory.indexed ? "" : "u");
        const row = document.createElement("div");
        row.className = "row" + (memory.name === state.memory ? " active" : "");
        row.innerHTML = `<div class="name"><span class="title">${escapeHtml(memory.name)}</span>
            <span class="mark">${mark}</span></div>
            <div class="sub">${escapeHtml(memory.kind || "no type")} · ${memory.age} · ${memory.size_human}
            ${memory.indexed ? "" : " · not in MEMORY.md"}</div>`;
        row.onclick = () => { state.memory = memory.name; drawMemories(); };
        if (memory.name === state.memory) openRow = row;
        root.append(row);
    }
    if (openRow) openRow.scrollIntoView({ block: "nearest" });
    drawMemoryDetail();
}

function drawMemoryDetail() {
    const memory = memories().find((m) => m.name === state.memory);
    const root = $("detail");
    if (!memory) {
        root.innerHTML = '<p class="empty">Select a memory.</p>';
        return;
    }
    const check = state.checks[memory.name];
    const bits = [memory.scope, memory.kind || "no type", memory.modified, memory.size_human];
    if (!memory.indexed) bits.push("not in MEMORY.md");
    if (memory.links.length) bits.push("links: " + memory.links.join(", "));
    const verdictClass = check
        ? (check.verdict === "current" ? "safe" : check.verdict === "stale" ? "keep" : "")
        : "";
    root.innerHTML = `
        <h2>${escapeHtml(memory.name)}</h2>
        <div id="meta">${escapeHtml(bits.join(" · "))}<br>${escapeHtml(memory.path)}</div>
        <div id="actions">
            <button id="domemorycheck" class="primary">${check ? "Check again" : "Is it still true?"}</button>
            <button id="domemorydelete" class="danger">Delete</button>
        </div>
        <div id="summary">${markdown(memory.description ? "*" + memory.description + "*\n\n" + memory.body : memory.body)}</div>
        <div id="review">
            <h3>Is it still true?</h3>
            ${check
                ? `<p><span class="verdict ${verdictClass}">${check.label}</span></p>
                   ${check.lines.length
                        ? "<ul>" + check.lines.map((line) => `<li>${markdownInline(line)}</li>`).join("") + "</ul>"
                        : '<p class="empty">Nothing in it could be checked.</p>'}
                   ${check.note ? `<p>${markdownInline(check.note)}</p>` : ""}`
                : '<p class="empty">Not checked yet — this reads the project to see whether the memory still holds.</p>'}
        </div>`;
    $("domemorycheck").onclick = () => checkMemory(memory, Boolean(check));
    $("domemorydelete").onclick = () => askDeleteMemory(memory);
}

async function checkMemory(memory, force) {
    const button = $("domemorycheck");
    button.disabled = true;
    button.textContent = "Checking…";
    try {
        const data = await api("/api/memory-check", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: memory.name, force }),
        });
        state.checks[memory.name] = data;
        toast(data.cached ? "Showed the cached check" : `Checked in ${data.seconds}s — ${data.label}`);
        drawMemories();
    } catch (error) {
        toast(String(error.message || error));
        drawMemoryDetail();
    }
}

function askDeleteMemory(memory) {
    $("deletedetail").textContent =
        `${memory.name} — ${memory.description || "no description"}. Its line in MEMORY.md goes with it.`;
    const dialog = $("deletedialog");
    dialog.showModal();
    $("deleteok").onclick = async () => {
        dialog.close();
        try {
            await api("/api/memory-delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: memory.name }),
            });
            toast("Deleted, and its index line with it");
            state.memory = null;
            await load(false);
        } catch (error) {
            toast(String(error.message || error));
        }
    };
}

function drawConversations() {
    const root = $("conversations");
    root.innerHTML = "";
    const rows = conversations();
    if (!rows.length) {
        root.innerHTML = '<p class="empty" style="padding:16px">Nothing here.</p>';
        drawDetail();
        return;
    }
    if (!rows.some((c) => c.session_id === state.session)) state.session = rows[0].session_id;
    let openRow = null;
    for (const convo of rows) {
        const summary = state.summaries[convo.session_id];
        const review = state.reviews[convo.session_id];
        const mark = (summary ? (summary.stale ? "~" : "✓") : "")
            + (review ? ({ "safe-to-delete": "✔", keep: "!", unclear: "?" }[review.verdict] || "?") : "")
            + ({ live: "●", archived: "▣" }[convo.state] || "");
        const row = document.createElement("div");
        row.className = "row" + (convo.session_id === state.session ? " active" : "");
        row.innerHTML = `<div class="name"><span class="title">${escapeHtml(convo.display_title)}</span>
            <span class="mark" title="${STATE_WORDS[convo.state] || ""}">${mark}</span></div>
            <div class="sub">${state.project === null ? escapeHtml(convo.project_path.split("/").pop()) + " · " : ""}
            ${convo.age} · ${convo.messages} messages · ${convo.size_human}</div>`;
        row.onclick = () => { state.session = convo.session_id; drawConversations(); };
        if (convo.session_id === state.session) openRow = row;
        root.append(row);
    }
    if (openRow) openRow.scrollIntoView({ block: "nearest" });
    drawDetail();
}

function current() {
    return conversations().find((c) => c.session_id === state.session) ?? null;
}

function drawDetail() {
    const convo = current();
    const root = $("detail");
    if (!convo) {
        root.innerHTML = '<p class="empty">Select a conversation.</p>';
        return;
    }
    const summary = state.summaries[convo.session_id];
    const review = state.reviews[convo.session_id];
    const tokens = convo.input_tokens + convo.output_tokens;
    const bits = [convo.project_path, convo.modified, `${convo.messages} messages`,
        `${convo.tool_calls} tool calls`, convo.size_human];
    if (tokens) bits.push(`${tokens.toLocaleString()} tokens`);
    if (convo.git_branch) bits.push(convo.git_branch);
    if (convo.state) bits.push(STATE_WORDS[convo.state]);
    const verdictClass = review
        ? (review.verdict === "safe-to-delete" ? "safe" : review.verdict === "keep" ? "keep" : "")
        : "";
    root.innerHTML = `
        <h2>${escapeHtml(convo.display_title)}</h2>
        <div id="meta">${escapeHtml(bits.join(" · "))}<br>${convo.session_id}</div>
        <div id="actions">
            <button id="dosummarize" class="primary">${summary ? "Re-summarize" : "Summarize"}</button>
            <button id="docheck">${review ? "Check again" : "Check outstanding items"}</button>
            <button id="dodelete" class="danger">Delete</button>
        </div>
        <div id="summary">${summary
            ? markdown(summary.text) + (summary.stale
                ? '<p class="empty">This summary predates the newest messages.</p>' : "")
            : '<p class="empty">No summary yet.</p>'}</div>
        <div id="review">
            <h3>Outstanding items check</h3>
            ${review
                ? `<p><span class="verdict ${verdictClass}">${review.label}</span></p>
                   ${review.lines.length
                        ? "<ul>" + review.lines.map((line) => `<li>${markdownInline(line)}</li>`).join("") + "</ul>"
                        : '<p class="empty">No outstanding items were listed.</p>'}
                   ${review.note ? `<p>${markdownInline(review.note)}</p>` : ""}
                   ${review.stale ? '<p class="empty">This check predates the newest messages.</p>' : ""}`
                : '<p class="empty">Not checked yet — this reads the project to see whether the outstanding items were dealt with.</p>'}
        </div>`;
    $("dosummarize").onclick = () => summarize(convo, Boolean(summary));
    $("docheck").onclick = () => check(convo, Boolean(review));
    $("dodelete").onclick = () => askDelete(convo);
}

/* Bold, code and italics inside one line, without the block handling. */
function markdownInline(text) {
    return escapeHtml(text)
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/`([^`]+)`/g, "<code>$1</code>");
}

async function check(convo, force) {
    const button = $("docheck");
    button.disabled = true;
    button.textContent = "Checking…";
    $("review").innerHTML = `<h3>Outstanding items check</h3>
        <p class="empty">Reading ${escapeHtml(convo.project_path)} to see what was dealt with…</p>`;
    try {
        const data = await api("/api/review", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: convo.session_id, force }),
        });
        state.reviews[convo.session_id] = { ...data, stale: false };
        toast(data.cached ? "Showed the cached check" : `Checked in ${data.seconds}s — ${data.label}`);
        drawConversations();
    } catch (error) {
        toast(String(error.message || error));
        drawDetail();
    }
}

async function summarize(convo, force) {
    const button = $("dosummarize");
    button.disabled = true;
    button.textContent = "Summarizing…";
    $("summary").innerHTML = '<p class="empty">Running claude on the transcript…</p>';
    try {
        const data = await api("/api/summarize", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: convo.session_id, force }),
        });
        state.summaries[convo.session_id] = { text: data.text, stale: false };
        toast(data.cached ? "Showed the cached summary" : `Summarized in ${data.seconds}s`);
        drawConversations();
    } catch (error) {
        toast(String(error.message || error));
        drawDetail();
    }
}

function askDelete(convo) {
    $("deletedetail").textContent =
        `${convo.display_title} — ${convo.messages} messages, ${convo.size_human}.`;
    const dialog = $("deletedialog");
    dialog.showModal();
    $("deleteok").onclick = async () => {
        dialog.close();
        try {
            const data = await api("/api/delete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: convo.session_id }),
            });
            toast(data.where === "deleted" ? "Deleted" : "Moved to the trash");
            state.session = null;
            await load(false);
        } catch (error) {
            toast(String(error.message || error));
        }
    };
}

$("deletecancel").onclick = () => $("deletedialog").close();
$("search").oninput = (event) => { state.filter = event.target.value; drawMiddle(); };
$("rescan").onclick = async () => { toast("Rescanning…"); await load(true); toast("Rescanned"); };

async function openPrompt(which) {
    const cfg = await api("/api/config");
    state.promptTab = which;
    $("prompttext").value = { review: cfg.review_prompt, memory: cfg.memory_prompt }[which]
        || cfg.summary_prompt;
    $("tabsummary").className = which === "summary" ? "primary" : "";
    $("tabreview").className = which === "review" ? "primary" : "";
    $("tabmemory").className = which === "memory" ? "primary" : "";
    $("prompthint").innerHTML = {
        review: 'Sent to <code>claude -p</code> with the project added and read-only tools, and must return the JSON the app parses.',
        memory: 'Sent to <code>claude -p</code> with the directories the memory names added, and must return the same JSON shape.',
    }[which] || 'Sent to <code>claude -p</code> with the rendered transcript on stdin.';
}

$("editprompt").onclick = async () => {
    await openPrompt("summary");
    $("promptdialog").showModal();
};
$("tabsummary").onclick = () => openPrompt("summary");
$("tabreview").onclick = () => openPrompt("review");
$("tabmemory").onclick = () => openPrompt("memory");

$("showtrash").onclick = async () => {
    const data = await api("/api/trash");
    $("trashpath").textContent = data.dir;
    $("trashlist").innerHTML = data.entries.length
        ? data.entries.map((e) => `<div class="row"><div class="name">
             <span class="title">${escapeHtml(e.project_path.split("/").pop() || e.project_slug)}</span>
             <span class="count">${e.size_human}</span></div>
             <div class="sub">${e.session_id} · deleted ${e.age}</div></div>`).join("")
        : '<p class="empty">Nothing in the trash.</p>';
    $("trashdialog").showModal();
};
$("trashclose").onclick = () => $("trashdialog").close();
$("promptcancel").onclick = () => $("promptdialog").close();
$("promptdefault").onclick = async () => {
    await api("/api/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ which: state.promptTab, prompt: "" }),
    });
    await openPrompt(state.promptTab);
};
$("promptsave").onclick = async () => {
    await api("/api/prompt", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ which: state.promptTab, prompt: $("prompttext").value }),
    });
    $("promptdialog").close();
    toast("Prompt saved");
};

/* The address bar remembers which half of the sidebar you were in, so a reload comes back to it. */
function readHash() {
    const [mode, slug] = decodeURIComponent(location.hash.replace(/^#/, "")).split("/");
    if (mode === "memories") {
        state.mode = "memories";
        if (slug) state.scope = slug;
    } else if (mode === "conversations") {
        state.mode = "conversations";
        state.project = slug || null;
    }
}

function writeHash() {
    const slug = state.mode === "memories" ? state.scope : state.project;
    const next = `#${state.mode}${slug ? "/" + slug : ""}`;
    if (location.hash !== next) history.replaceState(null, "", next);
}

const drawMiddleInner = drawMiddle;
drawMiddle = function () {
    writeHash();
    drawMiddleInner();
};

window.addEventListener("hashchange", () => { readHash(); drawSidebar(); drawMiddle(); });

readHash();
load(false).catch((error) => toast(String(error.message || error)));

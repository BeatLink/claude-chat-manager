"use strict";

const state = { projects: [], summaries: {}, reviews: {}, stats: {}, trash: 0, trashDir: "",
                project: null, session: null, filter: "", promptTab: "summary" };

const $ = (id) => document.getElementById(id);

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

/* Renders the small slice of markdown that summaries use. */
function markdown(text) {
    const out = [];
    let list = false;
    for (const raw of escapeHtml(text).split("\n")) {
        const line = raw.trim();
        const inline = (s) => s
            .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
            .replace(/`([^`]+)`/g, "<code>$1</code>")
            .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>");
        if (/^[-*]\s+/.test(line)) {
            if (!list) { out.push("<ul>"); list = true; }
            out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
            continue;
        }
        if (list) { out.push("</ul>"); list = false; }
        if (!line) continue;
        if (/^#{1,6}\s/.test(line)) out.push(`<h3>${inline(line.replace(/^#+\s*/, ""))}</h3>`);
        else if (/^\*\*[^*]+\*\*$/.test(line)) out.push(`<h3>${inline(line).replace(/<\/?strong>/g, "")}</h3>`);
        else out.push(`<p>${inline(line)}</p>`);
    }
    if (list) out.push("</ul>");
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
    const data = await api("/api/projects");
    state.projects = data.projects;
    state.summaries = data.summaries;
    state.reviews = data.reviews || {};
    state.stats = data.stats;
    state.trash = data.trash || 0;
    state.trashDir = data.trash_dir || "";
    $("showtrash").textContent = state.trash ? `Trash (${state.trash})` : "Trash";
    $("stats").textContent =
        `${data.stats.conversations} conversations · ${data.stats.projects} projects`;
    drawProjects();
    drawConversations();
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

function drawProjects() {
    const root = $("projects");
    root.innerHTML = "";
    const all = document.createElement("div");
    all.className = "row" + (state.project === null ? " active" : "");
    all.innerHTML = `<div class="name"><span class="title">All projects</span>
        <span class="count">${state.stats.conversations ?? 0}</span></div>`;
    all.onclick = () => { state.project = null; drawProjects(); drawConversations(); };
    root.append(all);

    for (const project of state.projects) {
        const row = document.createElement("div");
        row.className = "row" + (state.project === project.slug ? " active" : "");
        row.innerHTML = `<div class="name"><span class="title">${escapeHtml(project.name)}</span>
            <span class="count">${project.conversations.length}</span></div>
            <div class="sub">${escapeHtml(project.path)}${project.exists ? "" : " · gone"}</div>`;
        row.onclick = () => { state.project = project.slug; drawProjects(); drawConversations(); };
        root.append(row);
    }
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
        const marks = (summary ? (summary.stale ? "~" : "✓") : "")
            + (review ? ({ "safe-to-delete": "✔", keep: "!", unclear: "?" }[review.verdict] || "?") : "");
        const mark = marks;
        const row = document.createElement("div");
        row.className = "row" + (convo.session_id === state.session ? " active" : "");
        row.innerHTML = `<div class="name"><span class="title">${escapeHtml(convo.display_title)}</span>
            <span class="mark">${mark}</span></div>
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
$("search").oninput = (event) => { state.filter = event.target.value; drawConversations(); };
$("rescan").onclick = async () => { toast("Rescanning…"); await load(true); toast("Rescanned"); };

async function openPrompt(which) {
    const cfg = await api("/api/config");
    state.promptTab = which;
    $("prompttext").value = which === "review" ? cfg.review_prompt : cfg.summary_prompt;
    $("tabsummary").className = which === "summary" ? "primary" : "";
    $("tabreview").className = which === "review" ? "primary" : "";
    $("prompthint").innerHTML = which === "review"
        ? 'Sent to <code>claude -p</code> in the project directory, with read-only tools, and must return the JSON the app parses.'
        : 'Sent to <code>claude -p</code> with the rendered transcript on stdin.';
}

$("editprompt").onclick = async () => {
    await openPrompt("summary");
    $("promptdialog").showModal();
};
$("tabsummary").onclick = () => openPrompt("summary");
$("tabreview").onclick = () => openPrompt("review");

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

load(false).catch((error) => toast(String(error.message || error)));

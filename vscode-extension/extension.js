/* A button on a Claude Code conversation tab that closes it and deletes everything behind it.
   The deleting is left to claude-chat-manager, so the editor and the tool agree on what to remove. */

const { execFile } = require("node:child_process");
const vscode = require("vscode");

const PANEL_VIEW_TYPE = "claudeVSCodePanel";
/* A conversation with no title yet is labelled this, which names no particular one. */
const UNTITLED_LABEL = "Claude Code";

/* Run claude-chat-manager and hand back what it printed. */
function run(settings, args) {
    return new Promise((resolve, reject) => {
        execFile(settings.get("command", "ccm"), args, { timeout: 120000 }, (error, stdout, stderr) => {
            if (error) {
                reject(new Error((stderr || stdout || error.message).trim()));
                return;
            }
            resolve(stdout.trim());
        });
    });
}

/* The conversation tab the command should act on, which is the active one. */
function activeConversationTab() {
    const tab = vscode.window.tabGroups.activeTabGroup?.activeTab;
    const input = tab?.input;
    if (!tab || !input || input.viewType === undefined) return undefined;
    /* The editor prefixes a webview's view type, so the extension's own id is matched loosely. */
    return String(input.viewType).endsWith(PANEL_VIEW_TYPE) ? tab : undefined;
}

/* Which project folder the tab belongs to, since conversations are stored per working tree. */
function projectFolder() {
    const folders = vscode.workspace.workspaceFolders;
    return folders && folders.length ? folders[0].uri.fsPath : undefined;
}

/* One line naming everything that is about to go. */
function describe(details) {
    const parts = [`${details.messages} messages`, details.size_human].filter(Boolean);
    for (const item of details.leftovers || []) {
        parts.push(`${item.kind} ${item.size_human}`);
    }
    return parts.join(" · ");
}

async function deleteConversation() {
    const settings = vscode.workspace.getConfiguration("claudeChatManager");
    const tab = activeConversationTab();
    if (!tab) {
        vscode.window.showWarningMessage("Open a Claude Code conversation tab first.");
        return;
    }
    const project = projectFolder();
    if (!project) {
        vscode.window.showWarningMessage("This window has no folder open, so there is no project to look in.");
        return;
    }

    if (tab.label.trim() === UNTITLED_LABEL) {
        vscode.window.showWarningMessage(
            "This conversation has no title yet, so there is nothing to identify it by. Send a message first.",
        );
        return;
    }

    let details;
    try {
        details = JSON.parse(await run(settings, ["resolve-tab", tab.label, "--project", project, "--json"]));
    } catch (error) {
        vscode.window.showErrorMessage(`Could not work out which conversation this tab is: ${error.message}`);
        return;
    }

    const leftovers = settings.get("deleteLeftovers", true);
    const purge = settings.get("purge", false);
    const fate = purge ? "deleted outright" : "moved to the claude-chat-manager trash";
    const answer = await vscode.window.showWarningMessage(
        `Delete “${details.title}”?`,
        {
            modal: true,
            detail:
                `${describe(details)}\n\nThe tab closes and the transcript is ${fate}.` +
                (leftovers ? "\nIts scratchpad, session environment and file history are deleted outright." : ""),
        },
        "Delete",
    );
    if (answer !== "Delete") return;

    /* The tab goes first, so the session stops writing to a transcript that is about to be removed. */
    await vscode.window.tabGroups.close(tab);

    const args = ["delete", details.session_id, "--yes", "--keep-tab"];
    if (purge) args.push("--purge");
    if (leftovers) args.push("--everything");
    try {
        await run(settings, args);
    } catch (error) {
        vscode.window.showErrorMessage(`Closed the tab, but deleting failed: ${error.message}`);
        return;
    }
    vscode.window.showInformationMessage(`Deleted “${details.title}”.`);
}

function activate(context) {
    context.subscriptions.push(
        vscode.commands.registerCommand("claudeChatManager.deleteConversation", deleteConversation),
    );
}

function deactivate() {}

module.exports = { activate, deactivate };

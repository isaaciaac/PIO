import * as vscode from "vscode";
import * as path from "path";
import { VibeRunError, runVibe, runVibeCapture } from "./vibeRunner";

type ChatRole = "user" | "assistant" | "system";
type ChatMessage = {
  id: string;
  role: ChatRole;
  title?: string;
  text: string;
  ts: number;
};

type EnvOverridesProvider = () => Promise<NodeJS.ProcessEnv>;

function newId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function lastNonEmptyLine(text: string): string | undefined {
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 0);
  return lines.length ? lines[lines.length - 1] : undefined;
}

async function readTextFile(uri: vscode.Uri): Promise<string> {
  const buf = await vscode.workspace.fs.readFile(uri);
  return Buffer.from(buf).toString("utf-8");
}

async function exists(uri: vscode.Uri): Promise<boolean> {
  try {
    await vscode.workspace.fs.stat(uri);
    return true;
  } catch {
    return false;
  }
}

function extractCheckpoints(payload: any): any[] {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.checkpoints)) return payload.checkpoints;
  return [];
}

async function readCheckpoint(workspaceRoot: string, id: string): Promise<any | undefined> {
  const cpPath = vscode.Uri.file(path.join(workspaceRoot, ".vibe", "checkpoints.json"));
  try {
    const raw = await readTextFile(cpPath);
    const json = JSON.parse(raw);
    const items = extractCheckpoints(json);
    return items.find((c: any) => String(c.id) === id);
  } catch {
    return undefined;
  }
}

type LedgerEvent = { id?: string; ts?: string; agent?: string; type?: string; summary?: string };

async function readLedgerEventsSince(workspaceRoot: string, startLine: number): Promise<LedgerEvent[]> {
  const ledgerPath = vscode.Uri.file(path.join(workspaceRoot, ".vibe", "ledger.jsonl"));
  try {
    const raw = await readTextFile(ledgerPath);
    const lines = raw.split(/\r?\n/);
    const slice = lines.slice(startLine).filter((l) => l.trim().length > 0);
    const events: LedgerEvent[] = [];
    for (const line of slice) {
      try {
        events.push(JSON.parse(line));
      } catch {
        // ignore
      }
    }
    return events;
  } catch {
    return [];
  }
}

async function countLedgerLines(workspaceRoot: string): Promise<number> {
  const ledgerPath = vscode.Uri.file(path.join(workspaceRoot, ".vibe", "ledger.jsonl"));
  try {
    const raw = await readTextFile(ledgerPath);
    const lines = raw.split(/\r?\n/).filter((l) => l.trim().length > 0);
    return lines.length;
  } catch {
    return 0;
  }
}

function formatRunSummary(checkpointId: string, green: boolean | undefined, events: LedgerEvent[]): string {
  const lines: string[] = [];
  lines.push(`checkpoint: ${checkpointId}`);
  if (green !== undefined) lines.push(`green: ${green}`);
  if (events.length) {
    lines.push("");
    lines.push("events:");
    for (const e of events.slice(0, 12)) {
      const agent = e.agent || "unknown";
      const type = e.type || "EVENT";
      const summary = (e.summary || "").trim();
      lines.push(`- ${agent} ${type}: ${summary}`);
    }
    if (events.length > 12) lines.push(`- ... (${events.length - 12} more)`);
  }
  return lines.join("\n");
}

export class VibeDashboardViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private running = false;
  private messages: ChatMessage[] = [
    {
      id: newId("m"),
      role: "system",
      title: "Vibe",
      text: "Send a message to create a task and run the workflow. Use Mock for a key-less dry run.",
      ts: Date.now(),
    },
  ];

  constructor(
    private readonly output: vscode.OutputChannel,
    private readonly getEnvOverrides?: EnvOverridesProvider
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void | Thenable<void> {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this.renderHtml();

    webviewView.webview.onDidReceiveMessage(async (msg) => {
      const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      if (!root) {
        vscode.window.showErrorMessage("No workspace folder open.");
        return;
      }
      try {
        const envOverrides = await this.getEnvOverrides?.();
        if (msg?.type === "ready") {
          this.postState();
          return;
        }
        if (msg?.type === "clearChat") {
          this.messages = this.messages.slice(0, 1);
          this.postState();
          return;
        }
        if (msg?.type === "chatSend") {
          const text = String(msg?.text || "").trim();
          const mock = Boolean(msg?.mock);
          if (!text) return;
          await this.handleChatSend(root, text, mock);
          return;
        }
        if (msg?.type === "init") {
          await runVibe(["init", "--path", root], { cwd: root, mock: false, output: this.output, envOverrides });
        } else if (msg?.type === "addTask") {
          const text = await vscode.window.showInputBox({ title: "Vibe: Add Task", prompt: "Task description" });
          if (!text) return;
          await runVibe(["task", "add", text, "--path", root], { cwd: root, mock: false, output: this.output, envOverrides });
        } else if (msg?.type === "runMock") {
          await runVibe(["run", "--mock", "--path", root], { cwd: root, mock: true, output: this.output, envOverrides });
        } else if (msg?.type === "run") {
          await runVibe(["run", "--path", root], { cwd: root, mock: false, output: this.output, envOverrides });
        } else if (msg?.type === "openConfig") {
          await vscode.commands.executeCommand("vibe.openConfig");
          return;
        } else if (msg?.type === "openLedger") {
          await vscode.commands.executeCommand("vibe.openLedger");
          return;
        } else if (msg?.type === "checkpoints") {
          await runVibe(["checkpoint", "list", "--path", root], { cwd: root, mock: false, output: this.output, envOverrides });
        }
        this.refresh();
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        vscode.window.showErrorMessage(message);
      }
    });

    this.postState();
  }

  refresh(): void {
    this.postState();
  }

  private addMessage(role: ChatRole, text: string, title?: string): void {
    this.messages.push({ id: newId("m"), role, title, text, ts: Date.now() });
    this.postState();
  }

  private postState(): void {
    this.view?.webview.postMessage({ type: "state", running: this.running, messages: this.messages, ts: Date.now() });
  }

  private async ensureInit(root: string, envOverrides?: NodeJS.ProcessEnv): Promise<void> {
    const cfgPath = vscode.Uri.file(path.join(root, ".vibe", "vibe.yaml"));
    if (await exists(cfgPath)) return;
    this.addMessage("assistant", "Initializing .vibe ...", "init");
    await runVibeCapture(["init", "--path", root], {
      cwd: root,
      mock: false,
      output: this.output,
      title: "Vibe: Init",
      envOverrides,
    });
    this.addMessage("assistant", "Initialized .vibe", "init");
  }

  private async handleChatSend(root: string, text: string, mock: boolean): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.addMessage("user", text);
    this.postState();

    try {
      const envOverrides = await this.getEnvOverrides?.();

      await this.ensureInit(root, envOverrides);

      const before = await countLedgerLines(root);

      const taskRes = await runVibeCapture(["task", "add", text, "--path", root], {
        cwd: root,
        mock: false,
        output: this.output,
        title: "Vibe: Add Task",
        envOverrides,
      });
      const taskId = lastNonEmptyLine(taskRes.stdout) || "(unknown_task_id)";
      this.addMessage("assistant", `task: ${taskId}`, "task");

      const runArgs = ["run", "--task", taskId, "--path", root];
      if (mock) runArgs.push("--mock");
      const runRes = await runVibeCapture(runArgs, {
        cwd: root,
        mock,
        output: this.output,
        title: mock ? "Vibe: Run (Mock)" : "Vibe: Run",
        envOverrides,
      });
      const checkpointId = lastNonEmptyLine(runRes.stdout) || "(unknown_checkpoint_id)";
      const cp = await readCheckpoint(root, checkpointId);
      const events = await readLedgerEventsSince(root, before);
      this.addMessage("assistant", formatRunSummary(checkpointId, cp?.green, events), "run");
    } catch (e) {
      if (e instanceof VibeRunError) {
        const hint =
          (e.stderr || "").includes("Missing env var") || (e.stdout || "").includes("Missing env var")
            ? "\n\nHint: set keys via Command Palette → 'Vibe: Set DeepSeek API Key' / 'Vibe: Set DashScope API Key'."
            : "";
        this.addMessage("assistant", `${e.message}\n\nstdout:\n${e.stdout}\n\nstderr:\n${e.stderr}${hint}`, "error");
      } else {
        const message = e instanceof Error ? e.message : String(e);
        this.addMessage("assistant", message, "error");
      }
    } finally {
      this.running = false;
      this.postState();
    }
  }

  private renderHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body { font-family: var(--vscode-font-family); padding: 10px; }
      h3 { margin: 6px 0 10px; }
      button { margin: 6px 0; padding: 7px 8px; }
      .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
      .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
      .messages { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 8px; height: 45vh; overflow: auto; background: var(--vscode-editor-background); }
      .msg { padding: 8px; border-radius: 8px; margin: 6px 0; border: 1px solid transparent; }
      .msg.user { background: rgba(0, 122, 204, 0.12); border-color: rgba(0, 122, 204, 0.25); }
      .msg.assistant { background: rgba(128, 128, 128, 0.10); border-color: rgba(128, 128, 128, 0.18); }
      .msg.system { background: rgba(70, 130, 180, 0.10); border-color: rgba(70, 130, 180, 0.18); }
      .meta { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; }
      .meta small { color: var(--vscode-descriptionForeground); }
      textarea { width: 100%; box-sizing: border-box; resize: vertical; min-height: 64px; max-height: 180px; padding: 8px; border-radius: 6px; border: 1px solid var(--vscode-panel-border); background: var(--vscode-input-background); color: var(--vscode-input-foreground); }
      .sendRow { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; margin-top: 8px; }
      .status { margin-top: 8px; color: var(--vscode-descriptionForeground); }
      .title { font-size: 12px; opacity: 0.8; margin-bottom: 4px; }
      pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
    </style>
  </head>
  <body>
    <h3>Vibe (Sidebar)</h3>
    <div class="actions">
      <button id="init">Init</button>
      <button id="clear">Clear Chat</button>
      <button id="config">Open Config</button>
      <button id="ledger">Open Ledger</button>
    </div>

    <div id="messages" class="messages"></div>

    <div class="meta">
      <label><input type="checkbox" id="mock" /> Mock</label>
      <small>Settings: <code>vibe.cliPath</code> / <code>vibe.permissionMode</code></small>
    </div>

    <textarea id="input" placeholder="Describe your task…"></textarea>
    <div class="sendRow">
      <div class="status" id="status"></div>
      <button id="send">Send</button>
    </div>

    <div class="row">
      <button id="runMock">Run (Mock)</button>
      <button id="run">Run</button>
    </div>
    <button id="checkpoints">Checkpoint List</button>
    <script>
      const vscode = acquireVsCodeApi();

      const elMessages = document.getElementById('messages');
      const elInput = document.getElementById('input');
      const elSend = document.getElementById('send');
      const elStatus = document.getElementById('status');
      const elMock = document.getElementById('mock');

      function escapeHtml(s) {
        return s.replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c] || c));
      }

      function render(state) {
        const messages = state.messages || [];
        elMessages.innerHTML = '';
        for (const m of messages) {
          const div = document.createElement('div');
          div.className = 'msg ' + (m.role || 'assistant');
          const title = m.title ? '<div class=\"title\">' + escapeHtml(m.title) + '</div>' : '';
          div.innerHTML = title + '<pre>' + escapeHtml(m.text || '') + '</pre>';
          elMessages.appendChild(div);
        }
        elStatus.textContent = state.running ? 'Running… (check Output -> Vibe for details)' : '';
        elSend.disabled = !!state.running;
        elInput.disabled = !!state.running;
        if (!state.running) {
          setTimeout(() => elInput.focus(), 10);
        }
        elMessages.scrollTop = elMessages.scrollHeight;
      }

      window.addEventListener('message', (event) => {
        const msg = event.data;
        if (msg && msg.type === 'state') {
          render(msg);
        }
      });

      function sendChat() {
        const text = (elInput.value || '').trim();
        if (!text) return;
        vscode.postMessage({ type: 'chatSend', text, mock: !!elMock.checked });
        elInput.value = '';
      }

      elSend.addEventListener('click', sendChat);
      elInput.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
          e.preventDefault();
          sendChat();
        }
      });

      document.getElementById('init').addEventListener('click', () => vscode.postMessage({type:'init'}));
      document.getElementById('clear').addEventListener('click', () => vscode.postMessage({type:'clearChat'}));
      document.getElementById('runMock').addEventListener('click', () => vscode.postMessage({type:'runMock'}));
      document.getElementById('run').addEventListener('click', () => vscode.postMessage({type:'run'}));
      document.getElementById('config').addEventListener('click', () => vscode.postMessage({type:'openConfig'}));
      document.getElementById('ledger').addEventListener('click', () => vscode.postMessage({type:'openLedger'}));
      document.getElementById('checkpoints').addEventListener('click', () => vscode.postMessage({type:'checkpoints'}));

      vscode.postMessage({ type: 'ready' });
    </script>
  </body>
</html>`;
  }
}

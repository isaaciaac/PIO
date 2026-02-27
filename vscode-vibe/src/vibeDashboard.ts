import * as vscode from "vscode";
import { runVibe } from "./vibeRunner";

export class VibeDashboardViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;

  constructor(private readonly output: vscode.OutputChannel) {}

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
        if (msg?.type === "init") {
          await runVibe(["init", "--path", root], { cwd: root, mock: false, output: this.output });
        } else if (msg?.type === "addTask") {
          const text = await vscode.window.showInputBox({ title: "Vibe: Add Task", prompt: "Task description" });
          if (!text) return;
          await runVibe(["task", "add", text, "--path", root], { cwd: root, mock: false, output: this.output });
        } else if (msg?.type === "runMock") {
          await runVibe(["run", "--mock", "--path", root], { cwd: root, mock: true, output: this.output });
        } else if (msg?.type === "run") {
          await runVibe(["run", "--path", root], { cwd: root, mock: false, output: this.output });
        } else if (msg?.type === "openConfig") {
          await vscode.commands.executeCommand("vibe.openConfig");
          return;
        } else if (msg?.type === "openLedger") {
          await vscode.commands.executeCommand("vibe.openLedger");
          return;
        } else if (msg?.type === "checkpoints") {
          await runVibe(["checkpoint", "list", "--path", root], { cwd: root, mock: false, output: this.output });
        }
        this.refresh();
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        vscode.window.showErrorMessage(message);
      }
    });

    this.refresh();
  }

  refresh(): void {
    this.view?.webview.postMessage({ type: "refreshed", ts: Date.now() });
  }

  private renderHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body { font-family: var(--vscode-font-family); padding: 10px; }
      h3 { margin: 6px 0 12px; }
      button { width: 100%; margin: 6px 0; padding: 8px; }
      .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
      small { color: var(--vscode-descriptionForeground); }
    </style>
  </head>
  <body>
    <h3>Vibe Dashboard</h3>
    <div class="row">
      <button id="init">Init</button>
      <button id="task">Add Task</button>
    </div>
    <div class="row">
      <button id="runMock">Run (Mock)</button>
      <button id="run">Run</button>
    </div>
    <div class="row">
      <button id="config">Open Config</button>
      <button id="ledger">Open Ledger</button>
    </div>
    <button id="checkpoints">Checkpoint List</button>
    <small>Uses CLI setting: <code>vibe.cliPath</code></small>
    <script>
      const vscode = acquireVsCodeApi();
      document.getElementById('init').addEventListener('click', () => vscode.postMessage({type:'init'}));
      document.getElementById('task').addEventListener('click', () => vscode.postMessage({type:'addTask'}));
      document.getElementById('runMock').addEventListener('click', () => vscode.postMessage({type:'runMock'}));
      document.getElementById('run').addEventListener('click', () => vscode.postMessage({type:'run'}));
      document.getElementById('config').addEventListener('click', () => vscode.postMessage({type:'openConfig'}));
      document.getElementById('ledger').addEventListener('click', () => vscode.postMessage({type:'openLedger'}));
      document.getElementById('checkpoints').addEventListener('click', () => vscode.postMessage({type:'checkpoints'}));
    </script>
  </body>
</html>`;
  }
}


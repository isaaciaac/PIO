import * as vscode from "vscode";
import * as path from "path";
import { VibeDashboardViewProvider } from "./vibeDashboard";
import { readCheckpointIds, runVibe } from "./vibeRunner";

function requireWorkspaceRoot(): string {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("No workspace folder open.");
  }
  return folder.uri.fsPath;
}

async function openFileIfExists(absPath: string): Promise<void> {
  try {
    const uri = vscode.Uri.file(absPath);
    await vscode.workspace.fs.stat(uri);
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: false });
  } catch {
    throw new Error(`File not found: ${absPath}`);
  }
}

export function activate(context: vscode.ExtensionContext) {
  const output = vscode.window.createOutputChannel("Vibe");
  context.subscriptions.push(output);

  const dashboard = new VibeDashboardViewProvider(output);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("vibe.dashboard", dashboard, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  async function exec(args: string[], opts?: { mock?: boolean }) {
    const root = requireWorkspaceRoot();
    output.show(true);
    await runVibe(args, { cwd: root, mock: opts?.mock ?? false, output });
    dashboard.refresh();
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.init", async () => {
      await exec(["init", "--path", requireWorkspaceRoot()]);
      vscode.window.showInformationMessage("Vibe initialized.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.addTask", async () => {
      const text = await vscode.window.showInputBox({
        title: "Vibe: Add Task",
        prompt: "Task description",
        validateInput: (v) => (v.trim().length === 0 ? "Task cannot be empty." : undefined),
      });
      if (!text) return;
      await exec(["task", "add", text, "--path", requireWorkspaceRoot()]);
      vscode.window.showInformationMessage("Task added to ledger.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.runMock", async () => {
      await exec(["run", "--mock", "--path", requireWorkspaceRoot()], { mock: true });
      vscode.window.showInformationMessage("Vibe run completed (mock).");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.run", async () => {
      await exec(["run", "--path", requireWorkspaceRoot()]);
      vscode.window.showInformationMessage("Vibe run completed.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.openConfig", async () => {
      const root = requireWorkspaceRoot();
      await openFileIfExists(path.join(root, ".vibe", "vibe.yaml"));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.openLedger", async () => {
      const root = requireWorkspaceRoot();
      await openFileIfExists(path.join(root, ".vibe", "ledger.jsonl"));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.checkpointList", async () => {
      const root = requireWorkspaceRoot();
      await exec(["checkpoint", "list", "--path", root]);
      vscode.window.showInformationMessage("Printed checkpoints to Vibe output.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.checkpointRestore", async () => {
      const root = requireWorkspaceRoot();
      const ids = await readCheckpointIds(root);
      if (ids.length === 0) {
        vscode.window.showWarningMessage("No checkpoints found.");
        return;
      }
      const pick = await vscode.window.showQuickPick(ids, { title: "Restore checkpoint" });
      if (!pick) return;
      await exec(["checkpoint", "restore", pick, "--path", root]);
      vscode.window.showInformationMessage(`Restored checkpoint ${pick}.`);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.branchCreateFromCheckpoint", async () => {
      const root = requireWorkspaceRoot();
      const ids = await readCheckpointIds(root);
      if (ids.length === 0) {
        vscode.window.showWarningMessage("No checkpoints found.");
        return;
      }
      const checkpointId = await vscode.window.showQuickPick(ids, { title: "Create branch from checkpoint" });
      if (!checkpointId) return;
      const branchName = await vscode.window.showInputBox({
        title: "New branch name (optional)",
        prompt: "Leave empty to use default",
      });
      const args = ["branch", "create", "--from", checkpointId, "--path", root];
      if (branchName && branchName.trim().length > 0) {
        args.push("--name", branchName.trim());
      }
      await exec(args);
      vscode.window.showInformationMessage(`Branch created from ${checkpointId}.`);
    })
  );
}

export function deactivate() {}

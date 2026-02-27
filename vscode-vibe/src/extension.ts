import * as vscode from "vscode";
import * as path from "path";
import { registerApprovalWatcher } from "./approvals";
import { VibeDashboardViewProvider } from "./vibeDashboard";
import { readCheckpointIds, runVibe } from "./vibeRunner";

const SECRET_DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY";
const SECRET_DASHSCOPE_API_KEY = "DASHSCOPE_API_KEY";

function requireWorkspaceRoot(): string {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("No workspace folder open.");
  }
  return folder.uri.fsPath;
}

async function setSecretKey(
  context: vscode.ExtensionContext,
  label: string,
  secretKey: string
): Promise<void> {
  const value = await vscode.window.showInputBox({
    title: `Vibe: Set ${label} API Key`,
    prompt: `${label} API Key (stored in VS Code SecretStorage)`,
    password: true,
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim().length === 0 ? "API key cannot be empty." : undefined),
  });
  if (!value) return;
  await context.secrets.store(secretKey, value.trim());
  vscode.window.showInformationMessage(`${label} API key stored securely (SecretStorage).`);
}

async function showApiKeyStatus(context: vscode.ExtensionContext): Promise<void> {
  const ds = (await context.secrets.get(SECRET_DEEPSEEK_API_KEY)) ? "stored" : "not set";
  const qs = (await context.secrets.get(SECRET_DASHSCOPE_API_KEY)) ? "stored" : "not set";
  vscode.window.showInformationMessage(`Vibe API keys — DeepSeek: ${ds}; DashScope: ${qs}.`);
}

async function clearStoredApiKeys(context: vscode.ExtensionContext): Promise<void> {
  const choice = await vscode.window.showWarningMessage(
    "Clear stored API keys from VS Code SecretStorage?",
    { modal: true, detail: "This only removes keys saved by the Vibe extension. It does not change your shell environment variables." },
    "Clear",
    "Cancel"
  );
  if (choice !== "Clear") return;
  await context.secrets.delete(SECRET_DEEPSEEK_API_KEY);
  await context.secrets.delete(SECRET_DASHSCOPE_API_KEY);
  vscode.window.showInformationMessage("Stored Vibe API keys cleared.");
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

async function ensureVibeInit(root: string, output: vscode.OutputChannel): Promise<void> {
  const cfg = vscode.Uri.file(path.join(root, ".vibe", "vibe.yaml"));
  try {
    await vscode.workspace.fs.stat(cfg);
    return;
  } catch {
    // continue
  }

  const choice = await vscode.window.showInformationMessage(
    "No .vibe config found in this workspace. Run Vibe: Init now?",
    "Init",
    "Cancel"
  );
  if (choice !== "Init") {
    throw new Error("Missing .vibe. Run 'Vibe: Init' first.");
  }
  output.show(true);
  await runVibe(["init", "--path", root], { cwd: root, mock: false, output });
}

export function activate(context: vscode.ExtensionContext) {
  const output = vscode.window.createOutputChannel("Vibe");
  context.subscriptions.push(output);

  registerApprovalWatcher(context, output);

  async function getEnvOverrides(): Promise<NodeJS.ProcessEnv> {
    const env: NodeJS.ProcessEnv = {};
    const deepseek = await context.secrets.get(SECRET_DEEPSEEK_API_KEY);
    const dashscope = await context.secrets.get(SECRET_DASHSCOPE_API_KEY);
    if (deepseek) env[SECRET_DEEPSEEK_API_KEY] = deepseek;
    if (dashscope) env[SECRET_DASHSCOPE_API_KEY] = dashscope;
    return env;
  }

  const dashboard = new VibeDashboardViewProvider(output, getEnvOverrides);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("vibe.dashboard", dashboard, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  async function exec(args: string[], opts?: { mock?: boolean }) {
    const root = requireWorkspaceRoot();
    output.show(true);
    const envOverrides = await getEnvOverrides();
    await runVibe(args, { cwd: root, mock: opts?.mock ?? false, output, envOverrides });
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
      await ensureVibeInit(root, output);
      await openFileIfExists(path.join(root, ".vibe", "vibe.yaml"));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.openLedger", async () => {
      const root = requireWorkspaceRoot();
      await ensureVibeInit(root, output);
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

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.setDeepSeekApiKey", async () => {
      await setSecretKey(context, "DeepSeek", SECRET_DEEPSEEK_API_KEY);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.setDashScopeApiKey", async () => {
      await setSecretKey(context, "DashScope", SECRET_DASHSCOPE_API_KEY);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.showApiKeyStatus", async () => {
      await showApiKeyStatus(context);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.clearStoredApiKeys", async () => {
      await clearStoredApiKeys(context);
    })
  );
}

export function deactivate() {}

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
    throw new Error("未打开工作区文件夹。");
  }
  return folder.uri.fsPath;
}

async function setSecretKey(
  context: vscode.ExtensionContext,
  label: string,
  secretKey: string
): Promise<void> {
  const value = await vscode.window.showInputBox({
    title: `Vibe：设置 ${label} 密钥`,
    prompt: `${label} 密钥（将安全存储在 VS Code SecretStorage）`,
    password: true,
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim().length === 0 ? "密钥不能为空。" : undefined),
  });
  if (!value) return;
  await context.secrets.store(secretKey, value.trim());

  // Optional: also sync to workspace `.vibe/secrets.json` so running `vibe` in a terminal works.
  const syncEnabled = vscode.workspace.getConfiguration("vibe").get<boolean>("syncSecretsToWorkspace", true);
  let synced = false;
  if (syncEnabled) {
    try {
      const root = requireWorkspaceRoot();
      const vibeDir = vscode.Uri.file(path.join(root, ".vibe"));
      try {
        await vscode.workspace.fs.stat(vibeDir);
        const secretsPath = vscode.Uri.file(path.join(root, ".vibe", "secrets.json"));
        let current: any = {};
        try {
          const raw = await vscode.workspace.fs.readFile(secretsPath);
          current = JSON.parse(Buffer.from(raw).toString("utf8"));
        } catch {
          current = {};
        }
        if (typeof current !== "object" || current === null || Array.isArray(current)) current = {};
        current[secretKey] = value.trim();
        const data = Buffer.from(JSON.stringify(current, null, 2) + "\n", "utf8");
        await vscode.workspace.fs.writeFile(secretsPath, data);
        synced = true;
      } catch {
        // workspace not initialized with .vibe yet
      }
    } catch {
      // no workspace
    }
  }

  vscode.window.showInformationMessage(
    synced
      ? `${label} 密钥已保存（SecretStorage + 工作区 .vibe/secrets.json）。`
      : `${label} 密钥已安全保存（SecretStorage）。`
  );
}

async function showApiKeyStatus(context: vscode.ExtensionContext): Promise<void> {
  const ds = (await context.secrets.get(SECRET_DEEPSEEK_API_KEY)) ? "已保存" : "未设置";
  const qs = (await context.secrets.get(SECRET_DASHSCOPE_API_KEY)) ? "已保存" : "未设置";
  vscode.window.showInformationMessage(`Vibe 密钥状态：DeepSeek：${ds}；DashScope：${qs}。`);
}

async function clearStoredApiKeys(context: vscode.ExtensionContext): Promise<void> {
  const choice = await vscode.window.showWarningMessage(
    "确认清除已保存的密钥吗？",
    { modal: true, detail: "只会清除 Vibe 扩展保存在 VS Code SecretStorage 里的密钥，不会修改你的系统/终端环境变量。" },
    "清除",
    "取消"
  );
  if (choice !== "清除") return;
  await context.secrets.delete(SECRET_DEEPSEEK_API_KEY);
  await context.secrets.delete(SECRET_DASHSCOPE_API_KEY);
  vscode.window.showInformationMessage("已清除已保存的 Vibe 密钥。");
}

async function openFileIfExists(absPath: string): Promise<void> {
  try {
    const uri = vscode.Uri.file(absPath);
    await vscode.workspace.fs.stat(uri);
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: false });
  } catch {
    throw new Error(`文件不存在：${absPath}`);
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
    "当前工作区未找到 .vibe 配置。现在初始化 Vibe 吗？",
    "初始化",
    "取消"
  );
  if (choice !== "初始化") {
    throw new Error("缺少 .vibe。请先运行“Vibe：初始化”。");
  }
  output.show(true);
  await runVibe(["init", "--path", root], { cwd: root, mock: false, output });
}

async function syncSecretsToWorkspaceIfEnabled(context: vscode.ExtensionContext): Promise<void> {
  const syncEnabled = vscode.workspace.getConfiguration("vibe").get<boolean>("syncSecretsToWorkspace", true);
  if (!syncEnabled) return;

  let root: string;
  try {
    root = requireWorkspaceRoot();
  } catch {
    return;
  }

  const vibeDir = vscode.Uri.file(path.join(root, ".vibe"));
  try {
    await vscode.workspace.fs.stat(vibeDir);
  } catch {
    return;
  }

  const deepseek = await context.secrets.get(SECRET_DEEPSEEK_API_KEY);
  const dashscope = await context.secrets.get(SECRET_DASHSCOPE_API_KEY);
  if (!deepseek && !dashscope) return;

  const secretsPath = vscode.Uri.file(path.join(root, ".vibe", "secrets.json"));
  let current: any = {};
  try {
    const raw = await vscode.workspace.fs.readFile(secretsPath);
    current = JSON.parse(Buffer.from(raw).toString("utf8"));
  } catch {
    current = {};
  }
  if (typeof current !== "object" || current === null || Array.isArray(current)) current = {};
  if (deepseek) current[SECRET_DEEPSEEK_API_KEY] = deepseek;
  if (dashscope) current[SECRET_DASHSCOPE_API_KEY] = dashscope;
  const data = Buffer.from(JSON.stringify(current, null, 2) + "\n", "utf8");
  await vscode.workspace.fs.writeFile(secretsPath, data);
}

export function activate(context: vscode.ExtensionContext) {
  const output = vscode.window.createOutputChannel("Vibe");
  context.subscriptions.push(output);

  registerApprovalWatcher(context, output);
  // Best-effort: make terminal `vibe` runs work even when keys were saved earlier.
  syncSecretsToWorkspaceIfEnabled(context).catch(() => undefined);

  async function getEnvOverrides(): Promise<NodeJS.ProcessEnv> {
    const env: NodeJS.ProcessEnv = {};
    const deepseek = await context.secrets.get(SECRET_DEEPSEEK_API_KEY);
    const dashscope = await context.secrets.get(SECRET_DASHSCOPE_API_KEY);
    if (deepseek) env[SECRET_DEEPSEEK_API_KEY] = deepseek;
    if (dashscope) env[SECRET_DASHSCOPE_API_KEY] = dashscope;
    return env;
  }

  const dashboard = new VibeDashboardViewProvider(context, output, getEnvOverrides);
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
      vscode.window.showInformationMessage("Vibe 初始化完成。");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.addTask", async () => {
      const text = await vscode.window.showInputBox({
        title: "Vibe：添加任务",
        prompt: "请输入任务描述",
        validateInput: (v) => (v.trim().length === 0 ? "任务描述不能为空。" : undefined),
      });
      if (!text) return;
      await exec(["task", "add", text, "--path", requireWorkspaceRoot()]);
      vscode.window.showInformationMessage("任务已写入账本（ledger）。");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.runMock", async () => {
      await exec(["run", "--mock", "--path", requireWorkspaceRoot()], { mock: true });
      vscode.window.showInformationMessage("Vibe 运行完成（模拟）。");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.run", async () => {
      await exec(["run", "--path", requireWorkspaceRoot()]);
      vscode.window.showInformationMessage("Vibe 运行完成。");
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
      vscode.window.showInformationMessage("检查点列表已输出到「输出」→ Vibe。");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.checkpointRestore", async () => {
      const root = requireWorkspaceRoot();
      const ids = await readCheckpointIds(root);
      if (ids.length === 0) {
        vscode.window.showWarningMessage("未找到检查点。");
        return;
      }
      const pick = await vscode.window.showQuickPick(ids, { title: "选择要恢复的检查点" });
      if (!pick) return;
      await exec(["checkpoint", "restore", pick, "--path", root]);
      vscode.window.showInformationMessage(`已恢复检查点：${pick}。`);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("vibe.branchCreateFromCheckpoint", async () => {
      const root = requireWorkspaceRoot();
      const ids = await readCheckpointIds(root);
      if (ids.length === 0) {
        vscode.window.showWarningMessage("未找到检查点。");
        return;
      }
      const checkpointId = await vscode.window.showQuickPick(ids, { title: "选择检查点以创建分支" });
      if (!checkpointId) return;
      const branchName = await vscode.window.showInputBox({
        title: "新分支名（可选）",
        prompt: "留空使用默认名称",
      });
      const args = ["branch", "create", "--from", checkpointId, "--path", root];
      if (branchName && branchName.trim().length > 0) {
        args.push("--name", branchName.trim());
      }
      await exec(args);
      vscode.window.showInformationMessage(`已从检查点创建分支：${checkpointId}。`);
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

import * as vscode from "vscode";

type ToolRequest = {
  id: string;
  ts: string;
  agent_id: string;
  tool: string;
  detail: string;
};

async function readJsonWithRetry(uri: vscode.Uri, retries: number = 5): Promise<any> {
  let lastErr: any = undefined;
  for (let i = 0; i < retries; i++) {
    try {
      const buf = await vscode.workspace.fs.readFile(uri);
      return JSON.parse(Buffer.from(buf).toString("utf-8"));
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 120));
    }
  }
  throw lastErr;
}

export function registerApprovalWatcher(context: vscode.ExtensionContext, output: vscode.OutputChannel): void {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) return;

  const root = folder.uri;
  const pattern = new vscode.RelativePattern(folder, ".vibe/approvals/requests/*.json");
  const watcher = vscode.workspace.createFileSystemWatcher(pattern);
  context.subscriptions.push(watcher);

  let queue = Promise.resolve();

  const handle = async (uri: vscode.Uri) => {
    const req = (await readJsonWithRetry(uri)) as ToolRequest;
    if (!req?.id) return;

    const message = `允许 ${req.agent_id} 使用 ${req.tool} 吗？`;
    const detail = req.detail || "";
    output.appendLine(`[授权] ${req.id}: ${message} ${detail}`);

    const choice = await vscode.window.showWarningMessage(message, { modal: true, detail }, "允许", "拒绝");
    const allow = choice === "允许";

    const respDir = vscode.Uri.joinPath(root, ".vibe", "approvals", "responses");
    await vscode.workspace.fs.createDirectory(respDir);
    const respPath = vscode.Uri.joinPath(respDir, `${req.id}.json`);
    const payload = Buffer.from(JSON.stringify({ allow, ts: new Date().toISOString() }, null, 2) + "\n", "utf-8");
    await vscode.workspace.fs.writeFile(respPath, payload);
  };

  const enqueue = (uri: vscode.Uri) => {
    queue = queue
      .then(() => handle(uri))
      .catch((e) => {
        const msg = e instanceof Error ? e.message : String(e);
        output.appendLine(`[授权] 错误：${msg}`);
      });
  };

  watcher.onDidCreate(enqueue, null, context.subscriptions);
  watcher.onDidChange(enqueue, null, context.subscriptions);
}

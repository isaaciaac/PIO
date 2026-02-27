import * as vscode from "vscode";
import { spawn } from "child_process";
import * as path from "path";

export interface RunVibeOptions {
  cwd: string;
  mock: boolean;
  output: vscode.OutputChannel;
}

function getCliPath(): string {
  const cfg = vscode.workspace.getConfiguration("vibe");
  const cliPath = cfg.get<string>("cliPath") || "vibe";
  return cliPath;
}

function getPermissionMode(): string {
  const cfg = vscode.workspace.getConfiguration("vibe");
  return (cfg.get<string>("permissionMode") || "config").trim();
}

export async function runVibe(args: string[], options: RunVibeOptions): Promise<void> {
  const cli = getCliPath();
  const permissionMode = getPermissionMode();
  const finalArgs = permissionMode !== "config" ? ["--policy", permissionMode, ...args] : args;
  const env: NodeJS.ProcessEnv = { ...process.env };
  if (options.mock) {
    env.VIBE_MOCK_MODE = "1";
  }
  if (permissionMode === "prompt") {
    env.VIBE_APPROVAL_DIR = path.join(options.cwd, ".vibe", "approvals");
  }

  options.output.appendLine(`$ ${cli} ${finalArgs.join(" ")}`);

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Vibe", cancellable: false },
    () =>
      new Promise<void>((resolve, reject) => {
        const proc = spawn(cli, finalArgs, { cwd: options.cwd, env, shell: false });

        proc.stdout.on("data", (d) => options.output.append(d.toString()));
        proc.stderr.on("data", (d) => options.output.append(d.toString()));

        proc.on("error", (err: any) => {
          if (err?.code === "ENOENT") {
            reject(
              new Error(
                `Cannot find vibe CLI. Install it (e.g. pip install -e .) or set VS Code setting 'vibe.cliPath'.`
              )
            );
            return;
          }
          reject(err instanceof Error ? err : new Error(String(err)));
        });

        proc.on("close", (code) => {
          if (code === 0) {
            resolve();
          } else {
            reject(new Error(`vibe exited with code ${code}`));
          }
        });
      })
  );
}

export async function readCheckpointIds(workspaceRoot: string): Promise<string[]> {
  const cpPath = vscode.Uri.file(path.join(workspaceRoot, ".vibe", "checkpoints.json"));
  try {
    const data = await vscode.workspace.fs.readFile(cpPath);
    const json = JSON.parse(Buffer.from(data).toString("utf-8"));
    const items = Array.isArray(json) ? json : json?.checkpoints;
    if (!Array.isArray(items)) return [];
    return items.map((c: any) => String(c.id)).filter(Boolean);
  } catch {
    return [];
  }
}

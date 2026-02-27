import * as vscode from "vscode";
import { spawn } from "child_process";
import * as path from "path";

export interface RunVibeOptions {
  cwd: string;
  mock: boolean;
  output: vscode.OutputChannel;
  envOverrides?: NodeJS.ProcessEnv;
  policyOverride?: string;
}

export class VibeRunError extends Error {
  public readonly code: number;
  public readonly stdout: string;
  public readonly stderr: string;

  constructor(message: string, code: number, stdout: string, stderr: string) {
    super(message);
    this.code = code;
    this.stdout = stdout;
    this.stderr = stderr;
  }
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

export interface RunVibeCaptureOptions extends RunVibeOptions {
  title?: string;
  onStdout?: (chunk: string) => void;
  onStderr?: (chunk: string) => void;
}

export async function runVibeCapture(args: string[], options: RunVibeCaptureOptions): Promise<{ stdout: string; stderr: string }> {
  const cli = getCliPath();
  const permissionMode = (options.policyOverride ?? getPermissionMode()).trim();
  const finalArgs = permissionMode !== "config" ? ["--policy", permissionMode, ...args] : args;
  const env: NodeJS.ProcessEnv = { ...process.env, ...(options.envOverrides || {}) };
  if (options.mock) {
    env.VIBE_MOCK_MODE = "1";
  }
  // Always provide an approval channel so 'config' -> policy.mode=prompt works without a TTY.
  env.VIBE_APPROVAL_DIR = path.join(options.cwd, ".vibe", "approvals");

  options.output.appendLine(`$ ${cli} ${finalArgs.join(" ")}`);

  return await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: options.title || "Vibe", cancellable: false },
    () =>
      new Promise<{ stdout: string; stderr: string }>((resolve, reject) => {
        const proc = spawn(cli, finalArgs, { cwd: options.cwd, env, shell: false });

        let stdout = "";
        let stderr = "";

        proc.stdout.on("data", (d) => {
          const s = d.toString();
          stdout += s;
          options.output.append(s);
          options.onStdout?.(s);
        });
        proc.stderr.on("data", (d) => {
          const s = d.toString();
          stderr += s;
          options.output.append(s);
          options.onStderr?.(s);
        });

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
            resolve({ stdout, stderr });
          } else {
            reject(new VibeRunError(`vibe exited with code ${code}`, code ?? -1, stdout, stderr));
          }
        });
      })
  );
}

export async function runVibe(args: string[], options: RunVibeOptions): Promise<void> {
  await runVibeCapture(args, options);
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

import * as vscode from "vscode";
import * as path from "path";
import * as crypto from "crypto";
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

function tailText(text: string, opts: { maxLines: number; maxChars: number }): string {
  const raw = String(text || "");
  if (!raw.trim()) return "";
  const lines = raw.split(/\r?\n/);
  const tailLines = lines.slice(Math.max(0, lines.length - Math.max(1, opts.maxLines)));
  let tail = tailLines.join("\n").trim();
  if (tail.length > opts.maxChars) {
    tail = "…（已截断，仅显示末尾）…\n" + tail.slice(Math.max(0, tail.length - opts.maxChars)).trim();
  }
  return tail;
}

function formatRunError(e: VibeRunError): string {
  const stderrTail = tailText(e.stderr || "", { maxLines: 60, maxChars: 2400 });
  const stdoutTail = tailText(e.stdout || "", { maxLines: 40, maxChars: 1200 });

  // Prefer stderr; fall back to stdout.
  const excerpt = stderrTail || stdoutTail;
  const label = stderrTail ? "stderr（末尾）" : "stdout（末尾）";
  const hint = "详细日志请查看：`View -> Output` → 选择 `Vibe`。";

  if (!excerpt) return `${e.message}\n\n${hint}`;
  return `${e.message}\n\n${label}：\n${excerpt}\n\n${hint}`;
}

function compactMultiline(text: string, opts: { maxLines: number; maxChars: number }): string {
  const raw = String(text || "").trim();
  if (!raw) return "";
  const lines = raw.split(/\r?\n/).map((l) => l.trimEnd()).filter((l) => l.trim().length > 0);
  if (!lines.length) return "";
  let out = lines.slice(0, Math.max(1, opts.maxLines)).join("\n").trim();
  if (lines.length > opts.maxLines) out = out + "\n…（已省略）…";
  if (out.length > opts.maxChars) out = out.slice(0, opts.maxChars).trim() + "…";
  return out;
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

type LedgerEvent = {
  id?: string;
  ts?: string;
  agent?: string;
  type?: string;
  summary?: string;
  pointers?: string[];
  meta?: any;
};

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
  lines.push(`检查点：${checkpointId}`);
  if (green !== undefined) lines.push(`通过（绿灯）：${green}`);
  if (events.length) {
    lines.push("");
    lines.push("事件：");
    for (const e of events.slice(0, 12)) {
      const agent = e.agent || "未知";
      const type = e.type || "事件";
      const summary = (e.summary || "").trim();
      lines.push(`- ${agent} ${type}：${summary}`);
    }
    if (events.length > 12) lines.push(`- ……（还有 ${events.length - 12} 条）`);
  }
  return lines.join("\n");
}

export class VibeDashboardViewProvider implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private running = false;
  private statusText = "";
  private abort?: AbortController;
  private draftParts: string[] = [];
  private draftHinted = false;
  private workspaceRoot?: string;
  private persistTimer?: NodeJS.Timeout;
  private pendingVisionContext: string[] = [];

  private static readonly CHAT_HISTORY_KEY = "vibe.chatHistory.v1";
  private static readonly CHAT_HISTORY_VERSION = 1;
  private static readonly MAX_PERSIST_MESSAGES = 160;
  private static readonly MAX_CHARS_PER_MESSAGE = 10_000;
  private static readonly PERSIST_THROTTLE_MS = 500;
  private messages: ChatMessage[] = [
    {
      id: newId("m"),
      role: "system",
      title: "Vibe",
      text: "聊天模式：可选择角色（PM/架构/安全/工程等）对话；写项目模式（确认权限/完全授权）：我会先对话梳理需求，信息足够就自动执行工作流（创建任务→Router/PM/Coder/QA→产出检查点）。想停止累积草稿：发送「取消」。",
      ts: Date.now(),
    },
  ];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly output: vscode.OutputChannel,
    private readonly getEnvOverrides?: EnvOverridesProvider
  ) {}

  resolveWebviewView(webviewView: vscode.WebviewView): void | Thenable<void> {
    this.view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.html = this.renderHtml();

    this.workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    this.restorePersisted();

    webviewView.webview.onDidReceiveMessage(async (msg) => {
      const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
      if (!root) {
        vscode.window.showErrorMessage("No workspace folder open.");
        return;
      }
      this.workspaceRoot = root;
      try {
        const envOverrides = await this.getEnvOverrides?.();
        if (msg?.type === "ready") {
          this.postState();
          return;
        }
        if (msg?.type === "pasteImage") {
          const dataUrl = String(msg?.dataUrl || "").trim();
          const mime = String(msg?.mime || "").trim().toLowerCase();
          const mode = String(msg?.mode || "chat_only").trim();
          const agent = String(msg?.agent || "pm").trim();
          if (!dataUrl.startsWith("data:") || !dataUrl.includes("base64,")) {
            return;
          }
          if (this.running) return;
          await this.handlePasteImage(root, { dataUrl, mime, mode, agent }, envOverrides);
          return;
        }
        if (msg?.type === "stop") {
          if (this.running) {
            this.setStatus("正在停止…");
            try {
              this.abort?.abort();
            } catch {}
          }
          return;
        }
        if (msg?.type === "clearChat") {
          this.messages = this.messages.slice(0, 1);
          try {
            if (this.persistTimer) clearTimeout(this.persistTimer);
          } catch {}
          this.persistTimer = undefined;
          await this.context.workspaceState.update(VibeDashboardViewProvider.CHAT_HISTORY_KEY, undefined);
          this.postState();
          return;
        }
        if (msg?.type === "chatSend") {
          const text = String(msg?.text || "").trim();
          const mock = Boolean(msg?.mock);
          const permissionMode = String(msg?.mode || "chat_only").trim();
          let route = String(msg?.route || "auto").trim();
          const agent = String(msg?.agent || "pm").trim();
          const style = String(msg?.style || "balanced").trim();
          if (!text) return;
          const withVision = this.consumePendingVision(text);
          await this.handleSend(root, withVision, mock, permissionMode, route, agent, style);
          return;
        }
        if (msg?.type === "init") {
          await runVibe(["init", "--path", root], { cwd: root, mock: false, output: this.output, envOverrides });
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
    // 不持久化高频进度行，避免刷屏和频繁写入。
    if (String(title || "").trim() !== "进度") {
      this.schedulePersist();
    }
  }

  private consumePendingVision(text: string): string {
    const t = String(text || "").trim();
    if (!t) return t;
    if (!this.pendingVisionContext.length) return t;
    const ctx = this.pendingVisionContext.join("\n\n").trim();
    this.pendingVisionContext = [];
    if (!ctx) return t;
    return `${ctx}\n\n用户问题/需求：\n${t}`.trim();
  }

  private async handlePasteImage(
    root: string,
    opts: { dataUrl: string; mime: string; mode: string; agent: string },
    envOverrides: NodeJS.ProcessEnv | undefined
  ): Promise<void> {
    this.setStatus("正在上传图片…");
    this.addMessage("system", "已粘贴图片，正在识别…", "系统");
    this.postState();

    try {
      const parts = opts.dataUrl.split(",", 2);
      const b64 = parts.length === 2 ? parts[1] : "";
      const bytes = Buffer.from(b64, "base64");
      if (!bytes.length) {
        this.addMessage("assistant", "图片解析失败：剪贴板为空或格式不支持。", "错误");
        return;
      }

      const ext = this.extFromMime(opts.mime);
      const pointer = await this.storeArtifactBytes(root, bytes, ext);

      // Run vision via vibe CLI (DashScope Qwen-VL).
      this.setStatus("正在识别图片…");
      const res = await runVibeCapture(["vision", "--artifact", pointer, "--path", root, "--json"], {
        cwd: root,
        mock: false,
        output: this.output,
        title: "Vibe：识别图片",
        envOverrides,
      });

      let payload: any = undefined;
      try {
        payload = JSON.parse(res.stdout);
      } catch {
        payload = undefined;
      }
      const summary = String(payload?.summary || "").trim();
      const description = String(payload?.description || "").trim();
      const ocr = String(payload?.ocr_text || "").trim();
      const keyPoints = Array.isArray(payload?.key_points)
        ? payload.key_points.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
        : [];

      const lines: string[] = [];
      lines.push(summary ? `图片识别：${summary}` : "图片识别完成。");
      if (description) lines.push(`\n描述：\n${description}`);
      if (keyPoints.length) lines.push(`\n要点：\n- ${keyPoints.join("\n- ")}`);
      if (ocr) lines.push(`\nOCR：\n${ocr}`);
      const text = lines.join("\n").trim();
      this.addMessage("assistant", text || "图片识别完成。", "图片");

      // Make the analysis available to the next chat/workflow turn (persisted via chat.jsonl by inclusion).
      const ctx: string[] = [];
      ctx.push("【图片识别结果】");
      if (summary) ctx.push(`- 总结：${summary}`);
      if (description) ctx.push(`- 描述：${description}`);
      if (keyPoints.length) ctx.push(`- 要点：${keyPoints.join("；")}`);
      if (ocr) ctx.push(`- OCR：\n${ocr}`);
      this.pendingVisionContext.push(ctx.join("\n").trim());

      // In authorized modes, the draft should include the vision context.
      if (opts.mode && opts.mode !== "chat_only") {
        this.draftParts.push(ctx.join("\n").trim());
      }
    } catch (e) {
      if (e instanceof VibeRunError) {
        const hint =
          (e.stderr || "").includes("Missing env var") || (e.stdout || "").includes("Missing env var")
            ? "\n\n提示：请先设置 DashScope 密钥（DASHSCOPE_API_KEY）。"
            : "";
        this.addMessage("assistant", `${formatRunError(e)}${hint}`, "错误");
      } else {
        const message = e instanceof Error ? e.message : String(e);
        this.addMessage("assistant", message, "错误");
      }
    } finally {
      this.setStatus("");
      this.postState();
    }
  }

  private extFromMime(mime: string): string {
    const m = (mime || "").toLowerCase();
    if (m.includes("jpeg") || m.includes("jpg")) return ".jpg";
    if (m.includes("webp")) return ".webp";
    if (m.includes("gif")) return ".gif";
    return ".png";
  }

  private async storeArtifactBytes(root: string, data: Buffer, ext: string): Promise<string> {
    const digest = crypto.createHash("sha256").update(data).digest("hex");
    const rel = path.join(".vibe", "artifacts", "sha256", digest.slice(0, 2), `${digest}${ext}`).replace(/\\/g, "/");
    const abs = vscode.Uri.file(path.join(root, rel));
    const dir = vscode.Uri.file(path.dirname(abs.fsPath));
    await vscode.workspace.fs.createDirectory(dir);
    try {
      await vscode.workspace.fs.stat(abs);
    } catch {
      await vscode.workspace.fs.writeFile(abs, data);
    }
    return `${rel}@sha256:${digest}`;
  }

  private persistableMessages(): Array<{ role: ChatRole; title?: string; text: string; ts: number }> {
    const banner = this.messages.length ? this.messages[0] : undefined;
    const filtered = this.messages
      .slice(1)
      .filter((m) => String(m?.title || "").trim() !== "进度")
      .map((m) => {
        const raw = String(m.text || "");
        const text =
          raw.length > VibeDashboardViewProvider.MAX_CHARS_PER_MESSAGE
            ? raw.slice(0, VibeDashboardViewProvider.MAX_CHARS_PER_MESSAGE) + "\n…（已截断）…"
            : raw;
        return { role: m.role, title: m.title, text, ts: m.ts };
      });

    const limit = Math.max(1, VibeDashboardViewProvider.MAX_PERSIST_MESSAGES);
    const tail = filtered.slice(Math.max(0, filtered.length - Math.max(0, limit - 1)));
    const out: Array<{ role: ChatRole; title?: string; text: string; ts: number }> = [];
    if (banner) out.push({ role: banner.role, title: banner.title, text: banner.text, ts: banner.ts });
    out.push(...tail);
    return out;
  }

  private schedulePersist(): void {
    if (!this.workspaceRoot) return;
    try {
      if (this.persistTimer) clearTimeout(this.persistTimer);
    } catch {}
    this.persistTimer = setTimeout(() => {
      this.persistNow().catch(() => undefined);
    }, VibeDashboardViewProvider.PERSIST_THROTTLE_MS);
  }

  private async persistNow(): Promise<void> {
    if (!this.workspaceRoot) return;
    const payload = {
      version: VibeDashboardViewProvider.CHAT_HISTORY_VERSION,
      savedAt: Date.now(),
      messages: this.persistableMessages(),
    };
    await this.context.workspaceState.update(VibeDashboardViewProvider.CHAT_HISTORY_KEY, payload);
  }

  private restorePersisted(): void {
    try {
      const raw: any = this.context.workspaceState.get(VibeDashboardViewProvider.CHAT_HISTORY_KEY);
      if (!raw || typeof raw !== "object") return;
      if (raw.version !== VibeDashboardViewProvider.CHAT_HISTORY_VERSION) return;
      const items = Array.isArray(raw.messages) ? raw.messages : [];
      const restored: ChatMessage[] = [];
      for (const it of items) {
        const role = String(it?.role || "").trim() as ChatRole;
        if (role !== "user" && role !== "assistant" && role !== "system") continue;
        const text = String(it?.text || "");
        if (!text.trim()) continue;
        const title = it?.title ? String(it.title) : undefined;
        const ts = typeof it?.ts === "number" ? it.ts : Date.now();
        restored.push({ id: newId("m"), role, title, text, ts });
      }
      if (!restored.length) return;

      // 始终保留当前版本的 banner（文案可能会更新）。
      const banner = this.messages.length ? this.messages[0] : restored[0];
      this.messages = [banner, ...restored.slice(1)];
    } catch {
      // Ignore corrupt state.
    }
  }

  private isRunCommand(text: string): boolean {
    const t = text.trim().toLowerCase();
    return t === "执行" || t === "开始执行" || t === "/run" || t === "run";
  }

  private isCancelCommand(text: string): boolean {
    const t = text.trim().toLowerCase();
    return t === "取消" || t === "清空" || t === "/cancel" || t === "cancel";
  }

  private isConfirmToExecute(text: string): boolean {
    const raw = String(text || "").trim();
    if (!raw) return false;
    const compact = raw.replace(/[，,。.;；!！?？\s]+/g, "").toLowerCase();
    if (!compact) return false;

    // Pure confirmations (often sent after PM asks to execute).
    const re =
      /^(?:好|好的|可以|行|没问题|ok|okay|开始|继续|来吧|走起|搞起|开干|开工)(?:吧|呀|呢|啦|哈|呗|嘛|哦|噢|一下|下|就|现在|立刻|马上|走起)*$/i;
    return re.test(compact);
  }

  private lastAssistantMentionsExecute(): boolean {
    for (let i = this.messages.length - 1; i >= 0; i--) {
      const m = this.messages[i];
      if (m.role !== "assistant") continue;
      const t = String(m.text || "");
      if (!t) return false;
      const low = t.toLowerCase();
      return t.includes("执行") || low.includes("/run") || low.includes("vibe run") || t.includes("工作流");
    }
    return false;
  }

  private isInspectMessage(text: string): boolean {
    const raw = String(text || "").trim();
    if (!raw) return true;

    if (/[?？]$/.test(raw)) return true;

    const compact = raw.replace(/[，,。.;；!！?？\s]+/g, "").toLowerCase();
    if (!compact) return true;

    // Short greetings / acknowledgements: treat as chat.
    if (compact.length <= 6) {
      const smallTalk = ["你好", "在吗", "嗨", "哈喽", "hello", "hi", "hey", "谢谢", "谢了", "收到", "明白", "ok", "okay"];
      if (smallTalk.some((h) => compact.includes(h))) return true;
    }

    const hints = [
      "读一下",
      "看一下",
      "看看",
      "总结",
      "解释",
      "分析",
      "进度",
      "进展",
      "状态",
      "是什么",
      "为啥",
      "为什么",
      "怎么",
      "如何",
      "能不能",
      "是否",
      "有没有",
      "对不对",
      "行不行",
      "报错",
      "日志",
      "怎么跑",
      "怎么运行",
    ];
    if (hints.some((h) => raw.includes(h))) return true;

    return false;
  }

  private shouldAutoRunWorkflow(text: string): boolean {
    const raw = String(text || "").trim();
    if (!raw) return false;

    // Strong actionable requests should run (even if they include "看一下").
    const strongRunHints = [
      "实现",
      "修复",
      "创建",
      "生成",
      "搭建",
      "编写",
      "新增",
      "添加",
      "更新",
      "改造",
      "重构",
      "优化",
      "迁移",
      "升级",
      "集成",
      "发布",
      "加上",
      "加一个",
      "支持",
      "改成",
      "调整",
      "改一下",
    ];
    if (strongRunHints.some((h) => raw.includes(h))) return true;

    // Multi-line specs are usually tasks.
    if (raw.split(/\r?\n/).length >= 3) return true;

    // Long statements are usually tasks.
    if (raw.length >= 60) return true;

    // Explicit questions / inspections: do not auto-run.
    if (this.isInspectMessage(raw)) return false;

    // Soft actionable requests: likely should run.
    const softRunHints = ["帮我", "麻烦", "请你", "请帮我", "帮忙"];
    if (softRunHints.some((h) => raw.includes(h))) return true;

    return false;
  }

  private shouldAutoRunWorkflowFromDraft(draftText: string, style: string): boolean {
    const raw = String(draftText || "").trim();
    if (!raw) return false;

    const p = (style || "balanced").trim();
    const minLen = p === "free" ? 40 : p === "detailed" ? 140 : 80;
    const minLines = p === "free" ? 2 : 3;

    // If the draft contains strong "do work" hints, execute.
    const strongRunHints = [
      "实现",
      "修复",
      "创建",
      "生成",
      "搭建",
      "编写",
      "新增",
      "添加",
      "更新",
      "改造",
      "重构",
      "优化",
      "迁移",
      "升级",
      "集成",
      "发布",
      "改一下",
      "调整",
      "改成",
      "支持",
      "加上",
      "加一个",
    ];
    if (strongRunHints.some((h) => raw.includes(h))) return true;

    // If this looks like a pure inspection/diagnosis draft, don't auto-run.
    if (this.isInspectMessage(raw)) return false;

    const nonEmptyLines = raw.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 0);
    if (nonEmptyLines.length >= minLines) return true;
    if (raw.length >= minLen) return true;

    return false;
  }

  private parseInlineRun(text: string): { isRun: boolean; taskText?: string } {
    const raw = text.trim();
    if (!raw) return { isRun: false };
    const fillerPrefix = raw.replace(/^(好的|好|可以|行|ok|OK|okay|嗯|那就|麻烦|麻烦你|请|请你|你直接|直接|你就|就)\s*[，,。.;；!！]?\s*/i, "");
    const candidates = [raw, fillerPrefix].filter((x) => x && x.length > 0);

    const particleRe = /^(?:吧|呀|呢|啦|哈|呗|嘛|哦|噢|一下|下|就|现在|立刻|马上|走起)+$/;
    const particleStarts = ["一下", "吧", "呀", "呢", "啦", "哈", "呗", "嘛", "哦", "噢", "下", "就", "现在", "立刻", "马上", "走起"];
    const stripLeadingJunk = (s: string) => s.replace(/^[\s:：，,。.;；!！]+/, "").trim();

    const tokens = ["开始执行", "执行", "/run", "run", "开干", "开工", "动手", "跑起来"];

    const matchPrefix = (s: string): { token: string; rest: string } | undefined => {
      const low = s.toLowerCase();
      for (const t of tokens) {
        const tl = t.toLowerCase();
        if (!low.startsWith(tl)) continue;
        return { token: s.slice(0, t.length), rest: s.slice(t.length) };
      }
      return undefined;
    };

    for (const cand of candidates) {
      const m = matchPrefix(cand);
      if (!m) continue;

      const restRaw = m.rest || "";
      if (!restRaw.trim()) return { isRun: true };

      // Require either separators or common particles; avoid false positives like "执行过程…".
      const first = restRaw[0];
      const hasSeparator = Boolean(first && /[\s:：，,。.;；!！]/.test(first));
      const restStripped = stripLeadingJunk(restRaw);
      const restCompact = restStripped.replace(/[，,。.;；!！?？\s]+/g, "");
      const restIsParticles = restCompact.length > 0 && particleRe.test(restCompact);
      const restStartsWithParticle = particleStarts.some((p) => restStripped.startsWith(p));

      if (!hasSeparator && !restIsParticles && !restStartsWithParticle) {
        // No separator and not a particle => treat as normal chat.
        return { isRun: false };
      }

      if (!restStripped) return { isRun: true };
      if (particleRe.test(restCompact)) return { isRun: true };

      // Support "执行一下 修复…" by stripping leading particles.
      let rest2 = restStripped;
      while (true) {
        const c = rest2.replace(/^[\s，,。.;；!！]+/, "").trim();
        if (!c) break;
        const compact = c.replace(/[，,。.;；!！?？\s]+/g, "");
        if (!compact) break;
        if (compact.startsWith("一下")) {
          rest2 = c.slice(c.indexOf("一下") + 2).trim();
          continue;
        }
        const singles = ["吧", "呀", "呢", "啦", "哈", "呗", "嘛", "哦", "噢", "下", "就"];
        const words = ["现在", "立刻", "马上", "走起"];
        const single = singles.find((p) => c.startsWith(p));
        if (single) {
          rest2 = c.slice(single.length).trim();
          continue;
        }
        const word = words.find((p) => c.startsWith(p));
        if (word) {
          rest2 = c.slice(word.length).trim();
          continue;
        }
        break;
      }

      const final = rest2.trim();
      return final ? { isRun: true, taskText: final } : { isRun: true };
    }

    // Also support trailing commands like: "<task>。执行"
    const m2 = raw.match(
      /^(.*?)(?:\s*[，,。.;；！!？?]\s*)?(执行|开始执行|\/run|run|开干|开工|动手|跑起来)(?:\s*(?:一下|下|吧|呀|呢|啦|哈|呗|嘛|哦|噢|就|现在|立刻|马上|走起)*)\s*$/i
    );
    if (!m2) return { isRun: false };
    let rest = String(m2[1] || "").trim();
    rest = rest.replace(/[，,。.;；！!？?]+$/g, "").trim();
    const compact = rest.replace(/[，,。.;；！!？?\s]+/g, "");
    if (!compact) return { isRun: true };
    if (/^(好的|好|可以|行|ok|OK|嗯|那就|麻烦|请|请你|直接)$/.test(compact)) return { isRun: true };
    if (/^(?:你)?直接$/.test(compact)) return { isRun: true };
    return { isRun: true, taskText: rest };
  }

  private agentTitle(agentId: string): string {
    const id = (agentId || "pm").trim();
    const map: Record<string, string> = {
      pm: "产品经理（PM）",
      architect: "架构师",
      security: "安全",
      coder_backend: "后端工程师",
      coder_frontend: "前端工程师",
      integration_engineer: "集成工程师",
      qa: "测试（QA）",
      code_reviewer: "代码审查",
      router: "调度器（Router）",
      env_engineer: "环境工程师",
      devops: "DevOps",
      release_manager: "发布经理",
      doc_writer: "文档",
      support_engineer: "运维/支持",
      performance: "性能",
      compliance: "合规",
      requirements_analyst: "需求分析",
      ux_writer: "UX 文案",
      api_confirm: "API/契约",
      data_engineer: "数据/迁移",
      researcher: "研究员",
      log_compressor: "日志压缩",
    };
    return map[id] || `角色：${id}`;
  }

  private postState(): void {
    this.view?.webview.postMessage({
      type: "state",
      running: this.running,
      statusText: this.statusText,
      messages: this.messages,
      ts: Date.now(),
    });
  }

  private setStatus(text: string): void {
    const t = String(text || "").trim();
    if (this.statusText === t) return;
    this.statusText = t;
    this.postState();
  }

  private stagesForAgents(agents: string[]): { key: string; label: string; types: string[] }[] {
    const a = new Set((agents || []).map((x) => String(x).trim()).filter(Boolean));
    const stages: { key: string; label: string; types: string[] }[] = [
      { key: "route", label: "策略判定", types: ["ROUTE_SELECTED", "AGENTS_ACTIVATED"] },
      { key: "context", label: "构建上下文", types: ["CONTEXT_PACKET_BUILT"] },
    ];
    if (a.has("pm")) stages.push({ key: "req", label: "需求/验收", types: ["AC_DEFINED", "REQ_CREATED", "REQ_UPDATED"] });
    if (a.has("requirements_analyst")) stages.push({ key: "usecases", label: "用例分析", types: ["USECASES_DEFINED"] });
    if (a.has("architect")) stages.push({ key: "adr", label: "架构/ADR", types: ["ADR_ADDED", "ARCH_UPDATED"] });
    if (a.has("api_confirm")) stages.push({ key: "contract", label: "契约确认", types: ["CONTRACT_CONFIRMED", "CONTRACT_CHANGED"] });
    stages.push({ key: "plan", label: "规划", types: ["PLAN_CREATED"] });
    stages.push({ key: "impl", label: "实现", types: ["PATCH_WRITTEN", "CODE_COMMIT", "CODE_REFACTOR"] });
    if (a.has("env_engineer")) stages.push({ key: "env", label: "环境/可运行性", types: ["ENV_PROBED", "ENV_UPDATED"] });
    stages.push({ key: "test", label: "测试", types: ["TEST_RUN", "TEST_PASSED", "TEST_FAILED"] });
    if (a.has("code_reviewer")) stages.push({ key: "review", label: "代码审查", types: ["REVIEW_PASSED", "REVIEW_BLOCKED"] });
    if (a.has("security")) stages.push({ key: "security", label: "安全审查", types: ["SEC_REVIEW_PASSED", "SEC_REVIEW_BLOCKED", "SEC_FINDING"] });
    if (a.has("compliance")) stages.push({ key: "compliance", label: "合规/隐私", types: ["COMPLIANCE_PASSED", "COMPLIANCE_BLOCKED"] });
    if (a.has("performance")) stages.push({ key: "perf", label: "性能/资源", types: ["PERF_BENCH_RUN", "PERF_REGRESSION"] });
    if (a.has("doc_writer")) stages.push({ key: "docs", label: "文档", types: ["DOC_UPDATED"] });
    if (a.has("devops")) stages.push({ key: "ci", label: "CI/CD", types: ["CI_UPDATED"] });
    if (a.has("release_manager")) stages.push({ key: "release", label: "发布", types: ["CHANGELOG_UPDATED", "RELEASE_TAGGED"] });
    if (a.has("support_engineer")) stages.push({ key: "runbook", label: "运维/排障", types: ["RUNBOOK_UPDATED"] });
    if (a.has("data_engineer")) stages.push({ key: "migration", label: "迁移", types: ["DB_MIGRATION_PLANNED", "DB_MIGRATION_APPLIED"] });
    stages.push({ key: "checkpoint", label: "创建检查点", types: ["CHECKPOINT_CREATED"] });
    return stages;
  }

  private routeDisplayName(routeLevel: string): string {
    const r = String(routeLevel || "").trim();
    if (!r) return "";
    return r === "L0"
      ? "快速（草稿）"
      : r === "L1"
        ? "简单MVP"
        : r === "L2"
          ? "多模块MVP"
          : r === "L3"
            ? "可发布"
            : r === "L4"
              ? "生产级"
              : r;
  }

  private startLedgerProgressWatcher(root: string, startLine: number): () => void {
    const ledgerPath = vscode.Uri.file(path.join(root, ".vibe", "ledger.jsonl"));
    let cursor = Math.max(0, startLine);
    let stopped = false;
    let inFlight = false;
    let routeLevel = "";
    let agents: string[] = [];
    const seenTypes = new Set<string>();
    let lastProgressLine = "";

    const tick = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const raw = await readTextFile(ledgerPath);
        const lines = raw.split(/\r?\n/).filter((l) => l.trim().length > 0);
        if (cursor >= lines.length) return;
        const slice = lines.slice(cursor);
        cursor = lines.length;

        let last: LedgerEvent | undefined;
        for (const line of slice) {
          try {
            const e: LedgerEvent = JSON.parse(line);
            last = e;
            const t = String(e.type || "").trim();
            if (t === "ROUTE_SELECTED") {
              // Each workflow run emits a new ROUTE_SELECTED; reset progress for multi-run auto-fix.
              routeLevel = String(e.meta?.route_level || routeLevel || "").trim();
              seenTypes.clear();
              seenTypes.add(t);
            } else if (t) {
              seenTypes.add(t);
            }
            if (t === "AGENTS_ACTIVATED" && Array.isArray(e.meta?.agents)) {
              agents = e.meta.agents.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0);
            }
          } catch {
            // ignore
          }
        }
        if (!last) return;

        const stages = this.stagesForAgents(agents);
        let idx = -1;
        for (let i = 0; i < stages.length; i++) {
          if (stages[i].types.some((t) => seenTypes.has(t))) idx = i;
        }
        const stageLabel = idx >= 0 ? stages[idx].label : "启动";
        const stepText = stages.length && idx >= 0 ? `进度：${idx + 1}/${stages.length} ${stageLabel}` : `进度：${stageLabel}`;
        const who = this.agentTitle(String(last.agent || ""));
        const evtType = String(last.type || "").trim();
        const s = String(last.summary || "").trim();
        const short = s.length > 60 ? `${s.slice(0, 60)}…` : s;
        // 路由等级用于门禁/审计，不作为主要 UI 信息展示。
        const routeText = "";
        const detail = evtType ? ` · 最近：${who} ${evtType}${short ? `：${short}` : ""}` : "";
        const progressLine = `${stepText}${routeText}${detail}`.trim();
        // Keep the bottom status minimal; stream the detailed line into the chat timeline.
        this.setStatus(`${stepText}${routeText}`.trim());
        if (progressLine && progressLine !== lastProgressLine) {
          lastProgressLine = progressLine;
          this.addMessage("system", progressLine, "进度");
        }
      } finally {
        inFlight = false;
      }
    };

    const timer = setInterval(() => {
      tick().catch(() => {});
    }, 450);

    tick().catch(() => {});

    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }

  private collectWorkflowFacts(
    taskText: string,
    taskId: string,
    checkpointId: string,
    cp: any | undefined,
    events: LedgerEvent[],
    extra?: { attempts?: number }
  ): any {
    const green: boolean | undefined = typeof cp?.green === "boolean" ? Boolean(cp.green) : undefined;
    const meta = (cp && typeof cp === "object" ? cp.meta : undefined) || {};
    const attempts = Math.max(1, Number(extra?.attempts || 1));

    let routeLevel = String(meta?.route_level || "").trim();
    if (!routeLevel) {
      const e = events.find((x) => String(x.type || "").trim() === "ROUTE_SELECTED");
      routeLevel = String(e?.meta?.route_level || "").trim();
    }
    const style = String(meta?.style || "").trim();
    const reason = String(meta?.reason || "").trim();

    let agents: string[] = Array.isArray(meta?.agents)
      ? meta.agents.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];
    if (!agents.length) {
      const e = events.find((x) => String(x.type || "").trim() === "AGENTS_ACTIVATED");
      if (Array.isArray(e?.meta?.agents)) {
        agents = e!.meta.agents.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0);
      }
    }

    const lastByType = (types: string[]): LedgerEvent | undefined => {
      for (let i = events.length - 1; i >= 0; i--) {
        const t = String(events[i].type || "").trim();
        if (types.includes(t)) return events[i];
      }
      return undefined;
    };

    const coderEvt = lastByType(["PATCH_WRITTEN", "CODE_COMMIT", "CODE_REFACTOR"]);
    const filesChanged: string[] = Array.isArray(coderEvt?.meta?.files_changed)
      ? coderEvt!.meta.files_changed.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    const qaEvt = lastByType(["TEST_PASSED", "TEST_FAILED"]);
    const qaCommands: string[] = Array.isArray(qaEvt?.meta?.commands)
      ? qaEvt!.meta.commands.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];
    const qaBlockers: string[] = Array.isArray(qaEvt?.meta?.blockers)
      ? qaEvt!.meta.blockers.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    const reviewEvt = lastByType(["REVIEW_PASSED", "REVIEW_BLOCKED"]);
    const reviewBlockers: string[] = Array.isArray(reviewEvt?.meta?.blockers)
      ? reviewEvt!.meta.blockers.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    return {
      task_text: String(taskText || "").trim() || undefined,
      task_id: taskId,
      checkpoint_id: checkpointId,
      green,
      route_level: routeLevel || undefined,
      style: style || undefined,
      reason: reason || undefined,
      attempts,
      agents,
      code_change: coderEvt
        ? {
            summary: String(coderEvt.summary || "").trim() || undefined,
            files_changed: filesChanged.slice(0, 24),
            files_changed_count: filesChanged.length,
          }
        : undefined,
      qa: qaEvt
        ? {
            profile: String(qaEvt.meta?.profile || "").trim() || undefined,
            result: String(qaEvt.type || "").trim() || undefined,
            commands: qaCommands.slice(0, 10),
            blockers: qaBlockers.slice(0, 6),
          }
        : undefined,
      review: reviewEvt
        ? {
            result: String(reviewEvt.type || "").trim() || undefined,
            blockers: reviewBlockers.slice(0, 6),
          }
        : undefined,
    };
  }

  private async summarizeWorkflowWithPm(
    root: string,
    facts: any,
    mock: boolean,
    style: string,
    envOverrides: NodeJS.ProcessEnv | undefined,
    abortSignal: AbortSignal
  ): Promise<{ text: string; pointers: string[] }> {
    const prompt =
      "你是产品经理（PM）。下面是一轮 Vibe 工作流的“事实数据”（JSON）。请给用户一个自然语言总结，用中文回答。\n\n" +
      "必须包含：\n" +
      "1) 这次做了什么（1–3 句）\n" +
      "2) 谁参与了（列出角色）\n" +
      "3) 当前状态（绿灯/非绿灯；如果非绿灯说明原因；不要输出事件流水账）\n" +
      "4) 怎么用/怎么运行（从 README / run_manifest 推断，给 2–4 条具体步骤/命令）\n" +
      "5) 数据来源真实性（如果涉及“实时数据/价格/行情”，必须明确是「真实外部数据」还是「模拟/占位数据」；如果是模拟，告诉用户如何切换到真实数据：需要哪些 key/配置、在哪个文件改）\n" +
      "6) 下一步建议（<= 3 条）\n\n" +
      "约束：\n" +
      "- 不要要求用户手动运行 vibe 命令（例如不要说“请运行 vibe run”）。\n" +
      "- 不要输出长列表/日志；避免重复“进度/事件”。\n" +
      "- 如果你引用了仓库事实片段，请把对应指针写入输出的 pointers。\n\n" +
      "事实（JSON）：\n" +
      JSON.stringify(facts, null, 2);

    const args = ["chat", prompt, "--path", root, "--json", "--agent", "pm"];
    if (mock) args.push("--mock");
    if (style) args.push("--style", style);
    const res = await runVibeCapture(args, {
      cwd: root,
      mock,
      output: this.output,
      title: "Vibe：生成总结",
      envOverrides,
      policyOverride: "chat_only",
      abortSignal,
    });

    let payload: any = undefined;
    try {
      payload = JSON.parse(res.stdout);
    } catch {
      payload = undefined;
    }
    const replyText = (payload?.reply ? String(payload.reply) : res.stdout).trim();
    const actions = Array.isArray(payload?.suggested_actions)
      ? payload.suggested_actions.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];
    const pointers = Array.isArray(payload?.pointers)
      ? payload.pointers.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    const text = actions.length ? `${replyText}\n\n下一步：\n- ${actions.join("\n- ")}` : replyText;
    return { text: text || "（无总结）", pointers };
  }

  private formatWorkflowNarrative(
    taskId: string,
    checkpointId: string,
    cp: any | undefined,
    events: LedgerEvent[],
    extra?: { attempts?: number }
  ): string {
    const green: boolean | undefined = typeof cp?.green === "boolean" ? Boolean(cp.green) : undefined;
    const meta = (cp && typeof cp === "object" ? cp.meta : undefined) || {};
    const attempts = Math.max(1, Number(extra?.attempts || 1));

    const styleRaw = String(meta?.style || "").trim();
    const styleLabel =
      styleRaw === "free" ? "自由发挥" : styleRaw === "detailed" ? "细致严谨" : styleRaw ? "平衡" : "";

    let routeLevel = String(meta?.route_level || "").trim();
    if (!routeLevel) {
      const e = events.find((x) => String(x.type || "").trim() === "ROUTE_SELECTED");
      routeLevel = String(e?.meta?.route_level || "").trim();
    }
    const routeLabel = this.routeDisplayName(routeLevel) || "（未知）";

    const agentIds: string[] = Array.isArray(meta?.agents)
      ? meta.agents.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];
    const agentsText = agentIds.length ? agentIds.map((id) => this.agentTitle(id)).join("、") : "";

    const hadTestFailed = events.some((e) => String(e.type || "").trim() === "TEST_FAILED");
    const hadReviewBlocked = events.some((e) => String(e.type || "").trim() === "REVIEW_BLOCKED");

    const lastByType = (types: string[]): LedgerEvent | undefined => {
      for (let i = events.length - 1; i >= 0; i--) {
        const t = String(events[i].type || "").trim();
        if (types.includes(t)) return events[i];
      }
      return undefined;
    };

    const coderEvt = lastByType(["PATCH_WRITTEN", "CODE_COMMIT", "CODE_REFACTOR"]);
    const filesChanged: string[] = Array.isArray(coderEvt?.meta?.files_changed)
      ? coderEvt!.meta.files_changed.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    const qaEvt = lastByType(["TEST_PASSED", "TEST_FAILED"]);
    const qaProfile = String(qaEvt?.meta?.profile || "").trim();
    const qaCommands: string[] = Array.isArray(qaEvt?.meta?.commands)
      ? qaEvt!.meta.commands.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];
    const qaBlockers: string[] = Array.isArray(qaEvt?.meta?.blockers)
      ? qaEvt!.meta.blockers.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    const reviewEvt = lastByType(["REVIEW_PASSED", "REVIEW_BLOCKED"]);
    const reviewBlockers: string[] = Array.isArray(reviewEvt?.meta?.blockers)
      ? reviewEvt!.meta.blockers.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
      : [];

    const reasonRaw = String(meta?.reason || "").trim();
    const isDraft = Boolean(meta?.draft);
    const reasonText =
      isDraft
        ? "这是“快速（草稿）”检查点，默认不标绿。"
        : reasonRaw === "qa_no_commands"
          ? "未检测到可执行的测试/校验命令（因此不算绿灯）。"
          : reasonRaw === "fix_loop_blockers"
            ? "修复循环结束后仍存在阻塞（已创建非绿灯检查点）。"
          : reasonRaw === "chat_only"
            ? "当前为仅聊天（只读）模式：可读取/搜索/扫描，但不执行命令、不写代码。"
          : reasonRaw
              ? `原因：${reasonRaw}`
              : "";

    const lines: string[] = [];
    if (green === true && attempts > 1) {
      lines.push(`已自动修复并交付（共 ${attempts} 轮）。`);
    } else if (green === false && attempts > 1) {
      lines.push(`已自动尝试修复（共 ${attempts} 轮），但仍未通过。`);
    } else {
      lines.push(`已完成工作流：任务 ${taskId}。`);
    }
    lines.push(`检查点：${checkpointId}（绿灯：${green === undefined ? "未知" : green ? "是" : "否"}）。`);
    lines.push(`策略：${routeLabel}${styleLabel ? `；风格：${styleLabel}` : ""}`);
    if (agentsText) lines.push(`启用角色：${agentsText}`);
    if (green === true && (hadTestFailed || hadReviewBlocked) && attempts <= 1) {
      const parts: string[] = [];
      if (hadTestFailed) parts.push("测试失败");
      if (hadReviewBlocked) parts.push("审查阻塞");
      lines.push(`说明：过程中遇到${parts.join("和")}，已自动修复并通过。`);
    }

    if (coderEvt) {
      const s = String(coderEvt.summary || "").trim();
      const fileHint = filesChanged.length
        ? `（影响 ${filesChanged.length} 个文件：${filesChanged.slice(0, 8).join("、")}${filesChanged.length > 8 ? "…" : ""}）`
        : "";
      lines.push(`代码变更：${s || "（无摘要）"}${fileHint}`);
    }

    if (qaEvt) {
      const passed = String(qaEvt.type || "").trim() === "TEST_PASSED";
      if (!qaCommands.length) {
        lines.push(`测试：未执行（未检测到命令）。`);
      } else {
        const cmdHint = qaCommands.slice(0, 3).join("；") + (qaCommands.length > 3 ? "…" : "");
        lines.push(`测试：${passed ? "通过" : "失败"}${qaProfile ? `（${qaProfile}）` : ""}；命令：${cmdHint}`);
      }
      if (!passed && qaBlockers.length) {
        lines.push(`测试阻塞（摘要）：${compactMultiline(qaBlockers[0], { maxLines: 6, maxChars: 600 })}`);
      }
    }

    if (reviewEvt) {
      const passed = String(reviewEvt.type || "").trim() === "REVIEW_PASSED";
      lines.push(`代码审查：${passed ? "通过" : "阻塞"}`);
      if (!passed && reviewBlockers.length) {
        lines.push(`审查阻塞（摘要）：${compactMultiline(reviewBlockers[0], { maxLines: 6, maxChars: 600 })}`);
      }
    }

    if (green === false && reasonText) lines.push(`未绿灯说明：${reasonText}`);

    lines.push("");
    lines.push("关键事件：");
    const filtered = events.filter((e) => String(e.agent || "").trim() !== "user");
    const take = filtered.slice(Math.max(0, filtered.length - 10));
    for (const e of take) {
      const who = this.agentTitle(String(e.agent || ""));
      const t = String(e.type || "").trim() || "事件";
      const s = String(e.summary || "").trim();
      const short = s.length > 80 ? `${s.slice(0, 80)}…` : s;
      lines.push(`- ${who} ${t}${short ? `：${short}` : ""}`);
    }
    if (events.length > take.length) lines.push(`- ……（共 ${events.length} 条，详见账本）`);

    const suggestions: string[] = [];
    if (green === true) {
      suggestions.push("可以继续补充需求或直接开始下一条任务；需要更严格门禁可把策略升级到“多模块MVP/可发布”。");
    } else if (green === false) {
      if (isDraft) suggestions.push("这是“快速（草稿）”路线，想要绿灯请改用“简单MVP/多模块MVP”后重跑。");
      if (reasonRaw === "qa_no_commands") {
        suggestions.push("当前项目未识别到可运行的测试/校验命令：如果是 Python/Node，请确保存在 `pyproject.toml`/`package.json` 或 `tests/`；否则请先补一个最小 smoke 命令后再跑。");
      }
      if (reviewEvt && String(reviewEvt.type || "").trim() === "REVIEW_BLOCKED") {
        suggestions.push("存在审查阻塞：我可以继续自动修复（再多尝试几轮 / 升级策略），直到通过或找到需要你确认的点。");
      }
      if (qaEvt && String(qaEvt.type || "").trim() === "TEST_FAILED") {
        suggestions.push("存在测试阻塞：我会继续尝试自动修复；如果反复失败，优先打开失败命令的 artifacts 日志确认具体报错，再继续修。");
      }
    }

    if (suggestions.length) {
      lines.push("");
      lines.push("建议：");
      for (const s of suggestions) lines.push(`- ${s}`);
    }

    return lines.join("\n").trim();
  }

  private async ensureInit(root: string, envOverrides?: NodeJS.ProcessEnv, abortSignal?: AbortSignal): Promise<void> {
    const cfgPath = vscode.Uri.file(path.join(root, ".vibe", "vibe.yaml"));
    if (await exists(cfgPath)) return;
    this.addMessage("assistant", "Initializing .vibe ...", "init");
    await runVibeCapture(["init", "--path", root], {
      cwd: root,
      mock: false,
      output: this.output,
      title: "Vibe: Init",
      envOverrides,
      abortSignal,
    });
    this.addMessage("assistant", "Initialized .vibe", "init");
  }

  private async handleAgentChat(
    root: string,
    agentId: string,
    text: string,
    mock: boolean,
    policyOverride: string | undefined,
    style: string
  ): Promise<void> {
    if (this.running) return;
    this.running = true;
    const abort = new AbortController();
    this.abort = abort;
    this.setStatus("正在对话…");
    this.addMessage("user", text);
    this.postState();

    try {
      const envOverrides = await this.getEnvOverrides?.();
      await this.ensureInit(root, envOverrides, abort.signal);

      const args = ["chat", text, "--path", root, "--json", "--agent", agentId || "pm"];
      if (mock) args.push("--mock");
      if (style) args.push("--style", style);
      const res = await runVibeCapture(args, {
        cwd: root,
        mock,
        output: this.output,
        title: mock ? "Vibe：聊天（模拟）" : "Vibe：聊天",
        envOverrides,
        policyOverride,
        abortSignal: abort.signal,
      });

      let payload: any = undefined;
      try {
        payload = JSON.parse(res.stdout);
      } catch {
        payload = undefined;
      }
      const replyText = (payload?.reply ? String(payload.reply) : res.stdout).trim();
      const actions = Array.isArray(payload?.suggested_actions)
        ? payload.suggested_actions.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
        : [];
      const pointers = Array.isArray(payload?.pointers)
        ? payload.pointers.map((x: any) => String(x)).filter((x: string) => x.trim().length > 0)
        : [];

      const assistantText = actions.length
        ? `${replyText}\n\n下一步：\n- ${actions.join("\n- ")}`
        : replyText || "（无回复）";

      // 不在对话流里打印证据指针（pointers）；它们仍会保存在账本/工件中，供审计与回放。
      void pointers;
      this.addMessage("assistant", assistantText, this.agentTitle(agentId));
    } catch (e) {
      if (e instanceof VibeRunError) {
        if (e.code === 130 || /cancelled/i.test(e.message || "")) {
          this.addMessage("assistant", "已停止当前运行。", "系统");
          return;
        }
        const hint =
          (e.stderr || "").includes("Missing env var") || (e.stdout || "").includes("Missing env var")
            ? "\n\n提示：在命令面板（Ctrl+Shift+P）运行 `Vibe：设置 DeepSeek 密钥` / `Vibe：设置 DashScope 密钥`。"
            : "";
        this.addMessage("assistant", `${formatRunError(e)}${hint}`, "错误");
      } else {
        const message = e instanceof Error ? e.message : String(e);
        this.addMessage("assistant", message, "错误");
      }
    } finally {
      this.running = false;
      this.abort = undefined;
      this.setStatus("");
      this.postState();
    }
  }

  private async handleWorkflowRun(root: string, taskText: string, mock: boolean, policyOverride: string, route: string, style: string): Promise<void> {
    if (this.running) return;
    this.running = true;
    const abort = new AbortController();
    this.abort = abort;
    this.setStatus("准备执行…");
    this.postState();

    let stopWatcher: (() => void) | undefined;
    try {
      const envOverrides = await this.getEnvOverrides?.();

      await this.ensureInit(root, envOverrides, abort.signal);

      const before = await countLedgerLines(root);

      this.setStatus("正在创建任务…");
      const taskRes = await runVibeCapture(["task", "add", taskText, "--path", root], {
        cwd: root,
        mock: false,
        output: this.output,
        title: "Vibe：创建任务",
        envOverrides,
        policyOverride,
        abortSignal: abort.signal,
      });
      const taskId = lastNonEmptyLine(taskRes.stdout) || "(unknown_task_id)";

      const maxAttempts = policyOverride === "allow_all" ? 3 : policyOverride === "prompt" ? 2 : 1;
      let attempt = 0;
      let checkpointId = "(unknown_checkpoint_id)";
      let cp: any | undefined = undefined;

      const runStart = await countLedgerLines(root);
      stopWatcher = this.startLedgerProgressWatcher(root, runStart);

      let routeToUse = (route || "auto").trim() || "auto";

      while (attempt < maxAttempts) {
        if (abort.signal.aborted) break;
        attempt += 1;
        this.setStatus(attempt === 1 ? "正在运行工作流…" : `修复中（第 ${attempt} 轮/${maxAttempts}）…`);

        const runArgs = ["run", "--task", taskId, "--path", root, "--route", routeToUse];
        if (mock) runArgs.push("--mock");
        if (style) runArgs.push("--style", style);

        const title =
          attempt === 1
            ? mock
              ? "Vibe：运行（模拟）"
              : "Vibe：运行"
            : `Vibe：继续修复（第 ${attempt} 轮）`;

        const runRes = await runVibeCapture(runArgs, {
          cwd: root,
          mock,
          output: this.output,
          title,
          envOverrides,
          policyOverride,
          abortSignal: abort.signal,
        });

        checkpointId = lastNonEmptyLine(runRes.stdout) || checkpointId;
        cp = await readCheckpoint(root, checkpointId);

        const green = typeof cp?.green === "boolean" ? Boolean(cp.green) : undefined;
        const reason = String(cp?.meta?.reason || "").trim();

        if (green === true) break;
        if (attempt >= maxAttempts) break;

        // If we exhausted the workflow's internal fix-loop, keep going automatically (budget-limited).
        if (reason === "fix_loop_blockers") {
          // If user left route=auto, allow one escalation after a failed round.
          if (routeToUse === "auto" && attempt >= 1) {
            routeToUse = "L2";
          }
          continue;
        }

        break;
      }

      const events = await readLedgerEventsSince(root, before);
      // Stop streaming progress before generating the final summary (avoid interleaving).
      stopWatcher?.();
      stopWatcher = undefined;
      this.setStatus("正在生成总结…");

      const facts = this.collectWorkflowFacts(taskText, taskId, checkpointId, cp, events, { attempts: attempt });
      try {
        const summary = await this.summarizeWorkflowWithPm(root, facts, mock, style, envOverrides, abort.signal);
        // 不在对话流里打印证据指针（pointers）；它们仍会保存在账本/工件中，供审计与回放。
        this.addMessage("assistant", summary.text, "产品经理（PM）");
      } catch (e) {
        // Fallback: deterministic narrative (compact) if PM summary fails.
        const narrative = this.formatWorkflowNarrative(taskId, checkpointId, cp, events, { attempts: attempt });
        this.addMessage("assistant", narrative, "工作流");
      }
    } catch (e) {
      if (e instanceof VibeRunError) {
        if (e.code === 130 || /cancelled/i.test(e.message || "")) {
          this.addMessage("assistant", "已停止当前运行。", "系统");
          return;
        }
        const hint =
          (e.stderr || "").includes("Missing env var") || (e.stdout || "").includes("Missing env var")
            ? "\n\n提示：在命令面板（Ctrl+Shift+P）运行 `Vibe：设置 DeepSeek 密钥` / `Vibe：设置 DashScope 密钥`。"
            : "";
        this.addMessage("assistant", `${formatRunError(e)}${hint}`, "错误");
      } else {
        const message = e instanceof Error ? e.message : String(e);
        this.addMessage("assistant", message, "错误");
      }
    } finally {
      stopWatcher?.();
      stopWatcher = undefined;
      this.running = false;
      this.abort = undefined;
      this.setStatus("");
      this.postState();
    }
  }

  private async handleSend(
    root: string,
    text: string,
    mock: boolean,
    permissionMode: string,
    route: string,
    agent: string,
    style: string
  ): Promise<void> {
    const policyOverride = (permissionMode || "chat_only").trim();
    if (this.isCancelCommand(text)) {
      this.draftParts = [];
      this.draftHinted = false;
      this.addMessage("assistant", "已清空当前草稿。继续描述你的需求即可。", "系统");
      return;
    }

    if (policyOverride === "chat_only") {
      const inline = this.parseInlineRun(text);
      if (inline.isRun) {
        this.addMessage("user", text);
        this.addMessage("assistant", "当前是聊天模式，不能执行工作流。请切换到「确认权限」或「完全授权」后再发送需求。", "系统");
        return;
      }
      const chatAgent = agent || "pm";
      await this.handleAgentChat(root, chatAgent, text, mock, policyOverride, style);
      this.draftParts.push(text);
      return;
    }

    // Always extend the draft first; in authorized modes we may run using the accumulated draft.
    const draftCandidate = [...this.draftParts, text].join("\n\n").trim();

    const inline = this.parseInlineRun(text);
    if (inline.isRun) {
      this.addMessage("user", text);
      const taskText = (inline.taskText ?? this.draftParts.join("\n\n")).trim();
      if (!taskText) {
        this.addMessage("assistant", "当前没有可执行的草稿。请先描述你的需求，或直接发送一段完整需求来执行。", "系统");
        return;
      }
      await this.handleWorkflowRun(root, taskText, mock, policyOverride, route, style);
      this.draftParts = [];
      this.draftHinted = false;
      return;
    }

    // In fully authorized mode, allow a short "OK/好" to start execution when we already have a draft,
    // and the assistant just asked the user to execute.
    if (
      policyOverride === "allow_all" &&
      this.draftParts.length > 0 &&
      this.isConfirmToExecute(text) &&
      this.lastAssistantMentionsExecute()
    ) {
      const taskText = this.draftParts.join("\n\n").trim();
      this.addMessage("user", text);
      await this.handleWorkflowRun(root, taskText, mock, policyOverride, route, style);
      this.draftParts = [];
      this.draftHinted = false;
      return;
    }

    // If the current message looks like a task, run immediately using the accumulated draft.
    if (this.shouldAutoRunWorkflow(text)) {
      this.addMessage("user", text);
      await this.handleWorkflowRun(root, draftCandidate, mock, policyOverride, route, style);
      this.draftParts = [];
      this.draftHinted = false;
      return;
    }

    // Otherwise, keep chatting to clarify — but when the draft becomes actionable,
    // auto-run without requiring a special "/run" command.
    await this.handleAgentChat(root, "pm", text, mock, policyOverride, style);
    this.draftParts.push(text);

    const draftNow = this.draftParts.join("\n\n").trim();
    if (!this.isInspectMessage(text) && this.shouldAutoRunWorkflowFromDraft(draftNow, style)) {
      this.addMessage("assistant", "信息已足够，我开始执行工作流并落地到代码…", "系统");
      await this.handleWorkflowRun(root, draftNow, mock, policyOverride, route, style);
      this.draftParts = [];
      this.draftHinted = false;
      return;
    }
  }

  private renderHtml(): string {
    return `<!DOCTYPE html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root {
        --vibe-radius: 10px;
        --vibe-gap: 10px;
      }

      html, body { height: 100%; }
      body {
        font-family: var(--vscode-font-family);
        margin: 0;
        padding: 0;
        color: var(--vscode-foreground);
        background: var(--vscode-sideBar-background);
      }

      .app {
        height: 100vh;
        display: flex;
        flex-direction: column;
      }

      .topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--vibe-gap);
        padding: 10px 12px;
        border-bottom: 1px solid var(--vscode-panel-border);
        background: var(--vscode-sideBar-background);
      }

      .brand {
        display: flex;
        align-items: baseline;
        gap: 8px;
        user-select: none;
      }
      .brand strong { font-weight: 700; letter-spacing: 0.2px; }
      .brand small { color: var(--vscode-descriptionForeground); }

      .toolbar { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }

      button {
        cursor: pointer;
        border-radius: 8px;
        border: 1px solid transparent;
        padding: 6px 10px;
        font: inherit;
      }
      button.primary {
        background: var(--vscode-button-background);
        color: var(--vscode-button-foreground);
      }
      button.primary:hover { background: var(--vscode-button-hoverBackground); }
      button.secondary {
        background: transparent;
        color: var(--vscode-foreground);
        border-color: var(--vscode-panel-border);
      }
      button.secondary:hover { background: var(--vscode-list-hoverBackground); }
      button:disabled { opacity: 0.55; cursor: not-allowed; }

      .chat {
        flex: 1;
        overflow: auto;
        padding: 12px;
        background: var(--vscode-editor-background);
      }

      .msg {
        max-width: 92%;
        border-radius: var(--vibe-radius);
        padding: 10px 12px;
        margin: 8px 0;
        border: 1px solid var(--vscode-editorWidget-border, var(--vscode-panel-border));
        background: var(--vscode-editorWidget-background, rgba(128, 128, 128, 0.08));
      }
      .msg .title {
        font-size: 11px;
        opacity: 0.8;
        margin-bottom: 6px;
        display: flex;
        justify-content: space-between;
        gap: 10px;
      }
      .msg.user {
        margin-left: auto;
        background: var(--vscode-button-background);
        color: var(--vscode-button-foreground);
        border-color: var(--vscode-button-background);
      }
      .msg.user .title { color: var(--vscode-button-foreground); }
      .msg.system {
        background: var(--vscode-notifications-background, rgba(70, 130, 180, 0.10));
        border-color: var(--vscode-notifications-border, var(--vscode-panel-border));
      }
      /* 进度行：更紧凑，不显示卡片边框 */
      .msg.progress {
        max-width: 100%;
        margin: 4px 0;
        padding: 2px 0;
        border: none;
        border-radius: 0;
        background: transparent;
        color: var(--vscode-descriptionForeground);
        font-size: 11px;
      }
      pre { margin: 0; white-space: pre-wrap; word-break: break-word; }

      .composer {
        padding: 10px 12px;
        border-top: 1px solid var(--vscode-panel-border);
        background: var(--vscode-sideBar-background);
      }

      .metaRow {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 8px;
        color: var(--vscode-descriptionForeground);
        font-size: 12px;
      }

      .leftMeta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

      .agentWrap { display: inline-flex; align-items: center; gap: 6px; }
      .agentWrap label { color: var(--vscode-descriptionForeground); }

      select {
        background: var(--vscode-dropdown-background, var(--vscode-input-background));
        color: var(--vscode-dropdown-foreground, var(--vscode-input-foreground));
        border: 1px solid var(--vscode-dropdown-border, var(--vscode-panel-border));
        border-radius: 8px;
        padding: 4px 8px;
        font: inherit;
      }

      textarea {
        width: 100%;
        box-sizing: border-box;
        resize: vertical;
        min-height: 84px;
        max-height: 220px;
        padding: 10px 10px;
        border-radius: 10px;
        border: 1px solid var(--vscode-input-border, var(--vscode-panel-border));
        background: var(--vscode-input-background);
        color: var(--vscode-input-foreground);
        outline: none;
      }
      textarea:focus { border-color: var(--vscode-focusBorder); }

      .sendRow {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        margin-top: 8px;
      }

      .status { color: var(--vscode-descriptionForeground); font-size: 12px; }

      .btnRow {
        display: flex;
        gap: 8px;
        justify-content: flex-end;
        flex-wrap: wrap;
      }
    </style>
  </head>
  <body>
    <div class="app">
      <div class="topbar">
        <div class="brand">
          <strong>Vibe</strong>
          <small>多代理工作流</small>
        </div>
        <div class="toolbar">
          <button class="secondary" id="init" title="在当前工作区初始化 .vibe">初始化</button>
          <button class="secondary" id="config" title="打开 .vibe/vibe.yaml">配置</button>
          <button class="secondary" id="ledger" title="打开 .vibe/ledger.jsonl">账本</button>
          <button class="secondary" id="checkpoints" title="在「输出」中打印检查点列表">检查点</button>
          <button class="secondary" id="stop" title="停止当前运行（会中止 vibe 子进程）" disabled>停止</button>
          <button class="secondary" id="clear" title="清空聊天记录">清空</button>
        </div>
      </div>

      <div id="messages" class="chat"></div>

      <div class="composer">
        <div class="metaRow">
          <div class="leftMeta">
            <select id="mode" title="聊天模式：只对话（禁用本地工具）；写项目模式：允许本地工具（确认权限会逐项询问、完全授权不询问）。写项目模式下：信息足够会自动执行并落地到代码；不足会追问。">
              <option value="chat_only" selected>仅聊天（只读）</option>
              <option value="prompt">确认权限（逐项询问）</option>
              <option value="allow_all">完全授权（不询问）</option>
            </select>
            <span class="agentWrap" id="agentWrap" title="仅聊天模式可选角色；写项目模式默认与 PM 对话梳理需求（信息足够会自动触发工作流）。">
              <label for="agent">角色</label>
              <select id="agent">
                <option value="pm" selected>产品经理（PM）</option>
                <option value="architect">架构师</option>
                <option value="security">安全</option>
                <option value="coder_backend">后端工程师</option>
                <option value="coder_frontend">前端工程师</option>
                <option value="integration_engineer">集成工程师</option>
                <option value="qa">测试（QA）</option>
                <option value="code_reviewer">代码审查</option>
                <option value="env_engineer">环境工程师</option>
                <option value="devops">DevOps</option>
                <option value="release_manager">发布经理</option>
                <option value="doc_writer">文档</option>
                <option value="support_engineer">运维/支持</option>
                <option value="performance">性能</option>
                <option value="compliance">合规</option>
                <option value="requirements_analyst">需求分析</option>
                <option value="ux_writer">UX 文案</option>
                <option value="api_confirm">API/契约</option>
                <option value="data_engineer">数据/迁移</option>
                <option value="researcher">研究员</option>
                <option value="log_compressor">日志压缩</option>
                <option value="router">调度器（Router）</option>
              </select>
            </span>
            <select id="route" title="策略：自动由系统判定；越高越严格（会启用更多门禁与角色，耗时更长）">
              <option value="auto" selected>策略：自动</option>
              <option value="L0">策略：快速（草稿）</option>
              <option value="L1">策略：简单MVP</option>
              <option value="L2">策略：多模块MVP</option>
              <option value="L3">策略：可发布</option>
              <option value="L4">策略：生产级</option>
            </select>
            <select id="style" title="对话/方案的细致程度：自由发挥会更少追问、更多默认假设；细致严谨会更全面">
              <option value="balanced" selected>风格：平衡</option>
              <option value="free">风格：自由发挥</option>
              <option value="detailed">风格：细致严谨</option>
            </select>
          </div>
        </div>

        <textarea id="input" placeholder="请输入你的问题或需求…（Ctrl/⌘ + Enter 发送）"></textarea>

        <div class="sendRow">
          <div class="status" id="status"></div>
          <div class="btnRow">
            <button class="primary" id="send" title="发送（由上方模式决定：聊天或工作流）">发送</button>
          </div>
        </div>
      </div>
    </div>
    <script>
      const vscode = acquireVsCodeApi();

      const elMessages = document.getElementById('messages');
      const elInput = document.getElementById('input');
      const elSend = document.getElementById('send');
      const elStatus = document.getElementById('status');
      const elStop = document.getElementById('stop');
      const elMode = document.getElementById('mode');
      const elAgent = document.getElementById('agent');
      const elAgentWrap = document.getElementById('agentWrap');
      const elRoute = document.getElementById('route');
      const elStyle = document.getElementById('style');

      function escapeHtml(s) {
        return s.replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c] || c));
      }

      function render(state) {
        const messages = state.messages || [];
        elMessages.innerHTML = '';
        for (const m of messages) {
          const div = document.createElement('div');
          const titleLeft = m.title ? String(m.title) : '';
          const isProgress = titleLeft === '进度';
          div.className = 'msg ' + (m.role || 'assistant') + (isProgress ? ' progress' : '');
          const title = (!isProgress && titleLeft)
            ? '<div class=\"title\"><span>' + escapeHtml(titleLeft) + '</span><span></span></div>'
            : '';
          div.innerHTML = title + '<pre>' + escapeHtml(m.text || '') + '</pre>';
          elMessages.appendChild(div);
        }
        const st = (state && state.statusText) ? String(state.statusText) : '';
        elStatus.textContent = state.running ? (st || '运行中…（详情见「输出」→ Vibe）') : '';
        elSend.disabled = !!state.running;
        elInput.disabled = !!state.running;
        if (elStop) elStop.disabled = !state.running;
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
        const mode = (elMode && elMode.value) ? elMode.value : 'chat_only';
        const agent = (elAgent && elAgent.value) ? elAgent.value : 'pm';
        const route = (elRoute && elRoute.value) ? elRoute.value : 'auto';
        const style = (elStyle && elStyle.value) ? elStyle.value : 'balanced';
        vscode.postMessage({ type: 'chatSend', mode, route, agent, style, text, mock: false });
        elInput.value = '';
      }

      elSend.addEventListener('click', sendChat);
      elInput.addEventListener('paste', (e) => {
        try {
          const items = (e.clipboardData && e.clipboardData.items) ? Array.from(e.clipboardData.items) : [];
          const img = items.find((it) => it.kind === 'file' && it.type && it.type.startsWith('image/'));
          if (!img) return;
          const file = img.getAsFile();
          if (!file) return;
          e.preventDefault();
          const reader = new FileReader();
          reader.onload = () => {
            const dataUrl = String(reader.result || '');
            const mode = (elMode && elMode.value) ? elMode.value : 'chat_only';
            const agent = (elAgent && elAgent.value) ? elAgent.value : 'pm';
            vscode.postMessage({ type: 'pasteImage', dataUrl, mime: img.type || file.type || 'image/png', mode, agent });
          };
          reader.readAsDataURL(file);
        } catch {}
      });
      elInput.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
          e.preventDefault();
          sendChat();
        }
      });

      function syncMode() {
        const mode = (elMode && elMode.value) ? elMode.value : 'chat_only';
        if (elAgentWrap) {
          elAgentWrap.style.display = (mode === 'chat_only') ? 'inline-flex' : 'none';
        }
      }

      function loadUiState() {
        const st = vscode.getState() || {};
        try {
          if (st && st.mode && elMode) elMode.value = String(st.mode);
          if (st && st.agent && elAgent) elAgent.value = String(st.agent);
          if (st && st.route && elRoute) elRoute.value = String(st.route);
          if (st && st.style && elStyle) elStyle.value = String(st.style);
        } catch {}
      }

      function saveUiState() {
        vscode.setState({
          mode: (elMode && elMode.value) ? elMode.value : 'chat_only',
          agent: (elAgent && elAgent.value) ? elAgent.value : 'pm',
          route: (elRoute && elRoute.value) ? elRoute.value : 'auto',
          style: (elStyle && elStyle.value) ? elStyle.value : 'balanced',
        });
      }

      if (elMode) elMode.addEventListener('change', () => { syncMode(); saveUiState(); });
      if (elAgent) elAgent.addEventListener('change', saveUiState);
      if (elRoute) elRoute.addEventListener('change', saveUiState);
      if (elStyle) elStyle.addEventListener('change', saveUiState);

      loadUiState();
      syncMode();

      document.getElementById('init').addEventListener('click', () => vscode.postMessage({type:'init'}));
      document.getElementById('stop').addEventListener('click', () => vscode.postMessage({type:'stop'}));
      document.getElementById('clear').addEventListener('click', () => vscode.postMessage({type:'clearChat'}));
      document.getElementById('config').addEventListener('click', () => vscode.postMessage({type:'openConfig'}));
      document.getElementById('ledger').addEventListener('click', () => vscode.postMessage({type:'openLedger'}));
      document.getElementById('checkpoints').addEventListener('click', () => vscode.postMessage({type:'checkpoints'}));

      vscode.postMessage({ type: 'ready' });
    </script>
  </body>
</html>`;
  }
}

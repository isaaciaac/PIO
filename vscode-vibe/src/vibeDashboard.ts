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
  private draftParts: string[] = [];
  private draftHinted = false;
  private messages: ChatMessage[] = [
    {
      id: newId("m"),
      role: "system",
      title: "Vibe",
      text: "仅聊天模式：可选择角色（PM/架构/安全/工程等）对话；授权模式：只与 PM 对话并可执行工作流。开始执行：执行（或 /run）。清空草稿：取消（或 /cancel）。",
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
          const permissionMode = String(msg?.mode || "chat_only").trim();
          const route = String(msg?.route || "auto").trim();
          const agent = String(msg?.agent || "pm").trim();
          if (!text) return;
          await this.handleSend(root, text, mock, permissionMode, route, agent);
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
  }

  private isRunCommand(text: string): boolean {
    const t = text.trim().toLowerCase();
    return t === "执行" || t === "开始执行" || t === "/run" || t === "run";
  }

  private isCancelCommand(text: string): boolean {
    const t = text.trim().toLowerCase();
    return t === "取消" || t === "清空" || t === "/cancel" || t === "cancel";
  }

  private parseInlineRun(text: string): { isRun: boolean; taskText?: string } {
    const raw = text.trim();
    if (!raw) return { isRun: false };
    const m = raw.match(/^(执行|开始执行|\/run|run)\s*[:：]?\s*(.*)$/i);
    if (!m) return { isRun: false };
    const rest = String(m[2] || "").trim();
    return rest ? { isRun: true, taskText: rest } : { isRun: true };
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

  private async handleAgentChat(root: string, agentId: string, text: string, mock: boolean, policyOverride?: string): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.addMessage("user", text);
    this.postState();

    try {
      const envOverrides = await this.getEnvOverrides?.();
      await this.ensureInit(root, envOverrides);

      const args = ["chat", text, "--path", root, "--json", "--agent", agentId || "pm"];
      if (mock) args.push("--mock");
      const res = await runVibeCapture(args, {
        cwd: root,
        mock,
        output: this.output,
        title: mock ? "Vibe：聊天（模拟）" : "Vibe：聊天",
        envOverrides,
        policyOverride,
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

      const assistantText = actions.length
        ? `${replyText}\n\n下一步：\n- ${actions.join("\n- ")}`
        : replyText || "（无回复）";

      this.addMessage("assistant", assistantText, this.agentTitle(agentId));
    } catch (e) {
      if (e instanceof VibeRunError) {
        const hint =
          (e.stderr || "").includes("Missing env var") || (e.stdout || "").includes("Missing env var")
            ? "\n\n提示：在命令面板（Ctrl+Shift+P）运行 `Vibe：设置 DeepSeek 密钥` / `Vibe：设置 DashScope 密钥`。"
            : "";
        this.addMessage("assistant", `${e.message}\n\nstdout:\n${e.stdout}\n\nstderr:\n${e.stderr}${hint}`, "错误");
      } else {
        const message = e instanceof Error ? e.message : String(e);
        this.addMessage("assistant", message, "错误");
      }
    } finally {
      this.running = false;
      this.postState();
    }
  }

  private async handleWorkflowRun(root: string, taskText: string, mock: boolean, policyOverride: string, route: string): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.postState();

    try {
      const envOverrides = await this.getEnvOverrides?.();

      await this.ensureInit(root, envOverrides);

      const before = await countLedgerLines(root);

      const taskRes = await runVibeCapture(["task", "add", taskText, "--path", root], {
        cwd: root,
        mock: false,
        output: this.output,
        title: "Vibe：创建任务",
        envOverrides,
        policyOverride,
      });
      const taskId = lastNonEmptyLine(taskRes.stdout) || "(unknown_task_id)";
      this.addMessage("assistant", `已创建任务：${taskId}`, "任务");

      const runArgs = ["run", "--task", taskId, "--path", root, "--route", route || "auto"];
      if (mock) runArgs.push("--mock");
      const runRes = await runVibeCapture(runArgs, {
        cwd: root,
        mock,
        output: this.output,
        title: mock ? "Vibe：运行（模拟）" : "Vibe：运行",
        envOverrides,
        policyOverride,
      });
      const checkpointId = lastNonEmptyLine(runRes.stdout) || "(unknown_checkpoint_id)";
      const cp = await readCheckpoint(root, checkpointId);
      const events = await readLedgerEventsSince(root, before);
      this.addMessage("assistant", formatRunSummary(checkpointId, cp?.green, events), "工作流");
    } catch (e) {
      if (e instanceof VibeRunError) {
        const hint =
          (e.stderr || "").includes("Missing env var") || (e.stdout || "").includes("Missing env var")
            ? "\n\n提示：在命令面板（Ctrl+Shift+P）运行 `Vibe：设置 DeepSeek 密钥` / `Vibe：设置 DashScope 密钥`。"
            : "";
        this.addMessage("assistant", `${e.message}\n\nstdout:\n${e.stdout}\n\nstderr:\n${e.stderr}${hint}`, "错误");
      } else {
        const message = e instanceof Error ? e.message : String(e);
        this.addMessage("assistant", message, "错误");
      }
    } finally {
      this.running = false;
      this.postState();
    }
  }

  private async handleSend(root: string, text: string, mock: boolean, permissionMode: string, route: string, agent: string): Promise<void> {
    const policyOverride = (permissionMode || "chat_only").trim();
    if (this.isCancelCommand(text)) {
      this.draftParts = [];
      this.draftHinted = false;
      this.addMessage("assistant", "已清空当前草稿。继续描述你的需求，或发送：执行", "系统");
      return;
    }

    const inline = this.parseInlineRun(text);
    if (inline.isRun) {
      this.addMessage("user", text);
      const taskText = (inline.taskText ?? this.draftParts.join("\n\n")).trim();
      if (!taskText) {
        this.addMessage("assistant", "当前没有可执行的草稿。请先描述你要做的改动，然后再发送：执行", "系统");
        return;
      }
      await this.handleWorkflowRun(root, taskText, mock, policyOverride, route);
      this.draftParts = [];
      this.draftHinted = false;
      return;
    }

    // Authorized modes always talk to PM; chat_only can pick an agent.
    const chatAgent = policyOverride === "chat_only" ? (agent || "pm") : "pm";
    await this.handleAgentChat(root, chatAgent, text, mock, policyOverride);

    // Build a runnable draft in parallel with the conversation.
    this.draftParts.push(text);

    // If user is in a write-capable mode, guide them to run explicitly.
    if (policyOverride === "prompt" || policyOverride === "allow_all") {
      if (!this.draftHinted) {
        this.draftHinted = true;
        this.addMessage(
          "assistant",
          "已进入写项目模式：你可以继续补充需求/回答追问。确认要开始执行工作流时，请发送：执行（或 /run）。清空草稿：取消（或 /cancel）。",
          "系统"
        );
      }
    }
  }

  private renderHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
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
          <button class="secondary" id="clear" title="清空聊天记录">清空</button>
        </div>
      </div>

      <div id="messages" class="chat"></div>

      <div class="composer">
        <div class="metaRow">
          <div class="leftMeta">
            <label><input type="checkbox" id="mock" /> 模拟（无需密钥）</label>
            <select id="mode" title="权限只决定是否允许本地工具动作；你随时都在和 PM 对话。写项目时发送：执行">
              <option value="chat_only" selected>仅聊天（禁用工具）</option>
              <option value="prompt">确认权限（逐项询问）</option>
              <option value="allow_all">完全授权（不询问）</option>
            </select>
            <span class="agentWrap" id="agentWrap" title="仅聊天模式可选角色；授权模式固定与 PM 对话">
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
            <select id="route" title="路由等级：自动由 RouteDecider 决定；更高等级会启用更多门禁（未实现等级会报错）">
              <option value="auto" selected>路由：自动</option>
              <option value="L0">路由：L0 极速</option>
              <option value="L1">路由：L1 标准</option>
              <option value="L2">路由：L2 安全</option>
              <option value="L3">路由：L3 发布</option>
              <option value="L4">路由：L4 全路径</option>
            </select>
          </div>
          <span>设置：<code>vibe.cliPath</code> / <code>vibe.permissionMode</code></span>
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
      const elMock = document.getElementById('mock');
      const elMode = document.getElementById('mode');
      const elAgent = document.getElementById('agent');
      const elAgentWrap = document.getElementById('agentWrap');
      const elRoute = document.getElementById('route');

      function escapeHtml(s) {
        return s.replace(/[&<>\"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c] || c));
      }

      function render(state) {
        const messages = state.messages || [];
        elMessages.innerHTML = '';
        for (const m of messages) {
          const div = document.createElement('div');
          div.className = 'msg ' + (m.role || 'assistant');
          const titleLeft = m.title ? escapeHtml(m.title) : '';
          const title = titleLeft ? '<div class=\"title\"><span>' + titleLeft + '</span><span></span></div>' : '';
          div.innerHTML = title + '<pre>' + escapeHtml(m.text || '') + '</pre>';
          elMessages.appendChild(div);
        }
        elStatus.textContent = state.running ? '运行中…（详情见「输出」→ Vibe）' : '';
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
        const mode = (elMode && elMode.value) ? elMode.value : 'chat_only';
        const agent = (elAgent && elAgent.value) ? elAgent.value : 'pm';
        const route = (elRoute && elRoute.value) ? elRoute.value : 'auto';
        vscode.postMessage({ type: 'chatSend', mode, route, agent, text, mock: !!elMock.checked });
        elInput.value = '';
      }

      elSend.addEventListener('click', sendChat);
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
      if (elMode) elMode.addEventListener('change', syncMode);
      syncMode();

      document.getElementById('init').addEventListener('click', () => vscode.postMessage({type:'init'}));
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

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from vibe.orchestration.contracts import ContractAuditMixin
from vibe.orchestration.shared import _normalize_scope_pattern
from vibe.schemas import packs


class FailureDiagnosisMixin(ContractAuditMixin):
    def _coerce_contract_audit(self, raw: Any) -> Optional[packs.ContractAuditReport]:
        if isinstance(raw, packs.ContractAuditReport):
            return raw
        if not isinstance(raw, dict):
            return None
        try:
            return packs.ContractAuditReport.model_validate(raw)
        except Exception:
            return None

    def _observe_test_failure(self, *, report: packs.TestReport, blocker_text: str) -> tuple[dict[str, Any], Optional[str]]:
        failed_cmd = self._failed_command_from_report(report)
        traceback_location = self._traceback_location_from_text(blocker_text)
        related_files: list[str] = []
        pointers: list[str] = [str(p).strip() for p in (report.pointers or []) if str(p).strip()]

        def add_file(rel: str) -> None:
            rp = str(rel or "").replace("\\", "/").lstrip("/")
            if not rp or rp.startswith(".vibe/") or rp.startswith(".git/") or rp in related_files:
                return
            related_files.append(rp)
            try:
                rr = self.toolbox.read_file(agent_id="router", path=rp, start_line=1, end_line=220)
                if rr.pointer and rr.pointer not in pointers:
                    pointers.append(rr.pointer)
            except Exception:
                pass

        for rel in self._recent_changed_files(limit=10):
            add_file(rel)

        if traceback_location:
            add_file(traceback_location.split(":", 1)[0])

        for match in re.finditer(
            r"(?P<file>[A-Za-z0-9_./\\-]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error",
            blocker_text or "",
            flags=re.IGNORECASE,
        ):
            add_file(match.group("file"))
            if len(related_files) >= 10:
                break

        module = ""
        symbol = ""
        try:
            match = re.search(
                r"cannot import name ['\"](?P<sym>[^'\"]+)['\"] from ['\"](?P<mod>[^'\"]+)['\"]",
                blocker_text or "",
                flags=re.IGNORECASE,
            )
            if match:
                module = str(match.group("mod") or "").strip()
                symbol = str(match.group("sym") or "").strip()
        except Exception:
            module = ""
            symbol = ""
        if not module:
            try:
                match = re.search(r"No module named ['\"](?P<mod>[^'\"]+)['\"]", blocker_text or "", flags=re.IGNORECASE)
                if match:
                    module = str(match.group("mod") or "").strip()
            except Exception:
                module = ""
        if not symbol:
            try:
                match = re.search(
                    r"name ['\"](?P<sym>[A-Za-z_][A-Za-z0-9_]*)['\"] is not defined",
                    blocker_text or "",
                    flags=re.IGNORECASE,
                )
                if match:
                    symbol = str(match.group("sym") or "").strip()
            except Exception:
                symbol = ""
        if not symbol:
            try:
                match = re.search(
                    r"has no attribute ['\"](?P<sym>[A-Za-z_][A-Za-z0-9_]*)['\"]",
                    blocker_text or "",
                    flags=re.IGNORECASE,
                )
                if match:
                    symbol = str(match.group("sym") or "").strip()
            except Exception:
                symbol = ""

        if module:
            for candidate in list(getattr(self, "_module_candidate_paths")(module))[:24]:
                if (self.repo_root / candidate).exists():
                    add_file(candidate)

        observation_seed = {
            "module": module,
            "symbol": symbol,
            "related_files": list(related_files[:12]),
        }
        static_issues = self._python_static_skeleton_issues(observation=observation_seed, blocker_text=blocker_text)
        for issue in static_issues:
            for rel in list(issue.get("files") or [])[:8]:
                add_file(str(rel))

        contract_audit = self._python_contract_audit(
            observation={**observation_seed, "related_files": list(related_files[:12])},
            blocker_text=blocker_text,
        )
        contract_audit_ptr: Optional[str] = None
        if contract_audit.issues:
            for rel in list(contract_audit.affected_files or [])[:12]:
                add_file(rel)
            for issue in list(contract_audit.issues or [])[:8]:
                for rel in list(issue.files or [])[:8]:
                    add_file(rel)
            try:
                contract_audit_ptr = self.artifacts.put_json(
                    contract_audit.model_dump(),
                    suffix=".contract_audit.json",
                    kind="contract_audit",
                ).to_pointer()
            except Exception:
                contract_audit_ptr = None
            if contract_audit_ptr and contract_audit_ptr not in pointers:
                pointers.append(contract_audit_ptr)

        observation = {
            "summary": "Observed blocker for diagnoser",
            "failed_command": failed_cmd,
            "traceback_location": traceback_location,
            "module": module,
            "symbol": symbol,
            "recent_files": related_files[:10],
            "related_files": related_files[:12],
            "evidence_pointers": pointers[:24],
            "static_issues": static_issues,
            "contract_audit": contract_audit.model_dump() if contract_audit.issues else None,
            "contract_audit_pointer": contract_audit_ptr,
        }
        try:
            ptr = self.artifacts.put_json(observation, suffix=".observer.json", kind="observer").to_pointer()
        except Exception:
            ptr = None
        return observation, ptr

    def _diagnose_test_failure(
        self,
        *,
        report: packs.TestReport,
        blocker_text: str,
        observation: Optional[dict[str, Any]] = None,
    ) -> packs.ErrorObject:
        text = str(blocker_text or "").strip()
        low = text.lower()
        obs = observation or {}
        if not obs:
            try:
                obs, _ptr = self._observe_test_failure(report=report, blocker_text=text)
            except Exception:
                obs = {}
        failed_cmd = str(obs.get("failed_command") or self._failed_command_from_report(report) or "").strip()
        module = str(obs.get("module") or "").strip()
        symbol = str(obs.get("symbol") or "").strip()
        traceback_location = str(obs.get("traceback_location") or self._traceback_location_from_text(text) or "").strip()
        related_files = [str(x).strip() for x in list(obs.get("related_files") or []) if str(x).strip()][:12]
        evidence_pointers = [str(x).strip() for x in list(obs.get("evidence_pointers") or []) if str(x).strip()][:24]
        static_issues = [x for x in list(obs.get("static_issues") or []) if isinstance(x, dict)]
        static_issue_ids = [str(x.get("id") or "").strip() for x in static_issues if str(x.get("id") or "").strip()]
        static_summaries = [str(x.get("summary") or "").strip() for x in static_issues if str(x.get("summary") or "").strip()]

        contract_audit = self._coerce_contract_audit(obs.get("contract_audit"))
        contract_issue_ids: list[str] = []
        contract_summaries: list[str] = []
        if contract_audit is not None:
            for issue in list(contract_audit.issues or [])[:8]:
                issue_id = str(issue.issue_id or "").strip()
                if issue_id:
                    contract_issue_ids.append(issue_id)
                summary = str(issue.summary or "").strip()
                if summary:
                    contract_summaries.append(summary)
                for rel in list(issue.files or [])[:8]:
                    norm = _normalize_scope_pattern(str(rel))
                    if norm and norm not in related_files:
                        related_files.append(norm)
            for rel in list(contract_audit.affected_files or [])[:12]:
                norm = _normalize_scope_pattern(str(rel))
                if norm and norm not in related_files:
                    related_files.append(norm)
            for ptr in list(contract_audit.pointers or [])[:8]:
                s = str(ptr or "").strip()
                if s and s not in evidence_pointers:
                    evidence_pointers.append(s)

        error_type: packs.ErrorType = "unclassified"
        root_cause = "需要进一步诊断根因"

        contract_issue_map = {
            "package_shadow": "wrong_import_path",
            "missing_export": "missing_export",
            "call_signature_mismatch": "contract_drift",
            "exception_taxonomy_mismatch": "exception_taxonomy_mismatch",
            "engine_interface_mismatch": "engine_interface_mismatch",
            "data_shape_mismatch": "data_shape_mismatch",
            "contract_drift": "contract_drift",
        }
        dominant_contract_issue = (
            str(contract_audit.dominant_issue_type or "").strip() if contract_audit is not None else ""
        )
        contract_error_type = contract_issue_map.get(dominant_contract_issue, "")
        if contract_error_type:
            error_type = contract_error_type  # type: ignore[assignment]
            root_cause = str(contract_audit.primary_root_cause or contract_audit.summary or "").strip() or root_cause

        if error_type == "unclassified":
            if "py_package_shadow_root_module" in static_issue_ids:
                error_type = "wrong_import_path"
                root_cause = "本地 Python 包/根模块骨架不一致，导入链指向了不存在的子模块。"
            elif "syntaxerror" in low or "invalid syntax" in low:
                error_type = "syntax_error"
                root_cause = "语法错误导致解释/编译阶段直接失败。"
            elif "circular import" in low or "partially initialized module" in low:
                error_type = "circular_import"
                root_cause = "模块相互导入，解释阶段未完成初始化就访问了符号。"
            elif "py_missing_local_export_symbol" in static_issue_ids or "cannot import name" in low or "has no exported member" in low or "has no attribute" in low:
                # NOTE: `from pkg import submodule` raises "cannot import name" when the submodule file is missing.
                # In that case the correct fix is to add the missing submodule (or correct the import path),
                # not to "re-export" via __init__.py.
                if module and symbol and self._looks_like_local_python_module(module):
                    try:
                        leaf = getattr(self, "_module_leaf_candidate_paths", None)
                        if callable(leaf):
                            sub_candidates = list(leaf(f"{module}.{symbol}"))[:16]
                        else:
                            sub_candidates = list(getattr(self, "_module_candidate_paths")(f"{module}.{symbol}"))[:16]
                            # Filter out parent `__init__.py` files when using the broader candidate set.
                            sub_candidates = [c for c in sub_candidates if re.search(r"/" + re.escape(symbol) + r"(\.py|/__init__\.py)$", c.replace("\\", "/"))]
                        sub_exists = any((self.repo_root / c).exists() for c in sub_candidates)
                    except Exception:
                        sub_exists = False
                    if not sub_exists and re.fullmatch(r"[a-z_][a-z0-9_]*", symbol or ""):
                        error_type = "wrong_import_path"
                        root_cause = f"本地包 `{module}` 下缺少子模块 `{symbol}`（未找到对应 .py 或包目录）。"
                    else:
                        error_type = "missing_export"
                        root_cause = "导入方需要的符号没有从目标模块/包根正确导出。"
                else:
                    error_type = "missing_export"
                    root_cause = "导入方需要的符号没有从目标模块/包根正确导出。"
                if module:
                    rel = module.replace(".", "/").strip("/")
                    inventory: list[str] = []
                    for candidate in [f"{rel}.py", f"{rel}/__init__.py"]:
                        inventory.extend(self._python_symbol_inventory(candidate))
                    if symbol and inventory and symbol not in inventory:
                        match = difflib.get_close_matches(symbol, inventory, n=1, cutoff=0.72)
                        if match:
                            error_type = "symbol_rename"
                            root_cause = f"符号 `{symbol}` 很可能已更名/大小写变化，当前模块中更接近的是 `{match[0]}`。"
            elif "no module named" in low or "module not found" in low or "cannot find module" in low:
                error_type = "missing_import"
                local_exists = self._looks_like_local_python_module(module)
                if local_exists or "py_package_shadow_root_module" in static_issue_ids:
                    error_type = "wrong_import_path"
                    root_cause = "本地模块存在，但 import 路径/包根引用不正确。"
                else:
                    error_type = "config_missing"
                    root_cause = "缺少第三方依赖或环境未安装对应包/CLI。"
            elif "nameerror" in low or ("not defined" in low and any(k in low for k in ["typing", "dict", "list", "any", "optional"])):
                error_type = "typing_runtime_issue"
                root_cause = "运行时/导入时缺少 typing 符号或类型别名定义。"
            elif "assertionerror" in low or re.search(r"(^|\n)\s*e\s+assert\b", text, flags=re.IGNORECASE):
                error_type = "test_assert_mismatch"
                root_cause = "目标行为与测试断言不一致，需核对预期与实现。"
            elif any(
                token in low
                for token in [
                    "command not found",
                    "is not recognized as an internal or external command",
                    "enoent",
                    "eacces",
                    "spawn unknown",
                    "missing config",
                    "no such file or directory",
                ]
            ):
                error_type = "config_missing"
                root_cause = "命令、配置、依赖或运行环境缺失，导致验证无法执行。"

        if static_issue_ids:
            extra = "; ".join(static_summaries[:3])
            tagged = ", ".join([f"StaticIssue: {sid}" for sid in static_issue_ids[:4]])
            root_cause = f"{root_cause} {extra}".strip()
            if tagged:
                root_cause = f"{root_cause} ({tagged})".strip()
            for issue in static_issues[:6]:
                for rel in list(issue.get("files") or [])[:6]:
                    norm = _normalize_scope_pattern(str(rel))
                    if norm and norm not in related_files:
                        related_files.append(norm)

        if contract_issue_ids:
            extra = "; ".join(contract_summaries[:3])
            tagged = ", ".join([f"ContractIssue: {sid}" for sid in contract_issue_ids[:4]])
            root_cause = f"{root_cause} {extra}".strip()
            if tagged:
                root_cause = f"{root_cause} ({tagged})".strip()

        return packs.ErrorObject(
            error_type=error_type,
            module=module,
            symbol=symbol,
            traceback_location=traceback_location,
            suspected_root_cause=root_cause,
            failed_command=failed_cmd,
            related_files=related_files[:24],
            evidence_pointers=evidence_pointers[:24],
            static_issue_ids=static_issue_ids[:8],
            contract_issue_ids=contract_issue_ids[:8],
        )

    def _is_env_fix_candidate(self, *, error: Optional[packs.ErrorObject], blocker_text: str) -> bool:
        low = str(blocker_text or "").lower()
        if error is not None and error.error_type in {
            "wrong_import_path",
            "missing_export",
            "symbol_rename",
            "scope_mismatch",
            "exception_taxonomy_mismatch",
            "engine_interface_mismatch",
            "data_shape_mismatch",
            "contract_drift",
        }:
            return False
        if error is not None and (list(getattr(error, "static_issue_ids", []) or []) or list(getattr(error, "contract_issue_ids", []) or [])):
            return False
        if error is not None and error.error_type == "config_missing":
            return True
        if any(token in low for token in ["spawn unknown", "enoent", "eacces", "command not found", "is not recognized as an internal or external command"]):
            return True
        if any(token in low for token in ["no module named", "cannot find module", "missing dependency"]):
            module = str(getattr(error, "module", "") or "").strip()
            if module and self._looks_like_local_python_module(module):
                return False
            return True
        return False

    def _env_remediation_commands_for_tests(
        self,
        *,
        report: packs.TestReport,
        blocker_text: str,
        error: Optional[packs.ErrorObject],
        envspec_commands: Optional[list[str]] = None,
    ) -> list[str]:
        cmds: list[str] = []
        seen: set[str] = set()
        low = str(blocker_text or "").lower()

        def add(cmd: str) -> None:
            s = str(cmd or "").strip()
            if not s or s in seen:
                return
            seen.add(s)
            cmds.append(s)

        failed_cmd = ""
        try:
            failed_cmd = str(error.failed_command if error is not None else self._failed_command_from_report(report) or "").strip()
        except Exception:
            failed_cmd = ""
        cmd_dir = self._shell_cd_dir(failed_cmd or "")

        py_cmd = bool(re.search(r"\b(?:python|pytest|uvicorn|gunicorn|flask)\b", failed_cmd.lower()))
        node_cmd = bool(re.search(r"\b(?:npm|pnpm|yarn|npx)\b", failed_cmd.lower()))

        if self._python_setup_commands() and (py_cmd or (error is not None and error.error_type == "config_missing")):
            for cmd in self._python_setup_commands()[:2]:
                add(cmd)

        node_dir = cmd_dir
        if node_dir == Path(".") and self._find_node_project_dirs():
            node_dir = self._find_node_project_dirs()[0]
        if node_cmd or any(token in low for token in ["node_modules", ".bin", "npm ", "pnpm ", "yarn ", "hugo", "vite", "eslint", "tsc"]):
            pkg_json = self.repo_root / node_dir / "package.json"
            if pkg_json.exists():
                pm = self._package_manager(node_dir)
                add(self._shell_cmd_in_dir(rel_dir=node_dir, cmd=f"{pm} install"))

        for cmd in list(envspec_commands or [])[:4]:
            lowered = str(cmd or "").lower()
            if any(token in lowered for token in [" install", "pip install", "npm install", "pnpm install", "yarn install"]):
                add(cmd)

        return cmds[:4]

    def _compile_preflight_commands_for_tests(
        self,
        *,
        report: packs.TestReport,
        blocker_text: str,
        error: Optional[packs.ErrorObject],
        focus_commands: list[str],
    ) -> list[str]:
        low = str(blocker_text or "").lower()
        failed_cmd = str(error.failed_command if error is not None else self._failed_command_from_report(report) or "").strip()
        out: list[str] = []
        seen: set[str] = set()

        def add(cmd: str) -> None:
            s = str(cmd or "").strip()
            if not s or s in seen or s in focus_commands or s == failed_cmd:
                return
            seen.add(s)
            out.append(s)

        smoke_cmds = [str(c).strip() for c in self._determine_test_commands(profile="smoke") if str(c).strip()]
        compile_first_types = {
            "missing_import",
            "missing_export",
            "symbol_rename",
            "wrong_import_path",
            "circular_import",
            "typing_runtime_issue",
            "syntax_error",
            "config_missing",
            "exception_taxonomy_mismatch",
            "engine_interface_mismatch",
            "data_shape_mismatch",
            "contract_drift",
        }
        for cmd in smoke_cmds[:4]:
            if error is not None and error.error_type in compile_first_types:
                add(cmd)
            elif any(token in low for token in ["syntaxerror", "error ts", "typescript", "tsc", "compileall"]):
                add(cmd)
        return out[:4]

    def _failure_signature(
        self,
        *,
        report: packs.TestReport,
        extracted: list[str],
        blocker_text: str,
        error: Optional[packs.ErrorObject] = None,
    ) -> str:
        cmd = self._failed_command_from_report(report)
        parts: list[str] = []
        if cmd:
            parts.append("cmd:" + " ".join(cmd.strip().split())[:220].lower())
        sigs = extracted or self._extract_error_signals(blocker_text, limit=10)
        for signal in [str(x or "") for x in sigs[:10]]:
            normalized = " ".join(signal.strip().split()).lower()
            if not normalized:
                continue
            parts.append(normalized[:220])
        if error is not None:
            for sid in list(getattr(error, "static_issue_ids", []) or [])[:4]:
                tag = str(sid or "").strip().lower()
                if tag:
                    parts.append(f"static:{tag}")
            for cid in list(getattr(error, "contract_issue_ids", []) or [])[:4]:
                tag = str(cid or "").strip().lower()
                if tag:
                    parts.append(f"contract:{tag}")
        return "|".join(parts)[:1200]

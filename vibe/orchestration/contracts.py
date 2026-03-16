from __future__ import annotations

import ast
import difflib
import json
import re
from pathlib import Path
from typing import Any, Optional

from vibe.schemas import packs
from vibe.orchestration.shared import _normalize_scope_pattern


class ContractAuditMixin:
    def _looks_like_local_python_module(self, module: str) -> bool:
        mod = str(module or "").strip()
        if not mod or re.search(r"[^A-Za-z0-9_./]", mod):
            return False
        rel = mod.replace(".", "/").strip("/")
        if not rel:
            return False
        return any((self.repo_root / candidate).exists() for candidate in [f"{rel}.py", f"{rel}/__init__.py"])

    def _module_candidate_paths(self, module: str) -> list[str]:
        mod = str(module or "").strip()
        if not mod:
            return []
        rel = mod.replace(".", "/").strip("/")
        if not rel:
            return []
        out: list[str] = []
        for candidate in [f"{rel}.py", f"{rel}/__init__.py"]:
            if candidate not in out:
                out.append(candidate)
        if "/" in rel:
            pkg_parts = rel.split("/")[:-1]
            while pkg_parts:
                init_rel = f"{'/'.join(pkg_parts)}/__init__.py"
                if init_rel not in out:
                    out.append(init_rel)
                pkg_parts = pkg_parts[:-1]
        return out

    def _test_paths_from_text(self, text: str, *, limit: int = 8) -> list[str]:
        raw = str(text or "")
        out: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"\btests[/\\][^\s'\"()]+?\.py(?:::[A-Za-z0-9_:.\\/-]+)?\b", raw, flags=re.IGNORECASE):
            candidate = str(match.group(0) or "").replace("\\", "/").split("::", 1)[0]
            norm = _normalize_scope_pattern(candidate)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
            if len(out) >= limit:
                break
        return out

    def _source_candidates_for_test_path(self, test_path: str, *, limit: int = 8) -> list[str]:
        raw = _normalize_scope_pattern(str(test_path or "").split(":", 1)[0])
        if not raw or not raw.endswith(".py"):
            return []

        name = Path(raw).name
        stems: list[str] = []
        if name.startswith("test_"):
            stems.append(name[len("test_") : -3])
        stems.append(Path(name).stem)

        out: list[str] = []
        seen: set[str] = set()

        def add(path: str) -> None:
            p = _normalize_scope_pattern(path)
            if not p or p in seen:
                return
            if not (self.repo_root / p).exists():
                return
            seen.add(p)
            out.append(p)

        parts = raw.split("/")
        if parts and parts[0] == "tests":
            module_parts = parts[1:-1]
            for stem in stems:
                if stem:
                    add("/".join(["src", *module_parts, f"{stem}.py"]))
                    add("/".join(["src", *module_parts, stem, "__init__.py"]))
                    add("/".join(["src", f"{stem}.py"]))
                    add("/".join(["src", stem, "__init__.py"]))
        for rel in list(getattr(self, "_recent_changed_files")(limit=12) or [])[:12]:
            rel_norm = _normalize_scope_pattern(rel)
            if not rel_norm.endswith(".py"):
                continue
            if any(Path(rel_norm).name == f"{stem}.py" for stem in stems if stem):
                add(rel_norm)
        return out[:limit]

    def _resolve_python_import_module(self, *, importer_rel: str, module: str, level: int) -> str:
        base_parts = [part for part in str(importer_rel or "").replace("\\", "/").split("/") if part]
        if base_parts and base_parts[-1].endswith(".py"):
            base_parts = base_parts[:-1]
        if level > 0:
            up = max(0, len(base_parts) - level)
            base_parts = base_parts[:up]
        mod_parts = [part for part in str(module or "").split(".") if part]
        parts = [part for part in [*base_parts, *mod_parts] if part]
        return ".".join(parts)

    def _python_function_signatures(self, rel_path: str) -> dict[str, dict[str, Any]]:
        rel = _normalize_scope_pattern(rel_path)
        if not rel or not rel.endswith(".py"):
            return {}
        path = self.repo_root / rel
        if not path.exists():
            return {}
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {}
        signatures: dict[str, dict[str, Any]] = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = list(node.args.args or [])
            if positional and positional[0].arg in {"self", "cls"}:
                positional = positional[1:]
            positional_names = [str(arg.arg) for arg in positional]
            signatures[str(node.name)] = {
                "min_positional": max(0, len(positional_names) - len(list(node.args.defaults or []))),
                "max_positional": None if node.args.vararg else len(positional_names),
                "positional_names": positional_names,
                "keyword_names": [str(arg.arg) for arg in list(node.args.kwonlyargs or [])],
                "varargs": bool(node.args.vararg),
                "varkw": bool(node.args.kwarg),
            }
        return signatures

    def _python_symbol_inventory(self, rel_path: str, *, limit: int = 200) -> list[str]:
        rel = _normalize_scope_pattern(rel_path)
        if not rel or not rel.endswith(".py"):
            return []
        path = self.repo_root / rel
        if not path.exists():
            return []
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return []
        out: list[str] = []
        seen: set[str] = set()

        def add(name: str) -> None:
            s = str(name or "").strip()
            if not s or s in seen:
                return
            seen.add(s)
            out.append(s)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                add(node.target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    add(alias.asname or alias.name)
            if len(out) >= limit:
                break
        return out[:limit]

    def _python_class_method_inventory(self, rel_path: str) -> dict[str, set[str]]:
        rel = _normalize_scope_pattern(rel_path)
        path = self.repo_root / rel
        if not rel.endswith(".py") or not path.exists():
            return {}
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {}
        out: dict[str, set[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods: set[str] = set()
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(str(child.name))
            out[str(node.name)] = methods
        return out

    def _python_exception_inventory(self, rel_path: str) -> dict[str, str]:
        rel = _normalize_scope_pattern(rel_path)
        path = self.repo_root / rel
        if not rel.endswith(".py") or not path.exists():
            return {}
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {}
        out: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and (node.name.endswith("Error") or node.name.endswith("Exception")):
                out[str(node.name)] = "class"
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and (target.id.endswith("Error") or target.id.endswith("Exception")):
                        out[str(target.id)] = "alias"
        return out

    def _python_static_skeleton_issues(self, *, observation: dict[str, Any], blocker_text: str) -> list[dict[str, Any]]:
        raw = str(blocker_text or "")
        module = str(observation.get("module") or "").strip()
        symbol = str(observation.get("symbol") or "").strip()
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()

        def add(issue_id: str, summary: str, *, files: list[str], details: str = "") -> None:
            clean_files: list[str] = []
            file_seen: set[str] = set()
            for rel in files:
                norm = _normalize_scope_pattern(rel)
                if not norm or norm in file_seen:
                    continue
                file_seen.add(norm)
                clean_files.append(norm)
            key = (issue_id, tuple(clean_files))
            if key in seen:
                return
            seen.add(key)
            out.append({"id": issue_id, "summary": summary, "files": clean_files[:8], "details": str(details or "").strip()[:500]})

        def existing_module_files(mod_name: str) -> list[str]:
            return [rel for rel in self._module_candidate_paths(mod_name) if (self.repo_root / rel).exists()]

        if module:
            parts = [part for part in module.split(".") if part]
            if len(parts) > 1:
                parent_rel = "/".join(parts[:-1])
                missing_rel = "/".join(parts)
                parent_init = f"{parent_rel}/__init__.py"
                parent_root = f"{parent_rel}.py"
                if (
                    (self.repo_root / parent_init).exists()
                    and (self.repo_root / parent_root).exists()
                    and not any((self.repo_root / rel).exists() for rel in [f"{missing_rel}.py", f"{missing_rel}/__init__.py"])
                ):
                    add(
                        "py_package_shadow_root_module",
                        f"本地包 `{parent_rel}` 与根模块 `{parent_root}` 同名，但 `{missing_rel}` 目标模块不存在。",
                        files=[parent_init, parent_root],
                        details=f"StaticIssue: py_package_shadow_root_module | missing={module}",
                    )

        if module and symbol and self._looks_like_local_python_module(module):
            target_files = existing_module_files(module)
            inventory: list[str] = []
            for candidate in target_files:
                inventory.extend(self._python_symbol_inventory(candidate))
            inventory = list(dict.fromkeys(inventory))
            if inventory and symbol not in inventory:
                match = difflib.get_close_matches(symbol, inventory, n=1, cutoff=0.72)
                detail = f"StaticIssue: py_missing_local_export_symbol | module={module} | symbol={symbol}"
                if match:
                    detail = f"{detail} | closest={match[0]}"
                add(
                    "py_missing_local_export_symbol",
                    f"本地模块 `{module}` 没有导出 `{symbol}`，调用方与真实符号不一致。",
                    files=target_files,
                    details=detail,
                )

        scan_files: list[str] = []
        scan_seen: set[str] = set()
        for rel in list(observation.get("related_files") or []) + self._recent_changed_files(limit=8):
            norm = _normalize_scope_pattern(rel)
            if not norm or not norm.endswith(".py") or norm in scan_seen:
                continue
            if not (self.repo_root / norm).exists():
                continue
            scan_seen.add(norm)
            scan_files.append(norm)
            if len(scan_files) >= 8:
                break

        for rel in scan_files:
            path = self.repo_root / rel
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            imports: list[dict[str, str]] = []
            calls_by_name: dict[str, list[dict[str, Any]]] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    resolved = self._resolve_python_import_module(importer_rel=rel, module=str(node.module or ""), level=int(node.level or 0))
                    if not resolved:
                        continue
                    for alias in node.names:
                        imported_name = str(alias.name or "").strip()
                        if not imported_name or imported_name == "*":
                            continue
                        imports.append({"module": resolved, "symbol": imported_name, "name": str(alias.asname or imported_name)})
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    call_name = str(node.func.id or "").strip()
                    if not call_name:
                        continue
                    calls_by_name.setdefault(call_name, []).append(
                        {
                            "lineno": int(getattr(node, "lineno", 0) or 0),
                            "positional": len(list(node.args or [])),
                            "keywords": [str(kw.arg) for kw in list(node.keywords or []) if getattr(kw, "arg", None)],
                        }
                    )
            for item in imports:
                resolved = str(item.get("module") or "").strip()
                imported_name = str(item.get("symbol") or "").strip()
                if not resolved or not imported_name or not self._looks_like_local_python_module(resolved):
                    continue
                target_files = existing_module_files(resolved)
                if not target_files:
                    continue
                inventory: list[str] = []
                signatures: dict[str, dict[str, Any]] = {}
                for candidate in target_files:
                    inventory.extend(self._python_symbol_inventory(candidate))
                    signatures.update(self._python_function_signatures(candidate))
                inventory = list(dict.fromkeys(inventory))
                if inventory and imported_name not in inventory:
                    match = difflib.get_close_matches(imported_name, inventory, n=1, cutoff=0.72)
                    detail = f"StaticIssue: py_missing_local_export_symbol | importer={rel} | module={resolved} | symbol={imported_name}"
                    if match:
                        detail = f"{detail} | closest={match[0]}"
                    add(
                        "py_missing_local_export_symbol",
                        f"`{rel}` 依赖 `{resolved}.{imported_name}`，但目标模块没有这个导出。",
                        files=[rel, *target_files],
                        details=detail,
                    )
                call_sites = calls_by_name.get(str(item.get("name") or ""), [])
                sig = signatures.get(imported_name)
                if sig is None or not call_sites:
                    continue
                positional_names = [str(x) for x in list(sig.get("positional_names") or [])]
                keyword_names = [str(x) for x in list(sig.get("keyword_names") or [])]
                min_positional = int(sig.get("min_positional") or 0)
                max_positional = sig.get("max_positional")
                for call in call_sites[:6]:
                    positional = int(call.get("positional") or 0)
                    keywords = [str(x) for x in list(call.get("keywords") or []) if str(x)]
                    if positional < min_positional:
                        add(
                            "py_local_call_signature_mismatch",
                            f"`{rel}` 调用 `{resolved}.{imported_name}` 时缺少必需参数。",
                            files=[rel, *target_files],
                            details=(
                                "StaticIssue: py_local_call_signature_mismatch"
                                f" | caller={rel} | target={resolved}.{imported_name} | positional={positional} | min={min_positional}"
                            ),
                        )
                        break
                    if max_positional is not None and positional > int(max_positional):
                        add(
                            "py_local_call_signature_mismatch",
                            f"`{rel}` 传给 `{resolved}.{imported_name}` 的位置参数超过实现签名。",
                            files=[rel, *target_files],
                            details=(
                                "StaticIssue: py_local_call_signature_mismatch"
                                f" | caller={rel} | target={resolved}.{imported_name} | positional={positional} | max={max_positional}"
                            ),
                        )
                        break
                    if keywords and not bool(sig.get("varkw")):
                        unknown = [name for name in keywords if name not in positional_names and name not in keyword_names]
                        if unknown:
                            add(
                                "py_local_call_signature_mismatch",
                                f"`{rel}` 传给 `{resolved}.{imported_name}` 的关键字参数与实现签名不一致。",
                                files=[rel, *target_files],
                                details=(
                                    "StaticIssue: py_local_call_signature_mismatch"
                                    f" | caller={rel} | target={resolved}.{imported_name} | unknown_keywords={','.join(unknown[:4])}"
                                ),
                            )
                            break
        return out[:8]

    def _python_exception_taxonomy_issues(self, *, observation: dict[str, Any]) -> list[dict[str, Any]]:
        related = [str(x).strip() for x in list(observation.get("related_files") or []) if str(x).strip()]
        names_of_interest = {"ValidationError", "FileNotFoundError", "PolicyError", "JSONParseError"}
        definitions: dict[str, list[tuple[str, str]]] = {}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rel in related[:10]:
            inv = self._python_exception_inventory(rel)
            for name, kind in inv.items():
                if name not in names_of_interest:
                    continue
                definitions.setdefault(name, []).append((rel, kind))
        for name, items in definitions.items():
            files = [rel for rel, _kind in items]
            if len(files) > 1 and name not in seen:
                seen.add(name)
                out.append(
                    {
                        "id": "py_exception_taxonomy_split",
                        "issue_type": "exception_taxonomy_mismatch",
                        "summary": f"本地代码对 `{name}` 定义了多套异常来源，测试与实现可能捕获/抛出不同类型。",
                        "files": files[:8],
                        "details": f"ContractIssue: py_exception_taxonomy_split | name={name}",
                        "owner": "integration_engineer",
                    }
                )
            if any(kind == "alias" for _rel, kind in items) and name == "FileNotFoundError":
                out.append(
                    {
                        "id": "py_exception_taxonomy_split",
                        "issue_type": "exception_taxonomy_mismatch",
                        "summary": "检测到本地代码重绑定了 `FileNotFoundError`，这会让内置异常捕获逻辑失真。",
                        "files": files[:8],
                        "details": "ContractIssue: py_exception_taxonomy_split | builtin_shadow=FileNotFoundError",
                        "owner": "coder_backend",
                    }
                )
        return out[:4]

    def _python_data_shape_contract_issues(self, *, observation: dict[str, Any]) -> list[dict[str, Any]]:
        candidate_jsons: list[str] = []
        for folder in ("test_data", "data"):
            base = self.repo_root / folder
            if not base.exists():
                continue
            for path in list(base.rglob("*.json"))[:8]:
                candidate_jsons.append(path.relative_to(self.repo_root).as_posix())
        if not candidate_jsons:
            return []

        issue_files = [str(x).strip() for x in list(observation.get("related_files") or []) if str(x).strip().endswith(".py")]
        issue_files.extend([p for p in self._recent_changed_files(limit=8) if p.endswith(".py")])
        issue_files = list(dict.fromkeys(issue_files))[:10]
        if not issue_files:
            return []

        tracked_fields: set[str] = set()
        for rel in issue_files:
            path = self.repo_root / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"for\s+\w+\s+in\s+\w+\.get\(['\"](?P<field>[A-Za-z_][A-Za-z0-9_]*)['\"],\s*\[\]\)", text):
                tracked_fields.add(str(match.group("field")))

        out: list[dict[str, Any]] = []
        if not tracked_fields:
            return out

        for rel in candidate_jsons[:8]:
            path = self.repo_root / rel
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            entries = data if isinstance(data, list) else data.get("policies") if isinstance(data, dict) else None
            if not isinstance(entries, list) or not entries:
                continue
            sample = entries[0] if isinstance(entries[0], dict) else {}
            if not isinstance(sample, dict):
                continue
            for field in sorted(tracked_fields):
                value = sample.get(field)
                if isinstance(value, dict):
                    out.append(
                        {
                            "id": "py_data_shape_contract_drift",
                            "issue_type": "data_shape_mismatch",
                            "summary": f"样本数据中的 `{field}` 是对象，但模型解析逻辑按列表遍历，数据契约不一致。",
                            "files": [rel, *issue_files[:4]],
                            "details": f"ContractIssue: py_data_shape_contract_drift | field={field}",
                            "owner": "integration_engineer",
                        }
                    )
                    break
            if out:
                break
        return out[:4]

    def _python_engine_interface_issues(self, *, observation: dict[str, Any]) -> list[dict[str, Any]]:
        issue_files = [str(x).strip() for x in list(observation.get("related_files") or []) if str(x).strip().endswith(".py")]
        issue_files.extend([p for p in self._recent_changed_files(limit=8) if p.endswith(".py")])
        issue_files = list(dict.fromkeys(issue_files))[:10]
        out: list[dict[str, Any]] = []

        for rel in issue_files:
            path = self.repo_root / rel
            if not path.exists():
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue

            imported_types: dict[str, tuple[str, str]] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    resolved = self._resolve_python_import_module(importer_rel=rel, module=str(node.module or ""), level=int(node.level or 0))
                    if not resolved or not self._looks_like_local_python_module(resolved):
                        continue
                    for alias in node.names:
                        alias_name = str(alias.asname or alias.name or "").strip()
                        symbol = str(alias.name or "").strip()
                        if alias_name and symbol and symbol[:1].isupper():
                            imported_types[alias_name] = (resolved, symbol)

            function_arg_types: dict[str, str] = {}
            loop_var_types: dict[str, str] = {}
            attr_calls: list[tuple[str, str]] = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in list(node.args.args or []):
                        ann = getattr(arg, "annotation", None)
                        if isinstance(ann, ast.Name) and ann.id in imported_types:
                            function_arg_types[str(arg.arg)] = ann.id
                        elif isinstance(ann, ast.Subscript):
                            inner = getattr(ann, "slice", None)
                            if isinstance(inner, ast.Name) and inner.id in imported_types:
                                function_arg_types[str(arg.arg)] = inner.id
                elif isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and isinstance(node.target, ast.Name):
                    iter_name = str(node.iter.id or "")
                    target_name = str(node.target.id or "")
                    if iter_name in function_arg_types and target_name:
                        loop_var_types[target_name] = function_arg_types[iter_name]
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    var_name = str(node.func.value.id or "")
                    method = str(node.func.attr or "")
                    if var_name and method:
                        attr_calls.append((var_name, method))

            for var_name, method in attr_calls[:40]:
                type_name = loop_var_types.get(var_name) or function_arg_types.get(var_name)
                if not type_name:
                    continue
                resolved, symbol = imported_types.get(type_name, ("", ""))
                if not resolved or not symbol:
                    continue
                method_inventory: set[str] = set()
                target_files = [p for p in self._module_candidate_paths(resolved) if (self.repo_root / p).exists()]
                for candidate in target_files:
                    method_inventory.update(self._python_class_method_inventory(candidate).get(symbol, set()))
                if method_inventory and method not in method_inventory:
                    close = difflib.get_close_matches(method, list(method_inventory), n=1, cutoff=0.72)
                    detail = f"ContractIssue: py_engine_interface_drift | type={resolved}.{symbol} | method={method}"
                    if close:
                        detail = f"{detail} | closest={close[0]}"
                    out.append(
                        {
                            "id": "py_engine_interface_drift",
                            "issue_type": "engine_interface_mismatch",
                            "summary": f"`{rel}` 对 `{resolved}.{symbol}` 调用了不存在的方法 `{method}`，引擎接口前后不一致。",
                            "files": [rel, *target_files],
                            "details": detail,
                            "owner": "integration_engineer",
                        }
                    )
                    break
        return out[:4]

    def _python_contract_audit(self, *, observation: dict[str, Any], blocker_text: str) -> packs.ContractAuditReport:
        static_issues = [x for x in self._python_static_skeleton_issues(observation=observation, blocker_text=blocker_text) if isinstance(x, dict)]
        extra_issues: list[dict[str, Any]] = []
        extra_issues.extend(self._python_exception_taxonomy_issues(observation=observation))
        extra_issues.extend(self._python_engine_interface_issues(observation=observation))
        extra_issues.extend(self._python_data_shape_contract_issues(observation=observation))

        issues: list[packs.ContractIssue] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()
        affected_files: list[str] = []

        def add_files(files: list[str]) -> None:
            for rel in files:
                norm = _normalize_scope_pattern(rel)
                if norm and norm not in affected_files:
                    affected_files.append(norm)

        for raw_issue in [*static_issues, *extra_issues]:
            issue_id = str(raw_issue.get("id") or "").strip()
            files = [_normalize_scope_pattern(p) for p in list(raw_issue.get("files") or []) if _normalize_scope_pattern(p)]
            key = (issue_id, tuple(files))
            if not issue_id or key in seen:
                continue
            seen.add(key)
            add_files(files)
            issue_type_map = {
                "py_package_shadow_root_module": "package_shadow",
                # Missing local submodule is a path/layout issue (treat like package shadow for routing purposes).
                "py_missing_local_submodule": "package_shadow",
                "py_missing_local_export_symbol": "missing_export",
                "py_local_call_signature_mismatch": "call_signature_mismatch",
                "py_exception_taxonomy_split": "exception_taxonomy_mismatch",
                "py_engine_interface_drift": "engine_interface_mismatch",
                "py_data_shape_contract_drift": "data_shape_mismatch",
            }
            issue_type = issue_type_map.get(issue_id, str(raw_issue.get("issue_type") or "contract_drift"))
            issues.append(
                packs.ContractIssue(
                    issue_type=issue_type,  # type: ignore[arg-type]
                    issue_id=issue_id,
                    summary=str(raw_issue.get("summary") or "").strip(),
                    files=files[:8],
                    evidence=[str(raw_issue.get("details") or "").strip()] if str(raw_issue.get("details") or "").strip() else [],
                    suspected_owner=str(raw_issue.get("owner") or ""),
                    confidence="high" if issue_type in {"exception_taxonomy_mismatch", "engine_interface_mismatch", "data_shape_mismatch"} else "medium",
                )
            )

        if not issues:
            return packs.ContractAuditReport(summary="No contract issues detected", issues=[], affected_files=[])

        dominant_issue = issues[0]
        primary_root_cause = dominant_issue.summary
        summary = f"Contract audit found {len(issues)} issue(s); dominant={dominant_issue.issue_id}"
        return packs.ContractAuditReport(
            summary=summary,
            primary_root_cause=primary_root_cause,
            dominant_issue_type=str(dominant_issue.issue_type),
            issues=issues[:8],
            affected_files=affected_files[:16],
            pointers=[],
        )

#!/usr/bin/env python3
"""Native Prime toolchain: locked file scalpel + Camoufox web sensors."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    requests = None  # type: ignore

from tools.scout import camoufox_snapshot


class NativeToolsError(RuntimeError):
    pass


class NativeToolchain:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _canonicalize_raw_path(self, raw_path: str) -> str:
        raw = str(raw_path or "").strip()
        if not raw:
            raise NativeToolsError("path must be non-empty")
        # Normalize separators and trim redundant prefixes to avoid ambiguity.
        raw = raw.replace("\\", "/")
        root_posix = self.root.as_posix().rstrip("/")
        if raw.startswith(root_posix + "/"):
            raw = raw[len(root_posix) + 1 :]
        raw = raw.lstrip("/")
        if raw in {"", "."}:
            raise NativeToolsError("path resolves to locked root, file path required")
        return raw

    def _resolve_locked(self, raw_path: str) -> Path:
        path = Path(self._canonicalize_raw_path(raw_path))
        candidate = path if path.is_absolute() else (self.root / path)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise NativeToolsError(f"Path outside locked root: {raw_path}") from exc
        return resolved

    def _atomic_write(self, target: Path, content: str) -> None:
        tmp = target.with_name(f".{target.name}.tmp")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)

    def file_read(self, *, path: str, start_line: int = 1, end_line: int = 200, max_chars: int = 12000) -> dict[str, Any]:
        target = self._resolve_locked(path)
        if not target.exists() or not target.is_file():
            raise NativeToolsError(f"File not found: {target}")
        if end_line < start_line:
            raise NativeToolsError("end_line must be >= start_line")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        lo = max(1, int(start_line)) - 1
        hi = min(len(lines), int(end_line))
        snippet = "\n".join(lines[lo:hi])
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars]
        return {
            "ok": True,
            "tool": "file_read",
            "path": str(target),
            "start_line": lo + 1,
            "end_line": hi,
            "content": snippet,
            "truncated": len(snippet) >= max_chars,
        }

    def file_write(self, *, path: str, content: str, mode: str = "overwrite") -> dict[str, Any]:
        target = self._resolve_locked(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = str(content)
        if mode == "append" and target.exists():
            current = target.read_text(encoding="utf-8", errors="replace")
            self._atomic_write(target, current + data)
        else:
            self._atomic_write(target, data)
        return {
            "ok": True,
            "tool": "file_write",
            "path": str(target),
            "bytes": len(data.encode("utf-8")),
            "mode": mode,
        }

    def file_edit(
        self,
        *,
        path: str,
        find: str,
        replace: str,
        count: int = 1,
        mode: str = "literal",
        normalize_newlines: bool = True,
    ) -> dict[str, Any]:
        target = self._resolve_locked(path)
        if not target.exists() or not target.is_file():
            raise NativeToolsError(f"File not found: {target}")
        src = target.read_text(encoding="utf-8", errors="replace")
        if not find:
            raise NativeToolsError("find must be non-empty")
        find_text = str(find)
        repl_text = str(replace)
        src_text = src
        if normalize_newlines:
            src_text = src_text.replace("\r\n", "\n")
            find_text = find_text.replace("\r\n", "\n")
            repl_text = repl_text.replace("\r\n", "\n")

        if mode == "regex":
            max_count = 0 if int(count) <= 0 else int(count)
            replaced, replacements = re.subn(find_text, repl_text, src_text, count=max_count, flags=re.MULTILINE)
            occurrences = replacements
        else:
            occurrences = src_text.count(find_text)
            if occurrences == 0:
                raise NativeToolsError("find pattern not found")
            max_count = occurrences if int(count) <= 0 else int(count)
            replaced = src_text.replace(find_text, repl_text, max_count)
            replacements = min(occurrences, max_count)

        if occurrences == 0:
            raise NativeToolsError("find pattern not found")

        self._atomic_write(target, replaced)
        return {
            "ok": True,
            "tool": "file_edit",
            "path": str(target),
            "occurrences": occurrences,
            "replaced": replacements,
            "mode": mode,
        }

    def file_batch_edit(self, *, edits: list[dict[str, Any]]) -> dict[str, Any]:
        if not edits:
            raise NativeToolsError("edits must be non-empty")
        staged: list[tuple[Path, str]] = []
        reports: list[dict[str, Any]] = []

        for idx, edit in enumerate(edits, start=1):
            path = str(edit.get("path") or "")
            find = str(edit.get("find") or "")
            replace = str(edit.get("replace") or "")
            count = int(edit.get("count", 1))
            mode = str(edit.get("mode", "literal"))
            normalize_newlines = bool(edit.get("normalize_newlines", True))

            target = self._resolve_locked(path)
            if not target.exists() or not target.is_file():
                raise NativeToolsError(f"batch edit #{idx}: file not found: {target}")
            src = target.read_text(encoding="utf-8", errors="replace")
            if not find:
                raise NativeToolsError(f"batch edit #{idx}: find must be non-empty")

            src_text = src.replace("\r\n", "\n") if normalize_newlines else src
            find_text = find.replace("\r\n", "\n") if normalize_newlines else find
            repl_text = replace.replace("\r\n", "\n") if normalize_newlines else replace

            if mode == "regex":
                max_count = 0 if count <= 0 else count
                replaced, replacements = re.subn(find_text, repl_text, src_text, count=max_count, flags=re.MULTILINE)
                occurrences = replacements
            else:
                occurrences = src_text.count(find_text)
                if occurrences == 0:
                    raise NativeToolsError(f"batch edit #{idx}: find pattern not found")
                max_count = occurrences if count <= 0 else count
                replaced = src_text.replace(find_text, repl_text, max_count)
                replacements = min(occurrences, max_count)

            if occurrences == 0:
                raise NativeToolsError(f"batch edit #{idx}: find pattern not found")

            staged.append((target, replaced))
            reports.append(
                {
                    "path": str(target),
                    "occurrences": occurrences,
                    "replaced": replacements,
                    "mode": mode,
                }
            )

        # Commit phase after all edits validate.
        for target, content in staged:
            self._atomic_write(target, content)

        return {
            "ok": True,
            "tool": "file_batch_edit",
            "files_touched": len(staged),
            "reports": reports,
        }

    def web_sensor(self, *, url: str, mode: str = "markdown", max_chars: int = 7000) -> dict[str, Any]:
        if mode not in {"markdown", "a11y"}:
            raise NativeToolsError("mode must be markdown or a11y")
        snap = camoufox_snapshot(url=url, mode=mode, max_chars=max_chars)
        return {
            "ok": True,
            "tool": "web_sensor",
            "engine": "camoufox",
            "url": snap["url"],
            "title": snap["title"],
            "mode": snap["mode"],
            "snapshot": snap["snapshot"],
            "chars": snap["chars"],
        }

    def shell(self, *, command: str, timeout: int = 30, cwd: str | None = None) -> dict[str, Any]:
        """Execute shell command with timeout and output capture."""
        cmd = str(command or "").strip()
        if not cmd:
            raise NativeToolsError("command must be non-empty")
        
        work_dir = self.root
        if cwd:
            work_dir = self._resolve_locked(cwd).parent if self._resolve_locked(cwd).is_file() else self._resolve_locked(cwd)
        
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(work_dir),
            )
            return {
                "ok": result.returncode == 0,
                "tool": "shell",
                "command": cmd,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            raise NativeToolsError(f"Command timeout after {timeout}s")
        except Exception as exc:
            raise NativeToolsError(f"Shell execution failed: {exc}")

    def memory_store(self, *, key: str, value: str, namespace: str = "default") -> dict[str, Any]:
        """Store key-value pair in persistent memory."""
        mem_dir = self.root / "memory" / namespace
        mem_dir.mkdir(parents=True, exist_ok=True)
        
        key_safe = re.sub(r'[^\w\-_]', '_', str(key))
        mem_file = mem_dir / f"{key_safe}.json"
        
        data = {
            "key": key,
            "value": str(value),
            "namespace": namespace,
            "timestamp": time.time(),
        }
        
        self._atomic_write(mem_file.with_suffix('.json'), json.dumps(data, indent=2))
        
        return {
            "ok": True,
            "tool": "memory_store",
            "key": key,
            "namespace": namespace,
            "bytes": len(str(value)),
        }

    def memory_recall(self, *, key: str, namespace: str = "default") -> dict[str, Any]:
        """Recall stored value by key."""
        mem_dir = self.root / "memory" / namespace
        key_safe = re.sub(r'[^\w\-_]', '_', str(key))
        mem_file = mem_dir / f"{key_safe}.json"
        
        if not mem_file.exists():
            raise NativeToolsError(f"Memory key not found: {key}")
        
        data = json.loads(mem_file.read_text())
        
        return {
            "ok": True,
            "tool": "memory_recall",
            "key": key,
            "value": data.get("value", ""),
            "namespace": namespace,
            "timestamp": data.get("timestamp", 0),
        }

    def memory_forget(self, *, key: str, namespace: str = "default") -> dict[str, Any]:
        """Delete stored memory by key."""
        mem_dir = self.root / "memory" / namespace
        key_safe = re.sub(r'[^\w\-_]', '_', str(key))
        mem_file = mem_dir / f"{key_safe}.json"
        
        if mem_file.exists():
            mem_file.unlink()
            deleted = True
        else:
            deleted = False
        
        return {
            "ok": True,
            "tool": "memory_forget",
            "key": key,
            "namespace": namespace,
            "deleted": deleted,
        }

    def memory_list(self, *, namespace: str = "default", limit: int = 100) -> dict[str, Any]:
        """List all memory keys in namespace."""
        mem_dir = self.root / "memory" / namespace
        
        if not mem_dir.exists():
            return {
                "ok": True,
                "tool": "memory_list",
                "namespace": namespace,
                "keys": [],
                "count": 0,
            }
        
        keys = []
        for mem_file in sorted(mem_dir.glob("*.json"))[:limit]:
            try:
                data = json.loads(mem_file.read_text())
                keys.append({
                    "key": data.get("key", mem_file.stem),
                    "timestamp": data.get("timestamp", 0),
                })
            except Exception:
                continue
        
        return {
            "ok": True,
            "tool": "memory_list",
            "namespace": namespace,
            "keys": keys,
            "count": len(keys),
        }

    def browser_open(self, *, url: str, allowlist: list[str] | None = None) -> dict[str, Any]:
        """Open URL in system browser (allowlist enforced)."""
        url_clean = str(url or "").strip()
        if not url_clean.startswith(("http://", "https://")):
            raise NativeToolsError("URL must start with http:// or https://")
        
        # Default allowlist includes common safe domains
        default_allowlist = [
            "github.com", "stackoverflow.com", "python.org", "wikipedia.org",
            "docs.python.org", "readthedocs.io", "arxiv.org", "localhost"
        ]
        
        allowed = allowlist if allowlist is not None else default_allowlist
        
        # Check if URL domain is in allowlist
        from urllib.parse import urlparse
        domain = urlparse(url_clean).netloc
        domain_allowed = any(allowed_domain in domain for allowed_domain in allowed)
        
        if not domain_allowed:
            raise NativeToolsError(f"Domain not in allowlist: {domain}")
        
        try:
            webbrowser.open(url_clean)
            return {
                "ok": True,
                "tool": "browser_open",
                "url": url_clean,
                "domain": domain,
            }
        except Exception as exc:
            raise NativeToolsError(f"Browser open failed: {exc}")

    def content_search(self, *, query: str, limit: int = 10, engine: str = "duckduckgo") -> dict[str, Any]:
        """Search the web using DuckDuckGo."""
        if not requests:
            raise NativeToolsError("requests library not available for web search")
        
        query_clean = str(query or "").strip()
        if not query_clean:
            raise NativeToolsError("query must be non-empty")
        
        if engine != "duckduckgo":
            raise NativeToolsError("Only duckduckgo engine supported")
        
        try:
            # DuckDuckGo Instant Answer API
            params = {
                "q": query_clean,
                "format": "json",
                "no_html": 1,
                "skip_disambig": 1,
            }
            
            response = requests.get(
                "https://api.duckduckgo.com/",
                params=params,
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            
            results = []
            
            # Abstract
            if data.get("Abstract"):
                results.append({
                    "title": data.get("Heading", ""),
                    "snippet": data.get("Abstract", ""),
                    "url": data.get("AbstractURL", ""),
                })
            
            # Related topics
            for topic in data.get("RelatedTopics", [])[:limit]:
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append({
                        "title": topic.get("Text", "")[:100],
                        "snippet": topic.get("Text", ""),
                        "url": topic.get("FirstURL", ""),
                    })
            
            return {
                "ok": True,
                "tool": "content_search",
                "query": query_clean,
                "engine": engine,
                "results": results[:limit],
                "count": len(results[:limit]),
            }
        except Exception as exc:
            raise NativeToolsError(f"Search failed: {exc}")

    def schedule(self, *, action: str, task_id: str = "", command: str = "", schedule_time: str = "") -> dict[str, Any]:
        """Manage scheduled tasks via agentic_cron."""
        cron_state_file = self.root / "bin" / "cron_tasks.json"
        cron_state_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Load existing tasks
        if cron_state_file.exists():
            tasks = json.loads(cron_state_file.read_text())
        else:
            tasks = []
        
        if action == "create":
            if not command:
                raise NativeToolsError("command required for create action")
            
            task = {
                "id": task_id or f"task_{int(time.time())}",
                "command": command,
                "schedule": schedule_time,
                "created": time.time(),
                "status": "pending",
            }
            tasks.append(task)
            self._atomic_write(cron_state_file, json.dumps(tasks, indent=2))
            
            return {
                "ok": True,
                "tool": "schedule",
                "action": "create",
                "task_id": task["id"],
            }
        
        elif action == "list":
            return {
                "ok": True,
                "tool": "schedule",
                "action": "list",
                "tasks": tasks,
                "count": len(tasks),
            }
        
        elif action == "cancel":
            if not task_id:
                raise NativeToolsError("task_id required for cancel action")
            
            tasks = [t for t in tasks if t.get("id") != task_id]
            self._atomic_write(cron_state_file, json.dumps(tasks, indent=2))
            
            return {
                "ok": True,
                "tool": "schedule",
                "action": "cancel",
                "task_id": task_id,
            }
        
        else:
            raise NativeToolsError(f"Unknown action: {action}")

    def pushover(self, *, message: str, title: str = "", priority: int = 0) -> dict[str, Any]:
        """Send push notification (requires PUSHOVER_TOKEN and PUSHOVER_USER in env)."""
        if not requests:
            raise NativeToolsError("requests library not available for pushover")
        
        token = os.environ.get("PUSHOVER_TOKEN", "")
        user = os.environ.get("PUSHOVER_USER", "")
        
        if not token or not user:
            raise NativeToolsError("PUSHOVER_TOKEN and PUSHOVER_USER environment variables required")
        
        message_clean = str(message or "").strip()
        if not message_clean:
            raise NativeToolsError("message must be non-empty")
        
        try:
            response = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": token,
                    "user": user,
                    "message": message_clean,
                    "title": title or "Gator Notification",
                    "priority": priority,
                },
                timeout=10,
            )
            response.raise_for_status()
            
            return {
                "ok": True,
                "tool": "pushover",
                "message": message_clean,
                "status": response.json().get("status", 0),
            }
        except Exception as exc:
            raise NativeToolsError(f"Pushover notification failed: {exc}")

    def http_request(
        self,
        *,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: str | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Make HTTP request."""
        if not requests:
            raise NativeToolsError("requests library not available for HTTP requests")
        
        url_clean = str(url or "").strip()
        if not url_clean.startswith(("http://", "https://")):
            raise NativeToolsError("URL must start with http:// or https://")
        
        method_clean = str(method or "GET").upper()
        if method_clean not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}:
            raise NativeToolsError(f"Unsupported HTTP method: {method_clean}")
        
        try:
            response = requests.request(
                method=method_clean,
                url=url_clean,
                headers=headers or {},
                data=data,
                timeout=timeout,
            )
            
            # Try to parse as JSON, fallback to text
            try:
                content = response.json()
                content_type = "json"
            except Exception:
                content = response.text[:10000]  # Limit text response
                content_type = "text"
            
            return {
                "ok": response.status_code < 400,
                "tool": "http_request",
                "url": url_clean,
                "method": method_clean,
                "status_code": response.status_code,
                "content_type": content_type,
                "content": content,
                "headers": dict(response.headers),
            }
        except Exception as exc:
            raise NativeToolsError(f"HTTP request failed: {exc}")

    def execute(self, *, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = str(tool or "").strip().lower()
        args = dict(args or {})
        if tool == "file_read":
            return self.file_read(**args)
        if tool == "file_write":
            return self.file_write(**args)
        if tool == "file_edit":
            return self.file_edit(**args)
        if tool == "file_batch_edit":
            return self.file_batch_edit(**args)
        if tool in {"web_sensor", "camoufox_web"}:
            return self.web_sensor(**args)
        if tool == "shell":
            return self.shell(**args)
        if tool == "memory_store":
            return self.memory_store(**args)
        if tool == "memory_recall":
            return self.memory_recall(**args)
        if tool == "memory_forget":
            return self.memory_forget(**args)
        if tool == "memory_list":
            return self.memory_list(**args)
        if tool == "browser_open":
            return self.browser_open(**args)
        if tool == "content_search":
            return self.content_search(**args)
        if tool == "schedule":
            return self.schedule(**args)
        if tool == "pushover":
            return self.pushover(**args)
        if tool == "http_request":
            return self.http_request(**args)
        raise NativeToolsError(f"Unsupported tool: {tool}")

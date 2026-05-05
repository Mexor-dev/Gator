#!/usr/bin/env python3
"""Native Prime toolchain: locked file scalpel + Camoufox web sensors."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.scout import camoufox_snapshot


class NativeToolsError(RuntimeError):
    pass


class NativeToolchain:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _resolve_locked(self, raw_path: str) -> Path:
        path = Path(raw_path)
        candidate = path if path.is_absolute() else (self.root / path)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise NativeToolsError(f"Path outside locked root: {raw_path}") from exc
        return resolved

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
            target.write_text(target.read_text(encoding="utf-8", errors="replace") + data, encoding="utf-8")
        else:
            target.write_text(data, encoding="utf-8")
        return {
            "ok": True,
            "tool": "file_write",
            "path": str(target),
            "bytes": len(data.encode("utf-8")),
            "mode": mode,
        }

    def file_edit(self, *, path: str, find: str, replace: str, count: int = 1) -> dict[str, Any]:
        target = self._resolve_locked(path)
        if not target.exists() or not target.is_file():
            raise NativeToolsError(f"File not found: {target}")
        src = target.read_text(encoding="utf-8", errors="replace")
        if not find:
            raise NativeToolsError("find must be non-empty")
        occurrences = src.count(find)
        if occurrences == 0:
            raise NativeToolsError("find pattern not found")
        replaced = src.replace(find, replace, max(1, int(count)))
        target.write_text(replaced, encoding="utf-8")
        return {
            "ok": True,
            "tool": "file_edit",
            "path": str(target),
            "occurrences": occurrences,
            "replaced": min(occurrences, max(1, int(count))),
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

    def execute(self, *, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = str(tool or "").strip().lower()
        args = dict(args or {})
        if tool == "file_read":
            return self.file_read(**args)
        if tool == "file_write":
            return self.file_write(**args)
        if tool == "file_edit":
            return self.file_edit(**args)
        if tool in {"web_sensor", "camoufox_web"}:
            return self.web_sensor(**args)
        raise NativeToolsError(f"Unsupported tool: {tool}")

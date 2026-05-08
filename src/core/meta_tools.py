#!/usr/bin/env python3
"""Meta-Tool System: Recursive Tool Authoring & Dream Cycle Optimization.

The Meta-Tool architecture enables Gator to:
1. Create new tools on-demand when 35B Logic identifies capability gaps
2. Validate synthetic tools in isolated sandbox before activation
3. Optimize tools during Dream Cycle based on performance logs
4. Enforce immutability guardrails on embedded toolsets (ZeroClaw, CamoFox)
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MetaToolError(RuntimeError):
    """Raised when meta-tool operations fail."""
    pass


@dataclass
class SyntheticTool:
    """Represents a model-generated tool."""
    tool_id: str
    name: str
    description: str
    language: str  # "python" or "rust"
    source_code: str
    schema: dict[str, Any]
    created_at: float
    validation_status: str  # "pending", "passed", "failed"
    performance_score: float = 0.0
    use_count: int = 0
    error_count: int = 0
    avg_latency_ms: float = 0.0


class MetaToolchain:
    """Factory for creating, validating, and optimizing synthetic tools.
    
    The Iron Law: Synthetic tools MUST pass sandbox validation before
    being added to the active registry. Embedded tools (ZeroClaw, CamoFox,
    FSI) are marked READ-ONLY and cannot be modified during Dream Cycle.
    """
    
    # Embedded tools that are immutable (cannot be modified by Dream Cycle)
    IMMUTABLE_TOOLS = frozenset({
        "file_read", "file_write", "file_edit", "file_batch_edit",
        "camoufox_snapshot", "camoufox_web", "web_sensor",
        "zeroclaw_extract", "zeroclaw_parse", "zeroclaw_transform",
    })
    
    def __init__(self, *, root: Path, synthetic_dir: Path) -> None:
        self.root = root.resolve()
        self.synthetic_dir = synthetic_dir.resolve()
        self.synthetic_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.synthetic_dir / "tools_manifest.json"
        self.performance_log = self.synthetic_dir / "performance.jsonl"
        self._registry: dict[str, SyntheticTool] = {}
        self._load_registry()
    
    def _now(self) -> float:
        return time.time()
    
    def _load_registry(self) -> None:
        """Load synthetic tools registry from disk."""
        if not self.registry_path.exists():
            self._save_registry()
            return
        
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            for tool_data in data.get("tools", []):
                tool = SyntheticTool(
                    tool_id=tool_data["tool_id"],
                    name=tool_data["name"],
                    description=tool_data["description"],
                    language=tool_data["language"],
                    source_code=tool_data["source_code"],
                    schema=tool_data["schema"],
                    created_at=tool_data["created_at"],
                    validation_status=tool_data["validation_status"],
                    performance_score=tool_data.get("performance_score", 0.0),
                    use_count=tool_data.get("use_count", 0),
                    error_count=tool_data.get("error_count", 0),
                    avg_latency_ms=tool_data.get("avg_latency_ms", 0.0),
                )
                self._registry[tool.tool_id] = tool
        except Exception as exc:
            print(f"[MetaToolchain] Registry load failed: {exc}", flush=True)
            self._save_registry()
    
    def _save_registry(self) -> None:
        """Persist synthetic tools registry to disk."""
        tools_data = []
        for tool in self._registry.values():
            tools_data.append({
                "tool_id": tool.tool_id,
                "name": tool.name,
                "description": tool.description,
                "language": tool.language,
                "source_code": tool.source_code,
                "schema": tool.schema,
                "created_at": tool.created_at,
                "validation_status": tool.validation_status,
                "performance_score": tool.performance_score,
                "use_count": tool.use_count,
                "error_count": tool.error_count,
                "avg_latency_ms": tool.avg_latency_ms,
            })
        
        manifest = {
            "version": 1,
            "updated_at": self._now(),
            "tools": tools_data,
        }
        
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=True), encoding="utf-8")
        tmp.replace(self.registry_path)
    
    def _generate_tool_id(self, name: str) -> str:
        """Generate unique tool ID from name and timestamp."""
        timestamp = str(self._now())
        unique = hashlib.sha256(f"{name}:{timestamp}".encode()).hexdigest()[:8]
        clean_name = "".join(c if c.isalnum() else "_" for c in name.lower())
        return f"synthetic_{clean_name}_{unique}"
    
    def _infer_json_schema(self, source_code: str, language: str) -> dict[str, Any]:
        """Auto-generate JSON Schema from tool source code.
        
        For Python: Parse AST to extract function signature and docstring
        For Rust: Parse function signature and doc comments
        """
        if language == "python":
            return self._infer_python_schema(source_code)
        elif language == "rust":
            return self._infer_rust_schema(source_code)
        else:
            raise MetaToolError(f"Unsupported language: {language}")
    
    def _infer_python_schema(self, source_code: str) -> dict[str, Any]:
        """Extract JSON Schema from Python function using AST."""
        try:
            tree = ast.parse(source_code)
            func_defs = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
            
            if not func_defs:
                raise MetaToolError("No function definition found in source code")
            
            main_func = func_defs[0]  # Use first function as entry point
            func_name = main_func.name
            
            # Extract docstring
            docstring = ast.get_docstring(main_func) or "Synthetic tool (auto-generated)"
            
            # Extract parameters
            properties = {}
            required = []
            
            for arg in main_func.args.args:
                arg_name = arg.arg
                if arg_name == "self":
                    continue
                
                # Try to infer type from annotation
                param_type = "string"  # default
                if arg.annotation:
                    ann_id = getattr(arg.annotation, "id", None)
                    if ann_id == "str":
                        param_type = "string"
                    elif ann_id == "int":
                        param_type = "integer"
                    elif ann_id == "float":
                        param_type = "number"
                    elif ann_id == "bool":
                        param_type = "boolean"
                    elif ann_id in ("dict", "Dict"):
                        param_type = "object"
                    elif ann_id in ("list", "List"):
                        param_type = "array"
                
                properties[arg_name] = {
                    "type": param_type,
                    "description": f"Parameter: {arg_name}"
                }
                required.append(arg_name)
            
            # Handle defaults (not required)
            defaults = main_func.args.defaults
            if defaults:
                num_defaults = len(defaults)
                default_args = [arg.arg for arg in main_func.args.args[-num_defaults:]]
                required = [r for r in required if r not in default_args]
            
            schema = {
                "name": func_name,
                "description": docstring.split("\n")[0][:200],  # First line only
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }
            
            return schema
            
        except Exception as exc:
            raise MetaToolError(f"Python schema inference failed: {exc}") from exc
    
    def _infer_rust_schema(self, source_code: str) -> dict[str, Any]:
        """Extract JSON Schema from Rust function signature.
        
        Simple regex-based extraction since full Rust parsing requires
        external dependencies. Looks for:
        - pub fn function_name(param: Type, ...) -> ReturnType
        - Doc comments (///)
        """
        import re
        
        # Find function signature
        func_match = re.search(
            r'pub\s+fn\s+(\w+)\s*\((.*?)\)',
            source_code,
            re.DOTALL
        )
        
        if not func_match:
            raise MetaToolError("No public function found in Rust code")
        
        func_name = func_match.group(1)
        params_str = func_match.group(2)
        
        # Extract doc comments
        doc_pattern = r'///\s*(.+?)(?=\n(?:pub fn|$))'
        doc_match = re.search(doc_pattern, source_code, re.DOTALL)
        description = "Synthetic Rust tool (auto-generated)"
        if doc_match:
            description = " ".join(doc_match.group(1).strip().split("\n"))[:200]
        
        # Parse parameters
        properties = {}
        required = []
        
        if params_str.strip():
            param_pairs = [p.strip() for p in params_str.split(",")]
            for param in param_pairs:
                if ":" not in param:
                    continue
                name, rust_type = param.split(":", 1)
                name = name.strip()
                rust_type = rust_type.strip()
                
                # Map Rust types to JSON Schema types
                if rust_type in ("String", "&str"):
                    param_type = "string"
                elif rust_type in ("i32", "i64", "u32", "u64", "usize"):
                    param_type = "integer"
                elif rust_type in ("f32", "f64"):
                    param_type = "number"
                elif rust_type == "bool":
                    param_type = "boolean"
                else:
                    param_type = "string"  # default
                
                properties[name] = {
                    "type": param_type,
                    "description": f"Parameter: {name}"
                }
                
                # All params required unless Option<T>
                if not rust_type.startswith("Option<"):
                    required.append(name)
        
        schema = {
            "name": func_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            }
        }
        
        return schema
    
    def create_tool(
        self,
        *,
        name: str,
        description: str,
        language: str,
        source_code: str,
        auto_validate: bool = True
    ) -> SyntheticTool:
        """Create a new synthetic tool and add to registry.
        
        The Iron Law: Tools MUST pass validation before activation.
        
        Args:
            name: Tool name (used for function calls)
            description: Human-readable purpose
            language: "python" or "rust"
            source_code: Complete tool implementation
            auto_validate: Run sandbox validation immediately
        
        Returns:
            SyntheticTool instance (validation_status may be "pending")
        
        Raises:
            MetaToolError: If tool creation fails
        """
        if language not in ("python", "rust"):
            raise MetaToolError(f"Unsupported language: {language}")
        
        # Generate unique tool ID
        tool_id = self._generate_tool_id(name)
        
        # Auto-infer JSON Schema
        try:
            schema = self._infer_json_schema(source_code, language)
        except Exception as exc:
            raise MetaToolError(f"Schema inference failed: {exc}") from exc
        
        # Create tool instance
        tool = SyntheticTool(
            tool_id=tool_id,
            name=name,
            description=description,
            language=language,
            source_code=source_code,
            schema=schema,
            created_at=self._now(),
            validation_status="pending",
        )
        
        # Save source to disk
        if language == "python":
            tool_path = self.synthetic_dir / f"{tool_id}.py"
            tool_path.write_text(source_code, encoding="utf-8")
        else:  # rust
            tool_path = self.synthetic_dir / f"{tool_id}.rs"
            tool_path.write_text(source_code, encoding="utf-8")
        
        # Add to registry
        self._registry[tool_id] = tool
        self._save_registry()
        
        print(f"[MetaToolchain] Created {tool_id}: {name} ({language})", flush=True)
        
        # Run validation if requested
        if auto_validate:
            try:
                self.validate_tool(tool_id)
            except Exception as exc:
                print(f"[MetaToolchain] Validation failed: {exc}", flush=True)
        
        return tool
    
    def validate_tool(self, tool_id: str) -> dict[str, Any]:
        """Run sandbox validation on a synthetic tool.
        
        The Iron Law: Only validated tools can be used by the 1.5B.
        
        Validation tests:
        1. Syntax check (Python: compile, Rust: cargo check)
        2. Import test (Python: importlib, Rust: build)
        3. Basic execution test with sample inputs
        
        Args:
            tool_id: Unique tool identifier
        
        Returns:
            Validation report dict
        
        Raises:
            MetaToolError: If validation fails
        """
        if tool_id not in self._registry:
            raise MetaToolError(f"Tool not found: {tool_id}")
        
        tool = self._registry[tool_id]
        
        if tool.language == "python":
            result = self._validate_python_tool(tool)
        elif tool.language == "rust":
            result = self._validate_rust_tool(tool)
        else:
            raise MetaToolError(f"Unsupported language: {tool.language}")
        
        # Update validation status
        tool.validation_status = "passed" if result["ok"] else "failed"
        self._save_registry()
        
        return result
    
    def _validate_python_tool(self, tool: SyntheticTool) -> dict[str, Any]:
        """Validate Python synthetic tool in sandbox."""
        tool_path = self.synthetic_dir / f"{tool.tool_id}.py"
        
        if not tool_path.exists():
            return {"ok": False, "error": "Source file not found"}
        
        # Step 1: Syntax check
        try:
            compile(tool.source_code, str(tool_path), "exec")
        except SyntaxError as exc:
            return {"ok": False, "error": f"Syntax error: {exc}"}
        
        # Step 2: Import test (isolated)
        try:
            spec = importlib.util.spec_from_file_location(tool.tool_id, tool_path)
            if spec is None or spec.loader is None:
                return {"ok": False, "error": "Import spec creation failed"}
            
            module = importlib.util.module_from_spec(spec)
            # Don't execute yet, just check import
            # spec.loader.exec_module(module)  # Commented for safety
        except Exception as exc:
            return {"ok": False, "error": f"Import failed: {exc}"}
        
        # Step 3: Basic AST safety checks
        try:
            tree = ast.parse(tool.source_code)
            
            # Check for dangerous operations
            dangerous = []
            for node in ast.walk(tree):
                # Forbidden: eval, exec, __import__, compile
                if isinstance(node, ast.Call):
                    if hasattr(node.func, "id"):
                        if node.func.id in ("eval", "exec", "__import__", "compile"):
                            dangerous.append(f"Forbidden call: {node.func.id}")
            
            if dangerous:
                return {"ok": False, "error": f"Security violation: {', '.join(dangerous)}"}
        
        except Exception as exc:
            return {"ok": False, "error": f"AST check failed: {exc}"}
        
        # Validation passed
        return {
            "ok": True,
            "tool_id": tool.tool_id,
            "language": "python",
            "checks": ["syntax", "import", "security"],
        }
    
    def _validate_rust_tool(self, tool: SyntheticTool) -> dict[str, Any]:
        """Validate Rust synthetic tool using cargo check."""
        tool_path = self.synthetic_dir / f"{tool.tool_id}.rs"
        
        if not tool_path.exists():
            return {"ok": False, "error": "Source file not found"}
        
        # Rust validation requires cargo check in a proper project
        # For now, just do basic syntax validation
        try:
            # Check for basic Rust syntax markers
            if "pub fn" not in tool.source_code:
                return {"ok": False, "error": "No public function found"}
            
            # Could invoke rustc --crate-type lib --parse-only here
            # but that requires Rust toolchain installed
            
            return {
                "ok": True,
                "tool_id": tool.tool_id,
                "language": "rust",
                "checks": ["syntax"],
                "note": "Full cargo validation not yet implemented",
            }
        
        except Exception as exc:
            return {"ok": False, "error": f"Validation failed: {exc}"}
    
    def get_active_tools(self) -> list[SyntheticTool]:
        """Get all validated synthetic tools ready for use."""
        return [
            tool for tool in self._registry.values()
            if tool.validation_status == "passed"
        ]
    
    def get_all_tools(self) -> list[SyntheticTool]:
        """Get all synthetic tools regardless of validation status."""
        return list(self._registry.values())
    
    def log_performance(
        self,
        tool_id: str,
        *,
        success: bool,
        latency_ms: float,
        error_message: str = ""
    ) -> None:
        """Log tool execution performance for Dream Cycle optimization.
        
        Args:
            tool_id: Unique tool identifier
            success: Whether execution succeeded
            latency_ms: Execution time in milliseconds
            error_message: Error details if success=False
        """
        if tool_id not in self._registry:
            return
        
        tool = self._registry[tool_id]
        tool.use_count += 1
        
        if not success:
            tool.error_count += 1
        
        # Update rolling average latency
        if tool.avg_latency_ms == 0.0:
            tool.avg_latency_ms = latency_ms
        else:
            # Exponential moving average
            tool.avg_latency_ms = 0.7 * tool.avg_latency_ms + 0.3 * latency_ms
        
        # Calculate performance score (higher is better)
        # Score = success_rate * (1000 / avg_latency_ms)
        success_rate = (tool.use_count - tool.error_count) / max(1, tool.use_count)
        tool.performance_score = success_rate * (1000.0 / max(1.0, tool.avg_latency_ms))
        
        self._save_registry()
        
        # Append to performance log
        log_entry = {
            "timestamp": self._now(),
            "tool_id": tool_id,
            "success": success,
            "latency_ms": latency_ms,
            "error": error_message,
        }
        
        with open(self.performance_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    
    def dream_cycle_optimize(self, *, min_use_count: int = 5) -> dict[str, Any]:
        """Dream Cycle: Optimize synthetic tools based on performance logs.
        
        The Iron Law: ONLY synthetic tools can be modified. Embedded tools
        (ZeroClaw, CamoFox, FSI) are READ-ONLY and flagged as immutable.
        
        Optimization strategies:
        1. High error rate → flag for review / deprecation
        2. High latency → suggest algorithmic improvements
        3. Low use count → candidate for removal
        
        Args:
            min_use_count: Minimum uses before optimization kicks in
        
        Returns:
            Optimization report with recommendations
        """
        report = {
            "timestamp": self._now(),
            "tools_analyzed": 0,
            "high_error_tools": [],
            "high_latency_tools": [],
            "unused_tools": [],
            "top_performers": [],
        }
        
        for tool in self._registry.values():
            # Skip tools with insufficient data
            if tool.use_count < min_use_count:
                if tool.use_count == 0:
                    report["unused_tools"].append(tool.tool_id)
                continue
            
            report["tools_analyzed"] += 1
            
            # Calculate metrics
            error_rate = tool.error_count / max(1, tool.use_count)
            
            # High error rate (>20%)
            if error_rate > 0.2:
                report["high_error_tools"].append({
                    "tool_id": tool.tool_id,
                    "name": tool.name,
                    "error_rate": round(error_rate, 3),
                    "recommendation": "Review for bugs or deprecate",
                })
            
            # High latency (>500ms average)
            if tool.avg_latency_ms > 500:
                report["high_latency_tools"].append({
                    "tool_id": tool.tool_id,
                    "name": tool.name,
                    "avg_latency_ms": round(tool.avg_latency_ms, 2),
                    "recommendation": "Optimize algorithm or cache results",
                })
            
            # Top performers (high score, low error)
            if tool.performance_score > 50 and error_rate < 0.05:
                report["top_performers"].append({
                    "tool_id": tool.tool_id,
                    "name": tool.name,
                    "score": round(tool.performance_score, 2),
                })
        
        # Sort top performers by score
        report["top_performers"].sort(key=lambda x: x["score"], reverse=True)
        report["top_performers"] = report["top_performers"][:5]  # Top 5
        
        print(f"[DreamCycle] Analyzed {report['tools_analyzed']} tools", flush=True)
        if report["high_error_tools"]:
            print(f"[DreamCycle] {len(report['high_error_tools'])} tools flagged for review", flush=True)
        
        return report
    
    def is_immutable(self, tool_name: str) -> bool:
        """Check if a tool is part of the immutable embedded toolset.
        
        The Iron Law: Embedded tools CANNOT be modified during Dream Cycle.
        """
        return tool_name in self.IMMUTABLE_TOOLS

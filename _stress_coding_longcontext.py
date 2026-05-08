#!/usr/bin/env python3
"""
Long-context coding stress harness.
Tests multi-file code generation, editing, refactoring with tool orchestration.
Validates atomicity, consistency, and tool chaining.
"""

import json
import sys
import time
import subprocess
import os
from pathlib import Path
from dataclasses import dataclass

PROJECT_ROOT = "tmp/project_test"


@dataclass
class TestResult:
    name: str
    passed: bool
    latency_ms: float
    message: str


def api_request(endpoint: str, payload: dict) -> dict:
    """Make JSON RPC request to bridge."""
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                "-X",
                "POST",
                f"http://127.0.0.1:8090{endpoint}",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except Exception as e:
        print(f"API error: {e}")
        return {}


def test_multifile_project_creation() -> TestResult:
    """Create multi-file Python project structure."""
    start = time.time()
    try:
        # Create project directory structure
        base_path = f"/home/user/Gator/{PROJECT_ROOT}"
        os.makedirs(base_path, exist_ok=True)

        files_to_create = {
            "main.py": """#!/usr/bin/env python3
\"\"\"Main application entry point.\"\"\"
from utils import helper
from models import DataModel

def main():
    model = DataModel()
    result = helper.process(model)
    print(f"Result: {result}")

if __name__ == "__main__":
    main()
""",
            "utils/helper.py": """\"\"\"Helper utilities module.\"\"\"

def process(data):
    \"\"\"Process data object.\"\"\"
    return f"Processed: {data.name}"

def validate(item):
    \"\"\"Validate item.\"\"\"
    return item is not None
""",
            "models/__init__.py": """\"\"\"Data models package.\"\"\"
from .datamodel import DataModel

__all__ = ["DataModel"]
""",
            "models/datamodel.py": """\"\"\"Core data model.\"\"\"

class DataModel:
    \"\"\"Represents application data.\"\"\"
    
    def __init__(self, name="default"):
        self.name = name
    
    def serialize(self):
        return {"name": self.name}
""",
            "tests/__init__.py": "",
            "config.json": json.dumps(
                {"version": "1.0.0", "debug": True, "timeout": 30}, indent=2
            ),
        }

        success_count = 0
        for filepath, content in files_to_create.items():
            full_path = f"{base_path}/{filepath}"
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            
            # Create via tool API
            resp = api_request(
                "/api/tools/execute",
                {
                    "tool": "file_write",
                    "args": {
                        "path": f"{PROJECT_ROOT}/{filepath}",
                        "content": content,
                        "mode": "overwrite",
                    },
                    "issued_by": "stress-test",
                },
            )
            if resp.get("ok"):
                success_count += 1

        latency = (time.time() - start) * 1000
        return TestResult(
            name="multifile_project_creation",
            passed=success_count == len(files_to_create),
            latency_ms=latency,
            message=f"Created {success_count}/{len(files_to_create)} files",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="multifile_project_creation",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_function_signature_refactor() -> TestResult:
    """Edit function signature across files."""
    start = time.time()
    try:
        # Read original file
        read_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{PROJECT_ROOT}/utils/helper.py",
                    "start_line": 1,
                    "end_line": 20,
                },
                "issued_by": "stress-test",
            },
        )

        # Transactional multi-edit: signature + body change in one atomic operation.
        batch_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_batch_edit",
                "args": {
                    "edits": [
                        {
                            "path": f"{PROJECT_ROOT}/utils/helper.py",
                            "find": "def process(data):",
                            "replace": "def process(data, verbose=False):",
                        },
                        {
                            "path": f"{PROJECT_ROOT}/utils/helper.py",
                            "find": '"""Process data object."""\n    return f"Processed: {data.name}"',
                            "replace": '"""Process data object."""\n    if verbose:\n        print(f"Processing: {data.name}")\n    return f"Processed: {data.name}"',
                        },
                    ]
                },
                "issued_by": "stress-test",
            },
        )

        # Verify changes
        verify_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{PROJECT_ROOT}/utils/helper.py",
                    "start_line": 1,
                    "end_line": 20,
                },
                "issued_by": "stress-test",
            },
        )

        passed = (
            read_resp.get("ok")
            and batch_resp.get("ok")
            and "verbose" in verify_resp.get("content", "")
        )

        latency = (time.time() - start) * 1000
        return TestResult(
            name="function_signature_refactor",
            passed=passed,
            latency_ms=latency,
            message="Function signature updated across files"
            if passed
            else "Refactor failed",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="function_signature_refactor",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_class_docstring_update() -> TestResult:
    """Update class docstring with type hints."""
    start = time.time()
    try:
        edit_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_edit",
                "args": {
                    "path": f"{PROJECT_ROOT}/models/datamodel.py",
                    "find": 'class DataModel:\n    """Represents application data."""',
                    "replace": 'class DataModel:\n    """Represents application data.\n    \n    Attributes:\n        name: Object identifier (str)\n    """',
                },
                "issued_by": "stress-test",
            },
        )

        passed = edit_resp.get("ok")
        latency = (time.time() - start) * 1000
        return TestResult(
            name="class_docstring_update",
            passed=passed,
            latency_ms=latency,
            message="Docstring updated" if passed else "Failed to update docstring",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="class_docstring_update",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_test_file_generation() -> TestResult:
    """Generate test file based on implementation."""
    start = time.time()
    try:
        test_content = """\"\"\"Tests for DataModel.\"\"\"
import pytest
from models import DataModel

def test_datamodel_init():
    model = DataModel("test")
    assert model.name == "test"

def test_datamodel_serialize():
    model = DataModel("test")
    data = model.serialize()
    assert data["name"] == "test"

@pytest.mark.parametrize("name", ["a", "b", "c"])
def test_datamodel_variants(name):
    model = DataModel(name)
    assert model.name == name
"""

        write_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_write",
                "args": {
                    "path": f"{PROJECT_ROOT}/tests/test_datamodel.py",
                    "content": test_content,
                    "mode": "overwrite",
                },
                "issued_by": "stress-test",
            },
        )

        passed = write_resp.get("ok")
        latency = (time.time() - start) * 1000
        return TestResult(
            name="test_file_generation",
            passed=passed,
            latency_ms=latency,
            message="Test file created" if passed else "Failed to create test file",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="test_file_generation",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_multifile_read_verify() -> TestResult:
    """Read multiple files and verify consistency."""
    start = time.time()
    try:
        files = [
            (f"{PROJECT_ROOT}/main.py", 1, 50),
            (f"{PROJECT_ROOT}/models/datamodel.py", 1, 50),
            (f"{PROJECT_ROOT}/utils/helper.py", 1, 50),
        ]

        all_ok = True
        total_lines = 0

        for filepath, start_line, end_line in files:
            resp = api_request(
                "/api/tools/execute",
                {
                    "tool": "file_read",
                    "args": {
                        "path": filepath,
                        "start_line": start_line,
                        "end_line": end_line,
                    },
                    "issued_by": "stress-test",
                },
            )
            if not resp.get("ok"):
                all_ok = False
            else:
                content = resp.get("content", "")
                total_lines += len(content.split("\n"))

        latency = (time.time() - start) * 1000
        return TestResult(
            name="multifile_read_verify",
            passed=all_ok and total_lines > 0,
            latency_ms=latency,
            message=f"Read {len(files)} files, {total_lines} total lines"
            if all_ok
            else "Failed to read files",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="multifile_read_verify",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_config_update_chain() -> TestResult:
    """Chain: write→read→edit→read cycle."""
    start = time.time()
    try:
        # Write new config
        write_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_write",
                "args": {
                    "path": f"{PROJECT_ROOT}/config_new.json",
                    "content": '{"version": "2.0.0", "features": []}',
                    "mode": "overwrite",
                },
                "issued_by": "stress-test",
            },
        )

        # Read back
        read_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{PROJECT_ROOT}/config_new.json",
                    "start_line": 1,
                    "end_line": 5,
                },
                "issued_by": "stress-test",
            },
        )

        # Edit config
        edit_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_edit",
                "args": {
                    "path": f"{PROJECT_ROOT}/config_new.json",
                    "find": '"features": []',
                    "replace": '"features": ["feature_a", "feature_b"]',
                },
                "issued_by": "stress-test",
            },
        )

        # Read final
        final_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{PROJECT_ROOT}/config_new.json",
                    "start_line": 1,
                    "end_line": 5,
                },
                "issued_by": "stress-test",
            },
        )

        passed = (
            write_resp.get("ok")
            and read_resp.get("ok")
            and edit_resp.get("ok")
            and final_resp.get("ok")
            and "feature_a" in final_resp.get("content", "")
        )

        latency = (time.time() - start) * 1000
        return TestResult(
            name="config_update_chain",
            passed=passed,
            latency_ms=latency,
            message="Chain completed successfully" if passed else "Chain failed",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="config_update_chain",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_deeply_nested_paths() -> TestResult:
    """Handle deeply nested directory structures."""
    start = time.time()
    try:
        deep_path = f"{PROJECT_ROOT}/src/lib/utils/helpers/validators"
        content = "def is_valid(x): return x is not None"

        write_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_write",
                "args": {
                    "path": f"{deep_path}/check.py",
                    "content": content,
                    "mode": "overwrite",
                },
                "issued_by": "stress-test",
            },
        )

        read_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{deep_path}/check.py",
                    "start_line": 1,
                    "end_line": 10,
                },
                "issued_by": "stress-test",
            },
        )

        passed = write_resp.get("ok") and read_resp.get("ok")
        latency = (time.time() - start) * 1000
        return TestResult(
            name="deeply_nested_paths",
            passed=passed,
            latency_ms=latency,
            message="Nested path handling OK" if passed else "Failed to handle nested paths",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="deeply_nested_paths",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_large_file_handling() -> TestResult:
    """Handle large file reads and writes (>10KB)."""
    start = time.time()
    try:
        # Create large file (15KB of code)
        large_content = "# Large Python file\n"
        for i in range(400):
            large_content += (
                f"\ndef function_{i}(x, y, z):\n"
                f'    """Function {i}."""\n'
                f"    return x + y + z + {i}\n"
            )

        write_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_write",
                "args": {
                    "path": f"{PROJECT_ROOT}/large_module.py",
                    "content": large_content,
                    "mode": "overwrite",
                },
                "issued_by": "stress-test",
            },
        )

        read_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{PROJECT_ROOT}/large_module.py",
                    "start_line": 1,
                    "end_line": 100,
                },
                "issued_by": "stress-test",
            },
        )

        passed = write_resp.get("ok") and read_resp.get("ok")
        latency = (time.time() - start) * 1000
        file_size_kb = len(large_content) / 1024
        return TestResult(
            name="large_file_handling",
            passed=passed,
            latency_ms=latency,
            message=f"Handled {file_size_kb:.1f}KB file" if passed else "Large file handling failed",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="large_file_handling",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def test_sequential_edits() -> TestResult:
    """Apply multiple sequential edits to same file."""
    start = time.time()
    try:
        # Create initial file
        api_request(
            "/api/tools/execute",
            {
                "tool": "file_write",
                "args": {
                    "path": f"{PROJECT_ROOT}/evolving.py",
                    "content": "x = 1",
                    "mode": "overwrite",
                },
                "issued_by": "stress-test",
            },
        )

        # Apply 5 sequential edits
        edits = [
            ("x = 1", "x = 1\ny = 2"),
            ("y = 2", "y = 2\nz = 3"),
            ("z = 3", "z = 3\nresult = x + y + z"),
            ("result = x + y + z", "result = x + y + z\nprint(result)"),
            ("print(result)", "print(result)\n# Done"),
        ]

        all_ok = True
        for find_text, replace_text in edits:
            resp = api_request(
                "/api/tools/execute",
                {
                    "tool": "file_edit",
                    "args": {
                        "path": f"{PROJECT_ROOT}/evolving.py",
                        "find": find_text,
                        "replace": replace_text,
                    },
                    "issued_by": "stress-test",
                },
            )
            if not resp.get("ok"):
                all_ok = False
                break

        # Verify final state
        final_resp = api_request(
            "/api/tools/execute",
            {
                "tool": "file_read",
                "args": {
                    "path": f"{PROJECT_ROOT}/evolving.py",
                    "start_line": 1,
                    "end_line": 20,
                },
                "issued_by": "stress-test",
            },
        )

        passed = all_ok and "Done" in final_resp.get("content", "")
        latency = (time.time() - start) * 1000
        return TestResult(
            name="sequential_edits",
            passed=passed,
            latency_ms=latency,
            message="All edits applied successfully" if passed else "Sequential edits failed",
        )
    except Exception as e:
        latency = (time.time() - start) * 1000
        return TestResult(
            name="sequential_edits",
            passed=False,
            latency_ms=latency,
            message=str(e),
        )


def main():
    """Run long-context coding stress tests."""
    print("\n========================================")
    print("LONG-CONTEXT CODING STRESS TEST SUITE")
    print("========================================\n")

    tests = [
        test_multifile_project_creation,
        test_function_signature_refactor,
        test_class_docstring_update,
        test_test_file_generation,
        test_multifile_read_verify,
        test_config_update_chain,
        test_deeply_nested_paths,
        test_large_file_handling,
        test_sequential_edits,
    ]

    results = []
    for test_fn in tests:
        result = test_fn()
        results.append(result)
        status = "[PASS]" if result.passed else "[FAIL]"
        print(f"{status} {result.name} ({result.latency_ms:.1f}ms)")
        if not result.passed:
            print(f"       {result.message}")

    # Summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    avg_latency = sum(r.latency_ms for r in results) / total if total > 0 else 0

    print(f"\n========================================")
    print(f"RESULTS: {passed}/{total} passed")
    print(f"Average latency: {avg_latency:.1f}ms")
    print(f"========================================\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

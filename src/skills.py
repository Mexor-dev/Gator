#!/home/user/Gator/venv/bin/python3
"""Phase 3 Architect: recursive skill creation and persistence.

Given a requested missing tool, generate a Python script, sandbox-test it,
then persist the resulting Skill Node into LanceDB + Graphify source docs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa

from memory_core import GatorMemoryCore

GATOR_ROOT = Path.home() / "Gator"
TOOLS_DIR = GATOR_ROOT / "src" / "tools" / "generated"
RESEARCH_SKILLS_DIR = GATOR_ROOT / "research" / "skills"
GRAPHIFY_BIN = Path.home() / ".local" / "bin" / "graphify"
SKILL_TABLE = "skill_nodes"


class SkillsError(RuntimeError):
    pass


@dataclass
class SkillBuildResult:
    skill_name: str
    script_path: str
    test_exit_code: int
    test_output_tail: str
    skill_node_id: str
    graphify_updated: bool


class SkillArchitect:
    def __init__(self, server_url: str = "http://127.0.0.1:8081") -> None:
        self.mem = GatorMemoryCore(server_url=server_url)
        self.db = lancedb.connect(str(GATOR_ROOT / "db"))
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        RESEARCH_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    def _table_names(self) -> set[str]:
        raw = self.db.list_tables()
        if hasattr(raw, "tables"):
            raw = getattr(raw, "tables")
        return {str(x[0] if isinstance(x, (list, tuple)) and x else x) for x in raw}

    def _schema_for_dim(self, dim: int) -> pa.Schema:
        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("skill_name", pa.string()),
                pa.field("script_path", pa.string()),
                pa.field("spec", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("created_at", pa.float64()),
            ]
        )

    def _open_or_create_skill_table(self, dim: int):
        names = self._table_names()
        if SKILL_TABLE not in names:
            return self.db.create_table(SKILL_TABLE, schema=self._schema_for_dim(dim), mode="create")
        return self.db.open_table(SKILL_TABLE)

    def _skill_script_template(self, skill_name: str, spec: str) -> str:
        safe_name = skill_name.replace("-", "_")
        return textwrap.dedent(
            f"""
            #!/home/user/Gator/venv/bin/python3
            \"\"\"Auto-generated skill: {safe_name}.\"\"\"

            from __future__ import annotations

            import argparse
            import json


            def run(task: str) -> dict:
                # Minimal deterministic skill behavior for first stable deploy.
                return {{
                    "skill": "{safe_name}",
                    "task": task,
                    "result": "ok",
                    "notes": "{spec[:120].replace('"', "'")}",
                }}


            def _main() -> None:
                parser = argparse.ArgumentParser()
                parser.add_argument("--task", default="health-check")
                args = parser.parse_args()
                print(json.dumps(run(args.task)))


            if __name__ == "__main__":
                _main()
            """
        ).strip() + "\n"

    def _sandbox_test(self, script_path: Path) -> tuple[int, str]:
        compile_cmd = [str(GATOR_ROOT / "venv" / "bin" / "python"), "-m", "py_compile", str(script_path)]
        c1 = subprocess.run(compile_cmd, capture_output=True, text=True, check=False)
        if c1.returncode != 0:
            return c1.returncode, (c1.stderr or c1.stdout)[-600:]

        run_cmd = [str(GATOR_ROOT / "venv" / "bin" / "python"), str(script_path), "--task", "sandbox-test"]
        c2 = subprocess.run(run_cmd, capture_output=True, text=True, check=False, timeout=20)
        return c2.returncode, (c2.stdout + "\n" + c2.stderr)[-600:]

    def _update_graphify(self) -> bool:
        if not GRAPHIFY_BIN.exists():
            return False
        cmd = [str(GRAPHIFY_BIN), "update", str(GATOR_ROOT / "research")]
        proc = subprocess.run(cmd, cwd=str(GATOR_ROOT), capture_output=True, text=True, check=False)
        return proc.returncode == 0

    def create_skill(self, skill_name: str, spec: str) -> SkillBuildResult:
        if not skill_name.strip():
            raise SkillsError("skill_name cannot be empty")
        if not spec.strip():
            raise SkillsError("spec cannot be empty")

        safe = "".join(ch for ch in skill_name.lower() if ch.isalnum() or ch in "_-").strip("_-")
        if not safe:
            safe = f"skill_{uuid.uuid4().hex[:8]}"

        script_path = TOOLS_DIR / f"{safe}.py"
        script_path.write_text(self._skill_script_template(safe, spec), encoding="utf-8")

        exit_code, output_tail = self._sandbox_test(script_path)
        if exit_code != 0:
            raise SkillsError(f"sandbox test failed for {script_path}: {output_tail}")

        spec_blob = f"skill={safe}\nspec={spec}\npath={script_path}"
        vec, _ = self.mem._embed_text(spec_blob)
        table = self._open_or_create_skill_table(len(vec))

        node_id = str(uuid.uuid4())
        table.add(
            [
                {
                    "id": node_id,
                    "skill_name": safe,
                    "script_path": str(script_path),
                    "spec": spec,
                    "vector": np.asarray(vec, dtype=np.float32).tolist(),
                    "created_at": time.time(),
                }
            ]
        )

        doc_path = RESEARCH_SKILLS_DIR / f"{safe}.md"
        doc_path.write_text(
            f"# Skill Node: {safe}\n\nSpec: {spec}\n\nScript: {script_path}\n",
            encoding="utf-8",
        )

        graph_updated = self._update_graphify()

        return SkillBuildResult(
            skill_name=safe,
            script_path=str(script_path),
            test_exit_code=exit_code,
            test_output_tail=output_tail,
            skill_node_id=node_id,
            graphify_updated=graph_updated,
        )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Gator Architect skill creator")
    parser.add_argument("--skill-name", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--server", default="http://127.0.0.1:8081")
    args = parser.parse_args()

    arch = SkillArchitect(server_url=args.server)
    out = arch.create_skill(args.skill_name, args.spec)
    print(json.dumps(out.__dict__, indent=2))


if __name__ == "__main__":
    try:
        _main()
    except Exception as exc:
        raise SystemExit(f"[ERROR] {exc}")

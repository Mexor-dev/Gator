#!/usr/bin/env python3
"""GatorMap: hierarchical structural blueprint, snapshot, and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from event_bus import EventBusClient

GATOR_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = GATOR_ROOT / "src"
MAP_ROOT = GATOR_ROOT / "bin" / "gator_map"
MAP_FILE = MAP_ROOT / "gator_map.json"
SNAPSHOT_ROOT = MAP_ROOT / "snapshots"
MASTER_BASELINE_FILE = MAP_ROOT / "gator_blueprint_master.json"
VRAM_REVERT_THRESHOLD_MIB = 5800


class GatorMapError(RuntimeError):
    pass


@dataclass
class SnapshotMeta:
    snapshot_id: str
    created_at: float
    reason: str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _module_coordinate(path: Path, src_root: Path) -> dict[str, float]:
    rel = path.relative_to(src_root)
    depth = len(rel.parts) - 1
    stem_val = sum(ord(ch) for ch in rel.stem)
    x = float((stem_val % 97) - 48)
    y = float(depth * 12)
    z = float((len(rel.suffix) * 7 + stem_val % 31) - 15)
    return {"x": x, "y": y, "z": z}


def _estimate_vram_mib(path: Path, content: str) -> int:
    text = content.lower()
    base = 12
    if "cuda" in text or "gpu" in text or "vram" in text:
        base += 160
    if "lancedb" in text or "vector" in text:
        base += 60
    if "token" in text or "sampling" in text or "kv" in text:
        base += 90
    return base


class GatorMap:
    def __init__(self, root: Path = GATOR_ROOT) -> None:
        self.root = root
        self.src_root = root / "src"
        self.map_root = root / "bin" / "gator_map"
        self.snapshot_root = self.map_root / "snapshots"
        self.map_file = self.map_root / "gator_map.json"
        self.map_root.mkdir(parents=True, exist_ok=True)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)

    def _iter_source_files(self) -> list[Path]:
        return sorted(
            [
                p
                for p in self.src_root.rglob("*")
                if p.is_file() and p.suffix in {".py", ".cpp", ".h", ".hpp", ".sh", ".json", ".md"}
            ]
        )

    def build_blueprint(self) -> dict[str, Any]:
        files = self._iter_source_files()
        modules: list[dict[str, Any]] = []
        by_dir: dict[str, list[str]] = {}

        for file_path in files:
            rel = file_path.relative_to(self.root).as_posix()
            text = file_path.read_text(encoding="utf-8", errors="replace")
            module = {
                "path": rel,
                "sha256": _sha256_file(file_path),
                "bytes": file_path.stat().st_size,
                "vram_dependency_mib": _estimate_vram_mib(file_path, text),
                "coord": _module_coordinate(file_path, self.src_root),
            }
            modules.append(module)
            parent = str(file_path.parent.relative_to(self.root).as_posix())
            by_dir.setdefault(parent, []).append(rel)

        return {
            "schema": "gator_map.v1",
            "created_at": time.time(),
            "root": str(self.root),
            "module_count": len(modules),
            "directories": by_dir,
            "modules": modules,
            "layout_3d": {
                "units": "abstract",
                "axis": {"x": "module hash spread", "y": "directory depth", "z": "language-weighted variation"},
            },
            "identity_constraint": {
                "system_identity": "cpp_rtx_direct",
                "donor_bound": True,
                "domain": ["C++", "CUDA", "RTX", "GPU architecture", "inference kernels", "systems engineering"],
                "rejected_domains": ["Node.js", "Express", "npm", "React", "mobile dev", "Android", "iOS", "Flutter"],
            },
        }

    def save_map(self, blueprint: dict[str, Any]) -> Path:
        self.map_file.write_text(json.dumps(blueprint, indent=2), encoding="utf-8")
        try:
            EventBusClient().publish(
                {
                    "type": "gator_map_sync",
                    "source": "gator_map",
                    "module_count": int(blueprint.get("module_count", 0)),
                    "map_file": str(self.map_file),
                    "identity_constraint": blueprint.get("identity_constraint", {}),
                    "final": False,
                }
            )
        except Exception:
            pass
        return self.map_file

    def seal_master_baseline(self, label: str = "github_release", gauntlet_report: dict[str, Any] | None = None) -> Path:
        blueprint = self.build_blueprint()
        blueprint["master_baseline"] = {
            "sealed": True,
            "label": label,
            "sealed_at": time.time(),
            "gauntlet_report": gauntlet_report or {},
        }
        MASTER_BASELINE_FILE.write_text(json.dumps(blueprint, indent=2), encoding="utf-8")
        self.save_map(blueprint)
        return MASTER_BASELINE_FILE

    def load_map(self) -> dict[str, Any]:
        if not self.map_file.exists():
            raise GatorMapError(f"Missing map file: {self.map_file}")
        return json.loads(self.map_file.read_text(encoding="utf-8"))

    def snapshot_system_state(self, reason: str = "manual") -> dict[str, Any]:
        blueprint = self.build_blueprint()
        self.save_map(blueprint)

        snapshot_id = time.strftime("%Y%m%d_%H%M%S")
        target = self.snapshot_root / snapshot_id
        src_target = target / "src"
        src_target.mkdir(parents=True, exist_ok=True)

        for file_path in self._iter_source_files():
            rel = file_path.relative_to(self.src_root)
            dst = src_target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dst)

        meta = SnapshotMeta(snapshot_id=snapshot_id, created_at=time.time(), reason=reason)
        (target / "meta.json").write_text(json.dumps(meta.__dict__, indent=2), encoding="utf-8")

        return {
            "snapshot_id": snapshot_id,
            "snapshot_path": str(target),
            "module_count": blueprint["module_count"],
            "map_file": str(self.map_file),
        }

    def list_snapshots(self) -> list[str]:
        return sorted([p.name for p in self.snapshot_root.iterdir() if p.is_dir()])

    def rollback_to_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        snap = self.snapshot_root / snapshot_id
        src_snap = snap / "src"
        if not src_snap.exists():
            raise GatorMapError(f"Snapshot not found: {snapshot_id}")

        # Clear current tracked sources and restore from snapshot copy.
        restored = 0
        for file_path in self._iter_source_files():
            file_path.unlink(missing_ok=True)
        for file_path in sorted(src_snap.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(src_snap)
            dst = self.src_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dst)
            restored += 1

        blueprint = self.build_blueprint()
        self.save_map(blueprint)
        try:
            EventBusClient().publish(
                {
                    "type": "gator_map_rollback",
                    "snapshot_id": snapshot_id,
                    "restored_files": restored,
                    "final": True,
                }
            )
        except Exception:
            pass
        return {"rolled_back": True, "snapshot_id": snapshot_id, "restored_files": restored}

    def guard_and_revert(self, crashed: bool, vram_used_mib: int) -> dict[str, Any]:
        if not crashed and vram_used_mib <= VRAM_REVERT_THRESHOLD_MIB:
            return {"rollback": False, "reason": "within_threshold"}
        snapshots = self.list_snapshots()
        if not snapshots:
            raise GatorMapError("No stable snapshots available for rollback")
        latest = snapshots[-1]
        out = self.rollback_to_snapshot(latest)
        out["reason"] = "crash" if crashed else f"vram_spike:{vram_used_mib}"
        return out


def _main() -> None:
    parser = argparse.ArgumentParser(description="GatorMap snapshot and rollback utility")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--reason", default="manual")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--rollback", type=str)
    parser.add_argument("--guard", action="store_true")
    parser.add_argument("--crashed", action="store_true")
    parser.add_argument("--vram", type=int, default=0)
    parser.add_argument("--seal-master", action="store_true")
    parser.add_argument("--label", default="github_release")
    args = parser.parse_args()

    gm = GatorMap()
    out: dict[str, Any] = {}

    if args.snapshot:
        out["snapshot"] = gm.snapshot_system_state(reason=args.reason)
    if args.list:
        out["snapshots"] = gm.list_snapshots()
    if args.rollback:
        out["rollback"] = gm.rollback_to_snapshot(args.rollback)
    if args.guard:
        out["guard"] = gm.guard_and_revert(crashed=args.crashed, vram_used_mib=args.vram)
    if args.seal_master:
        out["master_baseline"] = str(gm.seal_master_baseline(label=args.label))

    if not out:
        parser.error("Provide an action flag")

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _main()

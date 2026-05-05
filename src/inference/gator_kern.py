#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import time
from pathlib import Path

GATOR_ROOT = Path(__file__).resolve().parents[2]


class GatorKernError(RuntimeError):
    pass


class GatorKernRuntime:
    def __init__(self, library_path: Path | None = None, vocab_size: int = 32000, kv_tokens: int = 512, kv_bytes_per_token: int = 32) -> None:
        self.library_path = library_path or self._default_library_path()
        self.lib = ctypes.CDLL(str(self.library_path))
        self._configure_signatures()
        self.handle = self.lib.gator_kern_create(vocab_size, kv_tokens, kv_bytes_per_token)
        if not self.handle:
            raise GatorKernError("Failed to initialize gator_kern runtime")
        self.vocab_size = vocab_size

    def _default_library_path(self) -> Path:
        candidates = [
            GATOR_ROOT / "src" / "inference" / "libgator_kern.so",
            GATOR_ROOT / "src" / "inference" / "gator_kern.dll",
            GATOR_ROOT / "build" / "src" / "inference" / "libgator_kern.so",
            GATOR_ROOT / "build" / "libgator_kern.so",
            GATOR_ROOT / "build" / "src" / "inference" / "gator_kern.dll",
            GATOR_ROOT / "build" / "gator_kern.dll",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise GatorKernError("libgator_kern.so not found; build the native target first")

    def _configure_signatures(self) -> None:
        self.lib.gator_kern_create.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t]
        self.lib.gator_kern_create.restype = ctypes.c_void_p
        self.lib.gator_kern_destroy.argtypes = [ctypes.c_void_p]
        self.lib.gator_kern_resize_kv.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.gator_kern_resize_kv.restype = ctypes.c_int
        self.lib.gator_kern_kv_bytes.argtypes = [ctypes.c_void_p]
        self.lib.gator_kern_kv_bytes.restype = ctypes.c_size_t
        self.lib.gator_kern_flush_pool.argtypes = [ctypes.c_void_p]
        self.lib.gator_kern_flush_pool.restype = ctypes.c_int
        self.lib.gator_kern_decode.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t]
        self.lib.gator_kern_decode.restype = ctypes.c_int
        self.lib.gator_kern_sample.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_size_t,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self.lib.gator_kern_sample.restype = ctypes.c_int32
        self.lib.gator_kern_logic_singleton_addr.argtypes = []
        self.lib.gator_kern_logic_singleton_addr.restype = ctypes.c_size_t

    def decode(self, token_id: int) -> list[float]:
        buf = (ctypes.c_float * self.vocab_size)()
        rc = self.lib.gator_kern_decode(self.handle, token_id, buf, self.vocab_size)
        if rc != 0:
            raise GatorKernError(f"decode failed: {rc}")
        return list(buf)

    def sample_tokens(self, start_token: int = 1, count: int = 128) -> list[int]:
        out: list[int] = []
        current = start_token
        for step in range(count):
            logits = self.decode(current)
            buf = (ctypes.c_float * self.vocab_size)(*logits)
            sampled = self.lib.gator_kern_sample(
                self.handle,
                buf,
                self.vocab_size,
                ctypes.c_float(0.8),
                ctypes.c_float(0.9),
                ctypes.c_uint64(int(time.time()) + step),
                None,
                0,
            )
            out.append(int(sampled))
            current = int(sampled)
        return out

    def resize_kv(self, kv_tokens: int) -> int:
        rc = self.lib.gator_kern_resize_kv(self.handle, kv_tokens)
        if rc != 0:
            raise GatorKernError(f"resize_kv failed: {rc}")
        return int(self.lib.gator_kern_kv_bytes(self.handle))

    def flush_pool(self) -> None:
        rc = self.lib.gator_kern_flush_pool(self.handle)
        if rc != 0:
            raise GatorKernError(f"flush_pool failed: {rc}")

    def logic_singleton_addr(self) -> int:
        return int(self.lib.gator_kern_logic_singleton_addr())

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.lib.gator_kern_destroy(self.handle)
            self.handle = None

    def __enter__(self) -> "GatorKernRuntime":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

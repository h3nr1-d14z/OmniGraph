"""Embedding backends."""

import os
import platform

from .base import BaseBackend
from .onnx_backend import OnnxBackend

__all__ = ["BaseBackend", "OnnxBackend", "get_backend"]

# Module-level singleton cache: name -> backend instance
_BACKEND_CACHE: dict[str, BaseBackend] = {}


def get_backend(backend_name: str | None = None) -> BaseBackend:
    """Factory: return a cached backend instance (model loaded once per process)."""
    raw_name: str = backend_name if backend_name else os.getenv("EMBED_BACKEND", "auto")
    name = raw_name.lower()

    if name in _BACKEND_CACHE:
        return _BACKEND_CACHE[name]

    backend: BaseBackend

    if name == "openvino":
        from .openvino_backend import OpenVinoBackend

        backend = OpenVinoBackend()
    elif name == "mlx":
        from .mlx_backend import MlxBackend

        backend = MlxBackend()
    elif name == "onnx":
        backend = OnnxBackend()
    elif name == "jina":
        from .jina_code_backend import JinaCodeBackend

        backend = JinaCodeBackend()
    else:
        # auto-detect
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "darwin" and machine in ("arm64", "aarch64"):
            backend = OnnxBackend()
        elif system == "linux":
            try:
                import onnxruntime as ort

                if "OpenVINOExecutionProvider" in ort.get_available_providers():
                    from .openvino_backend import OpenVinoBackend

                    backend = OpenVinoBackend()
                else:
                    backend = OnnxBackend()
            except Exception:
                backend = OnnxBackend()
        else:
            backend = OnnxBackend()

    _BACKEND_CACHE[name] = backend
    return backend

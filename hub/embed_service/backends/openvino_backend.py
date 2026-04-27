"""OpenVINO execution provider for Intel CPU optimization."""

import numpy as np

from .onnx_backend import OnnxBackend


class OpenVinoBackend(OnnxBackend):
    """ONNX Runtime with OpenVINO EP for Intel CPU."""

    def _load(self) -> None:
        import onnxruntime as ort

        try:
            providers = ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
        except Exception:
            providers = ["CPUExecutionProvider"]

        from pathlib import Path
        from huggingface_hub import snapshot_download
        import os

        DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"
        ONNX_FILE = "onnx/model.onnx"

        cache_dir = os.getenv("MODEL_CACHE")
        kwargs = {"cache_dir": cache_dir} if cache_dir else {}
        local_dir = snapshot_download(
            repo_id=self.model_name,
            allow_patterns=["onnx/*", "tokenizer.json"],
            **kwargs,
        )
        onnx_path = Path(local_dir) / ONNX_FILE
        tok_path = Path(local_dir) / "tokenizer.json"

        if not onnx_path.exists():
            raise FileNotFoundError(f"ONNX model not found at {onnx_path}")

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(onnx_path), sess_options, providers=providers
        )
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(str(tok_path))
        print(
            f"[openvino] Loaded {self.model_name} with providers {self._session.get_providers()}"
        )

    @property
    def name(self) -> str:
        return f"openvino-{self.model_name}"

    # Explicit pass-through documenting that OpenVINO inherits the nomic
    # prefix contract from OnnxBackend. Avoids "is the parent's contract
    # the right one for this backend?" doubt at the call site.
    def format_input(self, texts: list[str], mode: str) -> list[str]:
        return super().format_input(texts, mode)

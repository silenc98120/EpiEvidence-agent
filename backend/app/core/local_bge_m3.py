"""本地 BGE-M3 dense embedding Adapter。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class LocalBGEM3EmbeddingClient:
    """在进程内复用一个 SentenceTransformer BGE-M3 模型实例。"""

    def __init__(
        self,
        *,
        model_path: str | Path,
        device: str = "cpu",
        batch_size: int = 8,
        max_length: int = 1024,
        max_concurrency: int = 1,
        encoder: Any | None = None,
    ) -> None:
        self._model_path = Path(model_path)
        if batch_size < 1 or max_length < 1 or max_concurrency < 1:
            raise ValueError("batch_size、max_length 和 max_concurrency 必须大于 0")
        self._device = device
        self._batch_size = batch_size
        self._max_length = max_length
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._encoder = encoder
        self._load_lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return f"local-bge-m3:{self._model_path}"

    @property
    def model_revision(self) -> str:
        return "local"

    async def load(self) -> None:
        if self._encoder is not None:
            return
        async with self._load_lock:
            if self._encoder is not None:
                return
            if not self._model_path.is_dir():
                raise RuntimeError(f"BGE-M3 模型目录不存在: {self._model_path}")
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError(
                    "本地 BGE-M3 需要 sentence-transformers 与其 PyTorch 依赖"
                ) from exc

            def create_encoder() -> Any:
                model = SentenceTransformer(
                    str(self._model_path),
                    device=self._device,
                    local_files_only=True,
                )
                model.max_seq_length = self._max_length
                return model

            self._encoder = await asyncio.to_thread(create_encoder)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        await self.load()
        async with self._semaphore:
            encoded = await asyncio.to_thread(
                self._encoder.encode,
                list(texts),
                batch_size=self._batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return encoded.astype("float32", copy=False).tolist()

    async def close(self) -> None:
        self._encoder = None


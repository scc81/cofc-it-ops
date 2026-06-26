"""
embedding.py — Shared Ollama-backed ChromaDB embedding function
=================================================================
CofCITIP — College of Charleston IT Infrastructure Platform

Single source of truth for ChromaDB embeddings. Both jarvis_core.py and
seed_context.py import get_embedding_function() so the Ollama HTTP call lives
in exactly one place (no copy-paste drift between the two files).

WHY THIS EXISTS: Chroma's default embedding function (ONNXMiniLM_L6_V2)
lazily downloads an ONNX model into a cache under the *current user's* home
dir on first use. JARVIS runs as the `cofc-itip` system account, which has no
writable home — so the default crashes every /query with PermissionError.
This class routes embeddings through the already-running local Ollama
(nomic-embed-text), which keeps everything on-box (FERPA) and never writes to
the service account's home directory.

Egress note: the only network call this makes is to OLLAMA_HOST
(localhost:11434 by default). No external egress, ever.
"""

from __future__ import annotations

import os

import requests

# Reuse the same OLLAMA_HOST the rest of the stack uses. Embeddings model is
# nomic-embed-text (pulled by jarvis-install.sh); override only if the embed
# model name changes on BB.
OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL  = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "60"))


class OllamaEmbeddingFunction:
    """
    Chroma EmbeddingFunction backed by local Ollama.

    Implements Chroma's interface: __call__(self, input: list[str]) ->
    list[list[float]]. Chroma names the argument `input` (not `texts`) — the
    name matters; Chroma validates the signature on newer versions.
    """

    def __init__(self, model: str = EMBED_MODEL, host: str = OLLAMA_HOST):
        self._model = model
        self._host = host.rstrip("/")

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        # `input` shadows the builtin by Chroma's contract — required name.
        embeddings: list[list[float]] = []
        for text in input:
            resp = requests.post(
                f"{self._host}/api/embeddings",
                json={"model": self._model, "prompt": text},
                timeout=EMBED_TIMEOUT,
            )
            resp.raise_for_status()
            embeddings.append(resp.json()["embedding"])
        return embeddings

    # Chroma >=0.5 calls .name() when persisting collection config so it can
    # warn if a collection is reopened with a different embedder. Return a
    # stable identifier tied to the model.
    def name(self) -> str:
        return f"ollama-{self._model}"


def get_embedding_function() -> OllamaEmbeddingFunction:
    """Single constructor both jarvis_core.py and seed_context.py call."""
    return OllamaEmbeddingFunction()

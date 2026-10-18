"""Vecteurs denses (sens) : Qwen3-Embedding par défaut, via sentence-transformers.

Les documents sont vectorisés sans instruction ; les requêtes reçoivent une
instruction qui dépend du type de recherche (thème ou citation), pour les
modèles qui en acceptent.
"""

from __future__ import annotations

import numpy as np

MAX_SEQ_LENGTH = 512

QUERY_INSTRUCTIONS = {
    "theme": "Given a theme or a description, retrieve literary passages that depict it",
    "quote": "Given a quotation, possibly translated, retrieve the literary passage that contains it",
}


def _accepts_instructions(model_name: str) -> bool:
    return "qwen3-embedding" in model_name.lower()


def resolve_device(device: str | None) -> str:
    import torch

    return device or ("cuda" if torch.cuda.is_available() else "cpu")


class DenseEncoder:
    def __init__(self, model_name: str, device: str | None = None, batch_size: int = 32) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = model_name
        self.device = resolve_device(device)
        self.batch_size = batch_size
        processor_kwargs = {"padding_side": "left"} if _accepts_instructions(model_name) else {}
        self.model = SentenceTransformer(model_name, device=self.device, processor_kwargs=processor_kwargs)
        if self.device == "cuda":
            self.model.half()
        self.model.max_seq_length = MAX_SEQ_LENGTH

    @property
    def dim(self) -> int:
        return self.model.get_embedding_dimension()

    @property
    def tokenizer(self):
        return self.model.tokenizer

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        # sentence-transformers trie déjà par longueur pour limiter le padding.
        return self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(np.float32)

    def encode_query(self, text: str, mode: str = "theme") -> np.ndarray:
        prompt = None
        if _accepts_instructions(self.model_name):
            prompt = f"Instruct: {QUERY_INSTRUCTIONS[mode]}\nQuery: "
        return self.model.encode(
            text, prompt=prompt, normalize_embeddings=True, convert_to_numpy=True
        ).astype(np.float32)


def count_tokens(tokenizer, texts: list[str]) -> list[int]:
    if not texts:
        return []
    encoded = tokenizer(texts, add_special_tokens=False, truncation=False)["input_ids"]
    return [len(ids) for ids in encoded]

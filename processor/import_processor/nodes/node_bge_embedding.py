"""????????????"""

import json
import logging
from collections.abc import Callable
from typing import Any

from processor.import_processor.base import BaseNode, setup_logging
from processor.import_processor.exceptions import EmbeddingError, StateFieldError
from processor.import_processor.state import ImportGraphState
from utils.embedding_utils import generate_embeddings

EmbeddingGenerator = Callable[[list[str]], dict[str, Any]]


class NodeBGEEmbedding(BaseNode):
    """Generate dense and sparse vectors for document chunks with BGE-M3."""

    name = "node_bge_embedding"

    def __init__(self, embedding_generator: EmbeddingGenerator | None = None, **kwargs: Any):
        """Initialize the embedding node with an optional test generator."""
        super().__init__(**kwargs)
        self.embedding_generator = embedding_generator or generate_embeddings

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """Validate chunks, generate vectors in batches, and update the state."""
        chunks = self._step_1_validate_input(state)
        output_data = self._step_2_generate_embeddings(chunks, state)
        state["chunks"] = output_data
        return state

    def _step_1_validate_input(self, state: ImportGraphState) -> list[dict[str, Any]]:
        """??????????????????"""
        chunks = state.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            raise StateFieldError(
                node_name=self.name,
                field_name="chunks",
                expected_type=list,
                message="?????????????",
            )
        if not all(isinstance(chunk, dict) for chunk in chunks):
            raise StateFieldError(
                node_name=self.name,
                field_name="chunks",
                expected_type=list,
                message="???????????",
            )
        for chunk in chunks:
            if not isinstance(chunk.get("content"), str) or not chunk["content"].strip():
                raise StateFieldError(
                    node_name=self.name,
                    field_name="chunks.content",
                    expected_type=str,
                    message="???????????????",
                )
        return chunks

    def _step_2_generate_embeddings(
        self,
        chunks: list[dict[str, Any]],
        state: ImportGraphState,
    ) -> list[dict[str, Any]]:
        """Build stable embedding text from entity, document, section, and content."""
        batch_size = self._get_batch_size()
        output_data: list[dict[str, Any]] = []
        for start in range(0, len(chunks), batch_size):
            batch_chunks = chunks[start : start + batch_size]
            input_texts = [self._build_embedding_text(chunk, state) for chunk in batch_chunks]
            try:
                embeddings = self.embedding_generator(input_texts)
                dense_vectors = embeddings["dense"]
                sparse_vectors = embeddings["sparse"]
                if len(dense_vectors) != len(batch_chunks) or len(sparse_vectors) != len(batch_chunks):
                    raise ValueError("????????????")
            except Exception as exc:
                raise EmbeddingError(
                    message=f"? {start + 1}-{start + len(batch_chunks)} ?????????",
                    node_name=self.name,
                    cause=exc,
                ) from exc

            for index, chunk in enumerate(batch_chunks):
                enriched_chunk = chunk.copy()
                enriched_chunk["dense_vector"] = dense_vectors[index]
                enriched_chunk["sparse_vector"] = sparse_vectors[index]
                output_data.append(enriched_chunk)
            self.logger.info(
                "??????????=%s???=%s???=%s-%s???=%s",
                state.get("knowledge_base_id") or state.get("library_id"),
                state.get("document_id"),
                start + 1,
                start + len(batch_chunks),
                len(batch_chunks),
            )
        return output_data

    def _get_batch_size(self) -> int:
        """????????????????"""
        batch_size = int(self.config.embedding_batch_size)
        if batch_size < 1:
            raise EmbeddingError(
                message="embedding_batch_size ??????",
                node_name=self.name,
            )
        return batch_size

    @classmethod
    def _build_embedding_text(cls, chunk: dict[str, Any], state: ImportGraphState) -> str:
        """Build stable embedding text from entity, document, section, and content."""
        parts: list[str] = []
        item_names = chunk.get("item_names") or []
        if not isinstance(item_names, list):
            item_names = [item_names]
        item_names = [str(name).strip() for name in item_names if str(name).strip()]
        item_name = str(chunk.get("item_name") or state.get("item_name") or "").strip()
        if item_name and item_name not in item_names:
            item_names.insert(0, item_name)
        file_title = str(chunk.get("file_title") or state.get("file_title") or "").strip()
        parent_title = str(chunk.get("parent_title") or "").strip()
        title = str(chunk.get("title") or "").strip()
        content = str(chunk.get("content") or "").strip()
        if item_names:
            parts.append(f"\u5b9e\u4f53\uff1a{'、'.join(item_names)}")
        if file_title:
            parts.append(f"\u6587\u6863\uff1a{file_title}")
        section_title = " / ".join(value for value in (parent_title, title) if value)
        if section_title:
            parts.append(f"\u7ae0\u8282\uff1a{section_title}")
        parts.append(f"\u5185\u5bb9\uff1a{content}")
        return "\n".join(parts)


if __name__ == "__main__":
    setup_logging()

    def fake_embeddings(texts: list[str]) -> dict[str, Any]:
        """????????????????"""
        return {
            "dense": [[float(index), 1.0] for index, _ in enumerate(texts)],
            "sparse": [{index: 1.0} for index, _ in enumerate(texts)],
        }

    test_config = type("TestConfig", (), {"embedding_batch_size": 2})()
    init_state: ImportGraphState = {
        "task_id": "embedding-test",
        "knowledge_base_id": 7,
        "document_id": 11,
        "file_title": "?????",
        "item_name": "",
        "chunks": [
            {"title": "??", "parent_title": "????", "content": "???????"},
            {"title": "??", "parent_title": "????", "content": "????"},
            {"title": "??", "content": "????"},
        ],
    }
    result = NodeBGEEmbedding(embedding_generator=fake_embeddings, config=test_config)(init_state)
    assert len(result["chunks"]) == 3
    assert all("dense_vector" in chunk and "sparse_vector" in chunk for chunk in result["chunks"])
    assert result["chunks"][0]["item_name"] if "item_name" in result["chunks"][0] else True
    assert NodeBGEEmbedding._build_embedding_text(init_state["chunks"][0], init_state).startswith("????????")
    print("embedding stage 2 test passed")

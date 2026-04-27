"""Qdrant vector store client for code snippets."""

import os
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    SearchParams,
    VectorParams,
)

# Module-level singleton
_qdrant_instance: "QdrantCodeStore | None" = None


def get_qdrant() -> "QdrantCodeStore":
    """Return the singleton QdrantCodeStore instance."""
    global _qdrant_instance
    if _qdrant_instance is None:
        _qdrant_instance = QdrantCodeStore()
    return _qdrant_instance


class QdrantCodeStore:
    """Manage code embeddings in Qdrant."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        collection: str | None = None,
        vector_dim: int = 768,
    ):
        self.client = QdrantClient(
            host=host or os.getenv("QDRANT_HOST", "localhost"),
            port=port or int(os.getenv("QDRANT_PORT", "6333")),
        )
        # Collection-per-model isolation. Default aligns with .env.example
        # (`code_v1_nomic`) so deployments without an explicit env override
        # still hit the legacy collection during/after the embedder cutover.
        # Annotation kept explicit so mypy narrows the str | None param
        # against the str-typed env default.
        self.collection: str = collection if collection else os.getenv("QDRANT_COLLECTION", "code_v1_nomic")
        self.vector_dim = vector_dim
        self._ensure_collection()

    # Payload indexes the collection MUST have for fast filtering. Each is
    # added on collection bootstrap AND on every Hub start so existing
    # collections get new indexes (e.g. `entity` for symbol-level lookups
    # in Phase 5D) without a re-index. Schema uses qdrant's enum so mypy
    # can validate against the typed signature.
    from qdrant_client.http.models import PayloadSchemaType as _PSchema
    _PAYLOAD_INDEXES = (
        ("project", _PSchema.KEYWORD),
        ("file_path", _PSchema.KEYWORD),
        ("machine_id", _PSchema.KEYWORD),
        ("entity", _PSchema.KEYWORD),
    )

    def _ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=self.vector_dim,
                    distance=Distance.COSINE,
                    on_disk=os.getenv("QDRANT_ON_DISK", "true").lower() == "true",
                ),
            )
        self._ensure_payload_indexes()

    def _ensure_payload_indexes(self) -> None:
        """Idempotent: Qdrant's create_payload_index returns OK when the
        index already exists at the requested schema. Failures are logged
        but never raised — startup should not block on index churn."""
        import logging
        log = logging.getLogger(__name__)
        for field_name, schema in self._PAYLOAD_INDEXES:
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception as exc:
                log.warning("qdrant_payload_index_skip field=%s schema=%s err=%s",
                            field_name, schema, exc)

    def upsert(
        self,
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        ids: list[str] | None = None,
    ) -> None:
        if ids is None:
            ids = [self._point_id(payloads[i], i) for i in range(len(vectors))]

        points = [
            PointStruct(id=ids[i], vector=vectors[i], payload=payloads[i])
            for i in range(len(vectors))
        ]
        self.client.upsert(collection_name=self.collection, points=points)

    @staticmethod
    def _point_id(payload: dict[str, Any], index: int) -> str:
        machine_id = payload.get("machine_id")
        project = payload.get("project")
        file_path = payload.get("file_path")
        entity = payload.get("entity", "")
        chunk_id = payload.get("chunk_id", "0")
        if machine_id and project and file_path:
            key = f"{machine_id}:{project}:{file_path}:{entity}:{chunk_id}"
            return str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        return str(uuid.uuid4())

    def search(
        self,
        query_vector: list[float],
        project_scope: str | None = None,
        machine_id: str | None = None,
        limit: int = 10,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        # exact=True bypasses HNSW for deterministic ordering during
        # baseline capture / regression testing. Default False preserves
        # production ANN behaviour.
        results = self.client.query_points(
            collection_name=self.collection,
            query=query_vector,
            query_filter=self._build_filter(machine_id=machine_id, project=project_scope),
            limit=limit,
            with_payload=True,
            search_params=SearchParams(exact=exact) if exact else None,
        )
        return [
            {
                "id": r.id,
                "score": r.score,
                "payload": r.payload,
            }
            for r in results.points
        ]

    def delete_by_file(self, file_path: str, machine_id: str) -> None:
        """Tombstone: remove all vectors for a deleted file."""
        self.client.delete(
            collection_name=self.collection,
            points_selector=Filter(
                must=[
                    FieldCondition(key="file_path", match=MatchValue(value=file_path)),
                    FieldCondition(key="machine_id", match=MatchValue(value=machine_id)),
                ]
            ),
        )

    def delete_by_machine(self, machine_id: str) -> None:
        """Remove all vectors for a machine (disconnect)."""
        self.client.delete(
            collection_name=self.collection,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="machine_id", match=MatchValue(value=machine_id)
                    ),
                ]
            ),
        )

    def stats(
        self,
        machine_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        count_filter = self._build_filter(machine_id=machine_id, project=project)
        exact = os.getenv("QDRANT_STATS_EXACT", "false").lower() == "true"
        if count_filter is not None:
            exact = exact or os.getenv("QDRANT_STATS_SCOPED_EXACT", "true").lower() == "true"
        result = self.client.count(
            collection_name=self.collection,
            count_filter=count_filter,
            exact=exact,
        )
        return {
            "collection": self.collection,
            "points": int(result.count),
            "exact": exact,
        }

    @staticmethod
    def _build_filter(
        machine_id: str | None = None,
        project: str | None = None,
    ) -> Filter | None:
        conditions: list[Any] = []
        if project:
            conditions.append(
                FieldCondition(key="project", match=MatchValue(value=project))
            )
        if machine_id:
            conditions.append(
                FieldCondition(key="machine_id", match=MatchValue(value=machine_id))
            )
        return Filter(must=conditions) if conditions else None

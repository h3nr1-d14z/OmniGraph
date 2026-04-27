"""SQLite content store for file contents and project trees on the Hub."""

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

# Module-level singleton
_store_instance: "ContentStore | None" = None


def get_store() -> "ContentStore":
    """Return the singleton ContentStore instance."""
    global _store_instance
    if _store_instance is None:
        _store_instance = ContentStore()
    return _store_instance


class ContentStore:
    """Store and retrieve file contents indexed by (machine_id, file_path)."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            env_path = os.getenv("CONTENT_DB_PATH")
            if env_path:
                db_path = Path(env_path)
            else:
                home = Path.home()
                db_path = home / ".config" / "omnigraph" / "hub-content.db"
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # Persistent connection for the lifetime of the instance
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                machine_id TEXT NOT NULL,
                project TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content TEXT,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (machine_id, file_path)
            );
            CREATE INDEX IF NOT EXISTS idx_files_project ON files(machine_id, project);
            CREATE TABLE IF NOT EXISTS project_trees (
                machine_id TEXT NOT NULL,
                project TEXT NOT NULL,
                tree_text TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (machine_id, project)
            );
            CREATE TABLE IF NOT EXISTS query_embeddings (
                cache_key TEXT PRIMARY KEY,
                embedding_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
                machine_id UNINDEXED,
                project UNINDEXED,
                file_path,
                content,
                tokenize='unicode61',
                prefix='2 3 4'
            );
            """
        )
        # US-013: idempotent migration to add content_hash column on existing
        # databases. SQLite has no `ADD COLUMN IF NOT EXISTS`, so we catch the
        # duplicate-column OperationalError (analog of pg's
        # isDuplicateColumnError). NULL on legacy rows → self-healing on first
        # touch (full embed path, then hash is recorded).
        try:
            self._conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
            self._conn.commit()
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    def upsert_file(self, machine_id: str, project: str, file_path: str, content: str) -> None:
        self.upsert_files([(machine_id, project, file_path, content)])

    def upsert_files(
        self,
        files: list[tuple[str, str, str, str]],
        content_hashes: dict[tuple[str, str], str] | None = None,
    ) -> None:
        if not files:
            return
        updated_at = int(time.time())
        hashes = content_hashes or {}
        self._conn.executemany(
            """
            INSERT INTO files (machine_id, project, file_path, content, updated_at, content_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_id, file_path) DO UPDATE SET
                project=excluded.project,
                content=excluded.content,
                updated_at=excluded.updated_at,
                content_hash=COALESCE(excluded.content_hash, files.content_hash)
            """,
            [
                (machine_id, project, file_path, content, updated_at, hashes.get((machine_id, file_path)))
                for machine_id, project, file_path, content in files
            ],
        )
        self._sync_fts(files)
        self._conn.commit()

    def get_content_hashes(self) -> dict[tuple[str, str], str]:
        """Bulk preload all (machine_id, file_path) → content_hash for non-NULL rows.

        Used at Hub startup to seed the in-memory short-circuit cache, so a
        watcher restart's catch-up scan doesn't trigger redundant re-embeds.
        """
        rows = self._conn.execute(
            "SELECT machine_id, file_path, content_hash FROM files WHERE content_hash IS NOT NULL",
        ).fetchall()
        return {(machine_id, file_path): content_hash for machine_id, file_path, content_hash in rows}

    def update_content_hash(self, machine_id: str, file_path: str, content_hash: str) -> None:
        """Persist a content hash after a successful Qdrant upsert."""
        self._conn.execute(
            "UPDATE files SET content_hash = ? WHERE machine_id = ? AND file_path = ?",
            (content_hash, machine_id, file_path),
        )
        self._conn.commit()

    def _sync_fts(self, files: list[tuple[str, str, str, str]]) -> None:
        self._conn.executemany(
            "DELETE FROM files_fts WHERE machine_id = ? AND file_path = ?",
            [(machine_id, file_path) for machine_id, _, file_path, _ in files],
        )
        self._conn.executemany(
            """
            INSERT INTO files_fts (machine_id, project, file_path, content)
            VALUES (?, ?, ?, ?)
            """,
            files,
        )

    def get_file(self, machine_id: str, file_path: str) -> str | None:
        row = self._conn.execute(
            "SELECT content FROM files WHERE machine_id = ? AND file_path = ?",
            (machine_id, file_path),
        ).fetchone()
        return row[0] if row else None

    def delete_file(self, machine_id: str, file_path: str) -> None:
        self._conn.execute(
            "DELETE FROM files WHERE machine_id = ? AND file_path = ?",
            (machine_id, file_path),
        )
        self._conn.execute(
            "DELETE FROM files_fts WHERE machine_id = ? AND file_path = ?",
            (machine_id, file_path),
        )
        self._conn.commit()

    def refresh_project_tree(self, machine_id: str, project: str) -> None:
        rows = self._conn.execute(
            """
            SELECT file_path FROM files
            WHERE machine_id = ? AND project = ?
            ORDER BY file_path
            """,
            (machine_id, project),
        ).fetchall()
        if not rows:
            self._conn.execute(
                "DELETE FROM project_trees WHERE machine_id = ? AND project = ?",
                (machine_id, project),
            )
            self._conn.commit()
            return

        tree_text = _build_tree_from_paths([row[0] for row in rows], project)
        self.upsert_project_tree(machine_id, project, tree_text)

    def get_project_tree(self, machine_id: str, project: str) -> str | None:
        row = self._conn.execute(
            "SELECT tree_text FROM project_trees WHERE machine_id = ? AND project = ?",
            (machine_id, project),
        ).fetchone()
        return row[0] if row else None

    def upsert_project_tree(self, machine_id: str, project: str, tree_text: str) -> None:
        self._conn.execute(
            """
            INSERT INTO project_trees (machine_id, project, tree_text, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(machine_id, project) DO UPDATE SET
                tree_text=excluded.tree_text,
                updated_at=excluded.updated_at
            """,
            (machine_id, project, tree_text, int(time.time())),
        )
        self._conn.commit()

    def get_query_embedding(self, cache_key: str) -> list[float] | None:
        row = self._conn.execute(
            "SELECT embedding_json FROM query_embeddings WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def upsert_query_embedding(self, cache_key: str, embedding: list[float]) -> None:
        self._conn.execute(
            """
            INSERT INTO query_embeddings (cache_key, embedding_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                embedding_json=excluded.embedding_json,
                updated_at=excluded.updated_at
            """,
            (cache_key, json.dumps(embedding), int(time.time())),
        )
        self._conn.commit()

    def search_files_exact(
        self,
        query: str,
        machine_id: str | None = None,
        project_scope: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, str | float]]:
        fts_query = _fts_query(query)
        if not fts_query:
            return []

        clauses = ["files_fts MATCH ?"]
        params: list[str] = [fts_query]
        if machine_id:
            clauses.append("machine_id = ?")
            params.append(machine_id)
        if project_scope:
            clauses.append("project = ?")
            params.append(project_scope)

        rows = self._conn.execute(
            f"""
            SELECT machine_id, project, file_path, content, rank
            FROM files_fts
            WHERE {" AND ".join(clauses)}
            ORDER BY rank
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()

        return [
            {
                "machine_id": row_machine_id,
                "project": row_project,
                "file_path": file_path,
                "snippet": (content or "")[:500],
                "score": 1.0 / (1.0 + max(float(rank), 0.0)),
            }
            for row_machine_id, row_project, file_path, content, rank in rows
        ]

    def stats(
        self,
        machine_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        filters: list[str] = []
        params: list[str] = []
        if machine_id:
            filters.append("machine_id = ?")
            params.append(machine_id)
        if project:
            filters.append("project = ?")
            params.append(project)

        files_count = self._count_rows("files", filters, params)
        project_trees_count = self._count_rows("project_trees", filters, params)
        fts_rows_count = self._count_rows("files_fts", filters, params)
        content_hashed = self._count_content_hashed(filters, params)

        return {
            "db_path": self.db_path,
            "files": files_count,
            "project_trees": project_trees_count,
            "global_query_embeddings": self._count_query_embeddings(),
            "fts_rows": fts_rows_count,
            "content_hashed": content_hashed,
        }

    def _count_content_hashed(self, filters: list[str], params: list[str]) -> int:
        clauses = list(filters) + ["content_hash IS NOT NULL"]
        where = f" WHERE {' AND '.join(clauses)}"
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM files{where}",
            params,
        ).fetchone()
        return int(row[0]) if row else 0

    def _count_rows(self, table: str, filters: list[str], params: list[str]) -> int:
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM {table}{where}",
            params,
        ).fetchone()
        return int(row[0]) if row else 0

    def _count_query_embeddings(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM query_embeddings").fetchone()
        return int(row[0]) if row else 0


def _fts_query(query: str) -> str:
    terms = re.findall(r"[\w./-]+", query.lower())
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms[:16])


def _build_tree_from_paths(file_paths: list[str], project: str) -> str:
    root: dict[str, dict] = {}
    for file_path in file_paths:
        parts = [part for part in Path(file_path).parts if part not in ("/", "")]
        if project in parts:
            parts = parts[parts.index(project) + 1 :]
        elif parts:
            parts = [parts[-1]]
        node = root
        for part in parts:
            node = node.setdefault(part, {})

    lines = [f"{project}/"]
    _render_tree(root, lines, "")
    return "\n".join(lines)


def _render_tree(node: dict[str, dict], lines: list[str], prefix: str) -> None:
    items = sorted(node.items(), key=lambda item: (bool(item[1]), item[0]))
    for idx, (name, child) in enumerate(items):
        is_last = idx == len(items) - 1
        connector = "└── " if is_last else "├── "
        suffix = "/" if child else ""
        lines.append(prefix + connector + name + suffix)
        if child:
            extension = "    " if is_last else "│   "
            _render_tree(child, lines, prefix + extension)

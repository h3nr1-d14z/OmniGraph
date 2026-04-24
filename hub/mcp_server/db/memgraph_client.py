"""Memgraph graph client for code dependencies."""

import os
from typing import Any

import neo4j

ALLOWED_RELATIONS = {"DEPENDS_ON", "CALLS", "IMPORTS", "IMPLEMENTS", "EXTENDS"}

# Module-level singleton: one driver per process
_memgraph_instance: "MemgraphCodeGraph | None" = None


def get_memgraph() -> "MemgraphCodeGraph":
    """Return the singleton MemgraphCodeGraph instance."""
    global _memgraph_instance
    if _memgraph_instance is None:
        _memgraph_instance = MemgraphCodeGraph()
    return _memgraph_instance


class MemgraphCodeGraph:
    """Manage code entities and dependencies in Memgraph."""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        self.uri = uri or os.getenv(
            "MEMGRAPH_URI", "bolt://localhost:7687"
        )
        self.user = user or os.getenv("MEMGRAPH_USER", "")
        self.password = password or os.getenv("MEMGRAPH_PASSWORD", "")
        self._driver = neo4j.GraphDatabase.driver(
            self.uri, auth=(self.user, self.password) if self.user else None
        )
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._driver.session() as session:
            # Create indexes for fast lookups
            session.run("CREATE INDEX ON :Entity(file_path)")
            session.run("CREATE INDEX ON :Entity(name)")
            session.run("CREATE INDEX ON :Entity(project)")
            session.run("CREATE INDEX ON :Entity(machine_id)")

    def close(self) -> None:
        self._driver.close()

    def upsert_entity(
        self,
        machine_id: str,
        project: str,
        file_path: str,
        name: str,
        entity_type: str,
        content_hash: str,
    ) -> None:
        query = """
        MERGE (e:Entity {file_path: $file_path, machine_id: $machine_id, name: $name})
        SET e.project = $project,
            e.type = $entity_type,
            e.content_hash = $content_hash,
            e.updated_at = timestamp()
        """
        with self._driver.session() as session:
            session.run(
                query,
                file_path=file_path,
                machine_id=machine_id,
                project=project,
                name=name,
                entity_type=entity_type,
                content_hash=content_hash,
            )

    def upsert_dependency(
        self,
        from_file: str,
        to_file: str,
        relation: str = "DEPENDS_ON",
        machine_id: str = "",
    ) -> None:
        if relation not in ALLOWED_RELATIONS:
            raise ValueError(f"unsupported relation: {relation}")

        query = f"""
        MATCH (a:Entity {{file_path: $from_file, machine_id: $machine_id}})
        MATCH (b:Entity {{file_path: $to_file, machine_id: $machine_id}})
        MERGE (a)-[r:{relation}]->(b)
        SET r.updated_at = timestamp()
        """
        with self._driver.session() as session:
            session.run(
                query,
                from_file=from_file,
                to_file=to_file,
                machine_id=machine_id,
            )

    def upsert_entities(self, entities: list[dict[str, Any]]) -> None:
        if not entities:
            return
        query = """
        UNWIND $entities AS ent
        MERGE (e:Entity {file_path: ent.file_path, machine_id: ent.machine_id, name: ent.name})
        SET e.project = ent.project,
            e.type = ent.type,
            e.content_hash = ent.content_hash,
            e.updated_at = timestamp()
        """
        with self._driver.session() as session:
            session.run(query, entities=entities)

    def delete_file(self, file_path: str, machine_id: str) -> None:
        """Tombstone: delete entity and its edges."""
        query = """
        MATCH (e:Entity {file_path: $file_path, machine_id: $machine_id})
        DETACH DELETE e
        """
        with self._driver.session() as session:
            session.run(query, file_path=file_path, machine_id=machine_id)

    def delete_machine(self, machine_id: str) -> None:
        """Remove all entities for a machine."""
        query = """
        MATCH (e:Entity {machine_id: $machine_id})
        DETACH DELETE e
        """
        with self._driver.session() as session:
            session.run(query, machine_id=machine_id)

    def get_dependencies(
        self,
        entity_name: str,
        direction: str = "both",
        machine_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Get dependency graph for an entity.

        direction: upstream (who calls me), downstream (who I call), both.
        """
        results = {"nodes": [], "edges": []}

        params: dict[str, Any] = {"name": entity_name}
        if machine_id:
            params["machine_id"] = machine_id

        if direction in ("upstream", "both"):
            query = """
            MATCH (caller)-[:DEPENDS_ON]->(target:Entity {name: $name})
            """
            if machine_id:
                query += " WHERE caller.machine_id = $machine_id AND target.machine_id = $machine_id"
            query += " RETURN caller, target"
            with self._driver.session() as session:
                for record in session.run(query, **params):
                    results["nodes"].append(dict(record["caller"]))
                    results["edges"].append(
                        {
                            "from": record["caller"]["file_path"],
                            "to": record["target"]["file_path"],
                            "relation": "DEPENDS_ON",
                        }
                    )

        if direction in ("downstream", "both"):
            query = """
            MATCH (source:Entity {name: $name})-[:DEPENDS_ON]->(callee)
            """
            if machine_id:
                query += " WHERE source.machine_id = $machine_id AND callee.machine_id = $machine_id"
            query += " RETURN source, callee"
            with self._driver.session() as session:
                for record in session.run(query, **params):
                    results["nodes"].append(dict(record["callee"]))
                    results["edges"].append(
                        {
                            "from": record["source"]["file_path"],
                            "to": record["callee"]["file_path"],
                            "relation": "DEPENDS_ON",
                        }
                    )

        # Deduplicate nodes
        seen = set()
        unique_nodes = []
        for n in results["nodes"]:
            key = (n.get("file_path"), n.get("machine_id"))
            if key not in seen:
                seen.add(key)
                unique_nodes.append(n)
        results["nodes"] = unique_nodes

        return results

    def get_project_tree(self, machine_id: str, project: str) -> list[str]:
        """Get all file paths for a project on a machine."""
        query = """
        MATCH (e:Entity {machine_id: $machine_id, project: $project})
        RETURN e.file_path AS path
        ORDER BY path
        """
        with self._driver.session() as session:
            result = session.run(query, machine_id=machine_id, project=project)
            return [record["path"] for record in result]

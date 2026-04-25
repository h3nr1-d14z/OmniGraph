"""Memgraph graph client for code dependencies."""

import os
from typing import Any

import neo4j

ALLOWED_RELATIONS = {"DEPENDS_ON", "CALLS", "IMPORTS", "IMPLEMENTS", "EXTENDS"}


def _node_id(node: Any) -> str:
    data = dict(node)
    return data.get("file_path") or data.get("path") or data.get("name", "")


def _node_kind(node: Any) -> str:
    labels = set(getattr(node, "labels", []))
    for label in ("Entity", "File", "Module", "UnresolvedSymbol", "ResolvedSymbol"):
        if label in labels:
            return label
    return "Node"


def _node_payload(node: Any) -> dict[str, Any]:
    data = dict(node)
    data["id"] = _node_id(node)
    data["kind"] = _node_kind(node)
    return data


def _edge_payload(
    source: Any,
    target: Any,
    relation: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "from": _node_id(source),
        "to": _node_id(target),
        "relation": relation,
        "line": metadata.get("line"),
        "confidence": metadata.get("confidence"),
        "layer": metadata.get("layer"),
        "status": metadata.get("status"),
        "symbol_id": metadata.get("symbol_id"),
        "target_ref": metadata.get("target_ref"),
        "package": metadata.get("package"),
        "language": metadata.get("language"),
        "from_type": _node_kind(source),
        "to_type": _node_kind(target),
        "file_path": dict(source).get("file_path") or dict(source).get("path"),
    }


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
            session.run("CREATE INDEX ON :File(path)")
            session.run("CREATE INDEX ON :Module(name)")
            session.run("CREATE INDEX ON :UnresolvedSymbol(name)")
            session.run("CREATE INDEX ON :ResolvedSymbol(symbol_id)")
            session.run("CREATE INDEX ON :ResolvedSymbol(name)")

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
        MERGE (f:File {path: ent.file_path, machine_id: ent.machine_id})
        SET f.project = ent.project,
            f.updated_at = timestamp()
        MERGE (e:Entity {file_path: ent.file_path, machine_id: ent.machine_id, name: ent.name})
        SET e.project = ent.project,
            e.type = ent.type,
            e.content_hash = ent.content_hash,
            e.start_line = ent.start_line,
            e.end_line = ent.end_line,
            e.updated_at = timestamp()
        MERGE (f)-[r:CONTAINS]->(e)
        SET r.updated_at = timestamp()
        """
        with self._driver.session() as session:
            session.run(query, entities=entities)

    def upsert_relations(self, relations: list[dict[str, Any]]) -> None:
        if not relations:
            return
        imports = [rel for rel in relations if rel["type"] == "IMPORTS"]
        imports_resolved = [rel for rel in relations if rel["type"] == "IMPORTS_RESOLVED"]
        calls = [rel for rel in relations if rel["type"] == "CALLS_SYNTAX" and rel.get("source")]
        calls_resolved = [rel for rel in relations if rel["type"] == "CALLS_RESOLVED" and rel.get("source") and rel.get("symbol_id")]
        references = [rel for rel in relations if rel["type"] == "REFERENCES" and rel.get("source") and rel.get("symbol_id")]
        contains = [rel for rel in relations if rel["type"] == "CONTAINS"]
        with self._driver.session() as session:
            if imports:
                session.run(
                    """
                    UNWIND $relations AS rel
                    MERGE (f:File {path: rel.file_path, machine_id: rel.machine_id})
                    SET f.project = rel.project,
                        f.updated_at = timestamp()
                    MERGE (m:Module {name: rel.target, machine_id: rel.machine_id})
                    SET m.project = rel.project,
                        m.language = "go",
                        m.updated_at = timestamp()
                    MERGE (f)-[r:IMPORTS]->(m)
                    SET r.raw = rel.target,
                        r.line = rel.line,
                        r.confidence = rel.confidence,
                        r.updated_at = timestamp()
                    """,
                    relations=imports,
                )
            if calls:
                session.run(
                    """
                    UNWIND $relations AS rel
                    MATCH (source:Entity {file_path: rel.file_path, machine_id: rel.machine_id, name: rel.source})
                    MERGE (target:UnresolvedSymbol {name: rel.target, machine_id: rel.machine_id})
                    SET target.project = rel.project,
                        target.updated_at = timestamp()
                    MERGE (source)-[r:CALLS_SYNTAX]->(target)
                    SET r.line = rel.line,
                        r.confidence = rel.confidence,
                        r.layer = rel.layer,
                        r.status = rel.status,
                        r.target_ref = rel.target_ref,
                        r.package = rel.package,
                        r.language = rel.language,
                        r.updated_at = timestamp()
                    """,
                    relations=calls,
                )
            if imports_resolved:
                session.run(
                    """
                    UNWIND $relations AS rel
                    MERGE (f:File {path: rel.file_path, machine_id: rel.machine_id})
                    SET f.project = rel.project,
                        f.updated_at = timestamp()
                    MERGE (m:Module {name: rel.target, machine_id: rel.machine_id})
                    SET m.project = rel.project,
                        m.language = rel.language,
                        m.package = rel.package,
                        m.updated_at = timestamp()
                    MERGE (f)-[r:IMPORTS_RESOLVED]->(m)
                    SET r.raw = rel.target,
                        r.line = rel.line,
                        r.confidence = rel.confidence,
                        r.layer = rel.layer,
                        r.status = rel.status,
                        r.target_ref = rel.target_ref,
                        r.package = rel.package,
                        r.language = rel.language,
                        r.updated_at = timestamp()
                    """,
                    relations=imports_resolved,
                )
            if calls_resolved:
                session.run(
                    """
                    UNWIND $relations AS rel
                    MATCH (source:Entity {file_path: rel.file_path, machine_id: rel.machine_id, name: rel.source})
                    MERGE (target:ResolvedSymbol {symbol_id: rel.symbol_id, machine_id: rel.machine_id})
                    SET target.project = rel.project,
                        target.name = rel.target,
                        target.package = rel.package,
                        target.language = rel.language,
                        target.target_ref = rel.target_ref,
                        target.updated_at = timestamp()
                    MERGE (source)-[r:CALLS_RESOLVED]->(target)
                    SET r.line = rel.line,
                        r.confidence = rel.confidence,
                        r.layer = rel.layer,
                        r.status = rel.status,
                        r.symbol_id = rel.symbol_id,
                        r.target_ref = rel.target_ref,
                        r.package = rel.package,
                        r.language = rel.language,
                        r.updated_at = timestamp()
                    """,
                    relations=calls_resolved,
                )
            if references:
                session.run(
                    """
                    UNWIND $relations AS rel
                    MATCH (source:Entity {file_path: rel.file_path, machine_id: rel.machine_id, name: rel.source})
                    MERGE (target:ResolvedSymbol {symbol_id: rel.symbol_id, machine_id: rel.machine_id})
                    SET target.project = rel.project,
                        target.name = rel.target,
                        target.package = rel.package,
                        target.language = rel.language,
                        target.target_ref = rel.target_ref,
                        target.updated_at = timestamp()
                    MERGE (source)-[r:REFERENCES]->(target)
                    SET r.line = rel.line,
                        r.confidence = rel.confidence,
                        r.layer = rel.layer,
                        r.status = rel.status,
                        r.symbol_id = rel.symbol_id,
                        r.target_ref = rel.target_ref,
                        r.package = rel.package,
                        r.language = rel.language,
                        r.updated_at = timestamp()
                    """,
                    relations=references,
                )
            if contains:
                session.run(
                    """
                    UNWIND $relations AS rel
                    MATCH (f:File {path: rel.file_path, machine_id: rel.machine_id})
                    MATCH (e:Entity {file_path: rel.file_path, machine_id: rel.machine_id, name: rel.target})
                    MERGE (f)-[r:CONTAINS]->(e)
                    SET r.line = rel.line,
                        r.confidence = rel.confidence,
                        r.updated_at = timestamp()
                    """,
                    relations=contains,
                )

    def delete_file(self, file_path: str, machine_id: str) -> None:
        """Tombstone: delete file, entities, and their edges."""
        query = """
        MATCH (e:Entity {file_path: $file_path, machine_id: $machine_id})
        DETACH DELETE e
        """
        file_query = """
        MATCH (f:File {path: $file_path, machine_id: $machine_id})
        DETACH DELETE f
        """
        with self._driver.session() as session:
            session.run(query, file_path=file_path, machine_id=machine_id)
            session.run(file_query, file_path=file_path, machine_id=machine_id)
            self._prune_orphans(session, machine_id)

    def delete_machine(self, machine_id: str) -> None:
        """Remove all graph nodes for a machine."""
        query = """
        MATCH (n {machine_id: $machine_id})
        DETACH DELETE n
        """
        with self._driver.session() as session:
            session.run(query, machine_id=machine_id)

    @staticmethod
    def _prune_orphans(session: neo4j.Session, machine_id: str) -> None:
        session.run(
            """
            MATCH (n {machine_id: $machine_id})
            WHERE (n:Module OR n:UnresolvedSymbol OR n:ResolvedSymbol) AND NOT (n)--()
            DELETE n
            """,
            machine_id=machine_id,
        )

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
        if project:
            params["project"] = project

        with self._driver.session() as session:
            root_query = "MATCH (root:Entity {name: $name})"
            root_conditions = []
            if machine_id:
                root_conditions.append("root.machine_id = $machine_id")
            if project:
                root_conditions.append("root.project = $project")
            if root_conditions:
                root_query += " WHERE " + " AND ".join(root_conditions)
            root_query += " RETURN root"
            roots = [record["root"] for record in session.run(root_query, **params)]
            results["nodes"].extend(_node_payload(root) for root in roots)

            if direction in ("upstream", "both"):
                upstream_query = """
                MATCH (source)-[r:DEPENDS_ON|CALLS_SYNTAX|CALLS_RESOLVED|CONTAINS|IMPORTS|IMPORTS_RESOLVED|REFERENCES]->(root:Entity {name: $name})
                """
                conditions = []
                if machine_id:
                    conditions.append("source.machine_id = $machine_id AND root.machine_id = $machine_id")
                if project:
                    conditions.append("source.project = $project AND root.project = $project")
                if conditions:
                    upstream_query += " WHERE " + " AND ".join(conditions)
                upstream_query += " RETURN source, root AS target, type(r) AS relation, properties(r) AS metadata"
                for record in session.run(upstream_query, **params):
                    results["nodes"].append(_node_payload(record["source"]))
                    results["nodes"].append(_node_payload(record["target"]))
                    results["edges"].append(
                        _edge_payload(
                            record["source"],
                            record["target"],
                            record["relation"],
                            dict(record.get("metadata") or {}),
                        )
                    )

            if direction in ("downstream", "both"):
                downstream_query = """
                MATCH (source:Entity {name: $name})-[r:DEPENDS_ON|CALLS_SYNTAX|CALLS_RESOLVED|CONTAINS|IMPORTS|IMPORTS_RESOLVED|REFERENCES]->(target)
                """
                conditions = []
                if machine_id:
                    conditions.append("source.machine_id = $machine_id AND target.machine_id = $machine_id")
                if project:
                    conditions.append("source.project = $project AND target.project = $project")
                if conditions:
                    downstream_query += " WHERE " + " AND ".join(conditions)
                downstream_query += " RETURN source, target, type(r) AS relation, properties(r) AS metadata"
                for record in session.run(downstream_query, **params):
                    results["nodes"].append(_node_payload(record["source"]))
                    results["nodes"].append(_node_payload(record["target"]))
                    results["edges"].append(
                        _edge_payload(
                            record["source"],
                            record["target"],
                            record["relation"],
                            dict(record.get("metadata") or {}),
                        )
                    )

                import_query = """
                MATCH (file:File)-[:CONTAINS]->(source:Entity {name: $name})
                MATCH (file)-[r:IMPORTS|IMPORTS_RESOLVED]->(target)
                """
                import_conditions = []
                if machine_id:
                    import_conditions.append("file.machine_id = $machine_id AND source.machine_id = $machine_id AND target.machine_id = $machine_id")
                if project:
                    import_conditions.append("file.project = $project AND source.project = $project AND target.project = $project")
                if import_conditions:
                    import_query += " WHERE " + " AND ".join(import_conditions)
                import_query += " RETURN file AS source, target, type(r) AS relation, properties(r) AS metadata"
                for record in session.run(import_query, **params):
                    results["nodes"].append(_node_payload(record["source"]))
                    results["nodes"].append(_node_payload(record["target"]))
                    results["edges"].append(
                        _edge_payload(
                            record["source"],
                            record["target"],
                            record["relation"],
                            dict(record.get("metadata") or {}),
                        )
                    )

        seen = set()
        unique_nodes = []
        for n in results["nodes"]:
            key = (n.get("id"), n.get("machine_id"))
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

    def stats(
        self,
        machine_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, int]:
        params: dict[str, Any] = {}
        entity_where = self._scope_where("e", machine_id, project, params)
        relation_where = self._scope_where("a", machine_id, project, params, partner_alias="b")

        entity_query = "MATCH (e:Entity)"
        if entity_where:
            entity_query += f" WHERE {entity_where}"
        entity_query += " RETURN count(e) AS count"

        edge_query = "MATCH (a)-[r]->(b)"
        if relation_where:
            edge_query += f" WHERE {relation_where}"
        edge_query += " RETURN count(r) AS count"

        with self._driver.session() as session:
            entities = session.run(entity_query, **params).single()["count"]
            edges = session.run(edge_query, **params).single()["count"]

        return {
            "entities": entities,
            "edges": edges,
        }

    @staticmethod
    def _scope_where(
        alias: str,
        machine_id: str | None,
        project: str | None,
        params: dict[str, Any],
        partner_alias: str | None = None,
    ) -> str:
        clauses: list[str] = []
        aliases = [alias]
        if partner_alias:
            aliases.append(partner_alias)
        if machine_id:
            params["machine_id"] = machine_id
            clauses.extend(f"{current}.machine_id = $machine_id" for current in aliases)
        if project:
            params["project"] = project
            clauses.extend(f"{current}.project = $project" for current in aliases)
        return " AND ".join(clauses)

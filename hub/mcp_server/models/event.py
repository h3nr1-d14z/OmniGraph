"""Event models shared between Watcher and Hub API."""

from pydantic import BaseModel


class Entity(BaseModel):
    name: str
    type: str
    line: int
    start_line: int | None = None
    end_line: int | None = None


class Relation(BaseModel):
    type: str
    source: str | None = None
    target: str
    target_type: str | None = None
    line: int | None = None
    confidence: str | None = None
    layer: str | None = None
    status: str | None = None
    symbol_id: str | None = None
    target_ref: str | None = None
    package: str | None = None
    language: str | None = None


class FileEvent(BaseModel):
    type: str
    path: str
    old_path: str | None = None
    project: str
    machine_id: str
    timestamp: int
    content_hash: str | None = None
    content: str | None = None
    entities: list[Entity] | None = None
    relations: list[Relation] | None = None


class BatchPayload(BaseModel):
    machine_id: str
    project: str
    events: list[FileEvent]
    sent_at: str | None = None

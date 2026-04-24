"""Event models shared between Watcher and Hub API."""

from pydantic import BaseModel


class Entity(BaseModel):
    name: str
    type: str
    line: int
    start_line: int | None = None
    end_line: int | None = None


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


class BatchPayload(BaseModel):
    machine_id: str
    project: str
    events: list[FileEvent]
    sent_at: str | None = None

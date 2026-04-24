"""Pydantic models for MCP tool inputs/outputs."""

from pydantic import BaseModel, Field


class SemanticSearchInput(BaseModel):
    query: str = Field(..., description="Natural language description of the code to find")
    project_scope: str | None = Field(None, description="Optional project name to filter results")
    machine_id: str | None = Field(None, description="Optional machine identifier to filter results")


class SemanticSearchResult(BaseModel):
    file_path: str
    machine_id: str
    project: str
    snippet: str
    score: float


class DependencyGraphInput(BaseModel):
    entity_name: str = Field(..., description="Name of the function, class, or component")
    direction: str = Field(..., description="upstream, downstream, or both")
    machine_id: str = Field("local", description="Identifier of the machine where the entity resides")


class DependencyGraphResult(BaseModel):
    nodes: list[dict]
    edges: list[dict]


class ReadFileInput(BaseModel):
    machine_id: str = Field(..., description="Identifier of the machine where the file resides")
    file_path: str = Field(..., description="Absolute or relative path to the file")


class ProjectTreeInput(BaseModel):
    machine_id: str = Field(..., description="Identifier of the machine")
    project_name: str = Field(..., description="Name of the project to list")

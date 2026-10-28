"""Pydantic schemas for the documents API — Story 7.1 / FR-37."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DocumentACLEntryResponse(BaseModel):
    id: str
    employee_id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentResponse(BaseModel):
    id: str
    original_filename: str
    file_size: int
    mime_type: str
    owner_id: str
    status: str
    failure_reason: str | None = None
    indexing_status: str
    indexing_failure_reason: str | None = None
    version: int
    page_count: int | None = None
    processing_started_at: datetime | None = None
    processing_completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    acl: list[DocumentACLEntryResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]
    total: int


class DocumentUploadResponse(BaseModel):
    document: DocumentResponse
    warnings: list[str] = Field(default_factory=list)
    message: str = "Document uploaded successfully and queued for processing."


class SemanticSearchResultResponse(BaseModel):
    document_id: str
    filename: str
    heading: str | None
    content: str
    score: float
    document_version: int

    model_config = {"from_attributes": True}


class SemanticSearchResponse(BaseModel):
    results: list[SemanticSearchResultResponse]

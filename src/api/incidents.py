"""Read-only incident history API (SPEC-006).

Exposes the `incidents` table — populated by `_on_cluster_event` in
`src/main.py` for every watch-loop event that matches a rule, across all
four domains (Kubernetes, API backends, VM platforms, Azure).
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Incident
from src.database.postgres import get_db

logger = structlog.get_logger()
router = APIRouter(prefix="/api/incidents", tags=["Incidents"])


class IncidentSummary(BaseModel):
    """One incident record."""

    id: uuid.UUID
    title: str
    severity: str
    status: str
    event_type: str
    namespace: str | None = None
    resource_kind: str | None = None
    resource_name: str | None = None
    root_cause: str | None = None
    rca_confidence: float | None = None
    created_at: str
    resolved_at: str | None = None
    extra_data: dict[str, Any]

    @classmethod
    def from_model(cls, incident: Incident) -> "IncidentSummary":
        return cls(
            id=incident.id,
            title=incident.title,
            severity=incident.severity,
            status=incident.status,
            event_type=incident.event_type,
            namespace=incident.namespace,
            resource_kind=incident.resource_kind,
            resource_name=incident.resource_name,
            root_cause=incident.root_cause,
            rca_confidence=incident.rca_confidence,
            created_at=incident.created_at.isoformat(),
            resolved_at=incident.resolved_at.isoformat() if incident.resolved_at else None,
            extra_data=incident.extra_data or {},
        )


class IncidentListResponse(BaseModel):
    total: int
    items: list[IncidentSummary]


@router.get("", response_model=IncidentListResponse)
async def list_incidents(
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = Query(None),
    event_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> IncidentListResponse:
    """List incidents, most recent first, optionally filtered."""
    filters = []
    if status_filter:
        filters.append(Incident.status == status_filter)
    if severity:
        filters.append(Incident.severity == severity)
    if event_type:
        filters.append(Incident.event_type == event_type)

    count_stmt = select(func.count()).select_from(Incident)
    list_stmt = select(Incident).order_by(Incident.created_at.desc()).limit(limit).offset(offset)
    for f in filters:
        count_stmt = count_stmt.where(f)
        list_stmt = list_stmt.where(f)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(list_stmt)).scalars().all()

    return IncidentListResponse(
        total=total, items=[IncidentSummary.from_model(row) for row in rows]
    )


@router.get("/{incident_id}", response_model=IncidentSummary)
async def get_incident(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> IncidentSummary:
    """Get one incident by ID."""
    incident = await session.get(Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return IncidentSummary.from_model(incident)

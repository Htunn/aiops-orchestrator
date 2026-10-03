"""Unit tests for the read-only incidents API (SPEC-006)."""

import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.api.incidents import router
from src.database.models import Incident
from src.database.postgres import get_db


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeResult:
    def __init__(self, items=None, scalar=None):
        self._items = items or []
        self._scalar = scalar

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return _FakeScalars(self._items)


class FakeSession:
    """Minimal AsyncSession stand-in: first execute() = count, second = list."""

    def __init__(self, incidents):
        self.incidents = incidents
        self._call = 0

    async def execute(self, _stmt):
        self._call += 1
        if self._call == 1:
            return _FakeResult(scalar=len(self.incidents))
        return _FakeResult(items=self.incidents)

    async def get(self, _model, incident_id):
        return next((i for i in self.incidents if i.id == incident_id), None)


def _make_incident(**overrides) -> Incident:
    defaults = {
        "id": uuid.uuid4(),
        "title": "CrashLoopBackOff: payments/web-7f9c",
        "severity": "critical",
        "status": "resolved",
        "event_type": "crash_loop",
        "namespace": "payments",
        "resource_kind": "Pod",
        "resource_name": "web-7f9c",
        "root_cause": "Container exceeded memory limits",
        "rca_confidence": 0.85,
        "created_at": datetime.now(UTC),
        "resolved_at": datetime.now(UTC),
        "extra_data": {"failure_pattern": "OOMKill"},
    }
    defaults.update(overrides)
    return Incident(**defaults)


def _build_client(incidents: list[Incident]) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    async def _override_get_db():
        yield FakeSession(incidents)

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app)


class TestListIncidents:
    def test_list_returns_all(self):
        incidents = [_make_incident(), _make_incident(event_type="azure_resource_unhealthy")]
        client = _build_client(incidents)
        resp = client.get("/api/incidents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_list_item_shape(self):
        incidents = [_make_incident()]
        client = _build_client(incidents)
        resp = client.get("/api/incidents")
        item = resp.json()["items"][0]
        assert item["root_cause"] == "Container exceeded memory limits"
        assert item["rca_confidence"] == 0.85
        assert item["extra_data"] == {"failure_pattern": "OOMKill"}

    def test_list_empty(self):
        client = _build_client([])
        resp = client.get("/api/incidents")
        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "items": []}


class TestGetIncident:
    def test_get_by_id_found(self):
        incident = _make_incident()
        client = _build_client([incident])
        resp = client.get(f"/api/incidents/{incident.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == str(incident.id)

    def test_get_by_id_not_found(self):
        client = _build_client([_make_incident()])
        resp = client.get(f"/api/incidents/{uuid.uuid4()}")
        assert resp.status_code == 404

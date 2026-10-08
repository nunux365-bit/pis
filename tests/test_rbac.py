"""
RBAC parity: employees cannot hit agents/skills/integrations APIs (matches shell nav).

Requires PostgreSQL (DATABASE_URL). If the DB is down, tests skip.
"""

from __future__ import annotations


def test_agents_forbidden_for_employee(client_employee):
    r = client_employee.get("/api/agents/")
    assert r.status_code == 403


def test_skills_forbidden_for_employee(client_employee):
    r = client_employee.get("/api/skills/")
    assert r.status_code == 403


def test_integrations_forbidden_for_employee(client_employee):
    r = client_employee.get("/api/integrations")
    assert r.status_code == 403

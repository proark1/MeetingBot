"""Smoke tests for health and docs endpoints."""

import pytest
import httpx


@pytest.mark.asyncio
async def test_health_endpoint(client: httpx.AsyncClient):
    """GET /health should return 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_docs_accessible(client: httpx.AsyncClient):
    """GET /api/docs should return 200."""
    resp = await client.get("/api/docs")
    assert resp.status_code == 200

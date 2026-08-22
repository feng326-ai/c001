"""Shared pytest fixtures only; tests belong in explicit test modules."""

import pytest


@pytest.fixture(scope="session")
def base_url():
    """API base used only by explicit external/integration tests."""
    return "http://localhost:8000"


@pytest.fixture
def api_client(base_url):
    """HTTP client fixture; constructing it does not contact a live service."""
    import requests

    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    yield session
    session.close()

"""Cross-site protection: other websites must not be able to start a
conversion (or the run check) from a visitor's browser, or read the API.

Browsers mark requests with Sec-Fetch-Site and Origin; pages cannot forge
them. The LLM is a fake, so no API calls are made.
"""

import pytest
from fastapi.testclient import TestClient
from test_notebook import FakeProvider

import api.main as api_main

PDF = b"%PDF-1.7 fake"
ENDPOINTS = ["/convert", "/convert/stream"]


@pytest.fixture
def client():
    provider = FakeProvider()
    overrides = api_main.app.dependency_overrides
    overrides[api_main.get_llm_provider] = lambda: provider
    # TestClient sends Host: testserver.
    yield TestClient(api_main.app), provider
    overrides.clear()


def _post(test_client, path, **headers):
    return test_client.post(
        path,
        files={"file": ("paper.pdf", PDF, "application/pdf")},
        headers=headers,
    )


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        # Another website, as a browser marks it.
        {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        # Another app on this machine (e.g. localhost:3000) is same-site
        # but not same-origin.
        {"Sec-Fetch-Site": "same-site", "Origin": "http://testserver:3000"},
        # Older browsers: Origin only.
        {"Origin": "https://evil.example"},
        {"Origin": "http://testserver:3000"},
        # Sandboxed iframes and some redirects send "null".
        {"Origin": "null"},
    ],
)
def test_cross_site_requests_are_refused(client, path, headers):
    test_client, provider = client
    response = _post(test_client, path, **headers)
    assert response.status_code == 403
    assert "other websites" in response.json()["detail"]
    assert provider.calls == []  # refused before any paid request


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize(
    "headers",
    [
        # The dashboard itself.
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"},
        # Typed or bookmarked by the user.
        {"Sec-Fetch-Site": "none"},
        # Older browsers on the same origin.
        {"Origin": "http://testserver"},
        # curl, scripts: no browser headers at all.
        {},
    ],
)
def test_same_origin_and_scripts_are_allowed(client, path, headers):
    test_client, provider = client
    response = _post(test_client, path, **headers)
    assert response.status_code == 200
    assert len(provider.calls) == 1


def test_no_cors_permission_for_other_sites(client):
    test_client, _ = client
    for request in (
        lambda: test_client.get(
            "/api/config", headers={"Origin": "https://evil.example"}
        ),
        lambda: test_client.options(
            "/convert/stream",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        ),
    ):
        response = request()
        assert "access-control-allow-origin" not in response.headers
        assert "access-control-allow-credentials" not in response.headers

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


# ---------------------------------------------------------------------------
# F1: allowed hosts (DNS rebinding)
# ---------------------------------------------------------------------------

import api.security as security  # noqa: E402


@pytest.fixture
def real_hosts(monkeypatch):
    """Use the real allowed hosts (no test host), from a given config."""

    def use(config=None):
        hosts = security.load_allowed_hosts(config or {})
        monkeypatch.setattr(security, "allowed_hosts", lambda: hosts)
        return hosts

    return use


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ("localhost:8000", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("[::1]:8000", "::1"),
        ("Notebooks.Example.com", "notebooks.example.com"),
        ("", ""),
        (None, ""),
        ("[bad", ""),
    ],
)
def test_hostname_parsing(header, name):
    assert security.hostname(header) == name


@pytest.mark.parametrize(
    ("host", "allowed", "expected"),
    [
        ("localhost:8000", security.LOOPBACK_HOSTS, True),
        ("rebind.attacker.example", security.LOOPBACK_HOSTS, False),
        ("a.example.com", frozenset({"*.example.com"}), True),
        ("example.com.evil", frozenset({"*.example.com"}), False),
        ("anything", frozenset({"*"}), True),
        ("", frozenset({"*"}), False),
    ],
)
def test_host_allowed(host, allowed, expected):
    assert security.host_allowed(host, allowed) is expected


def test_allowed_hosts_from_config(caplog):
    hosts = security.load_allowed_hosts(
        {"app": {"allowed_hosts": [" Notebooks.Example.com "]}}
    )
    assert hosts == security.LOOPBACK_HOSTS | {"notebooks.example.com"}
    assert security.load_allowed_hosts({"app": {"allowed_hosts": "x"}}) == (
        security.LOOPBACK_HOSTS
    )
    assert "Ignoring invalid [app] allowed_hosts" in caplog.text


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"],
)
def test_loopback_hosts_are_served(real_hosts, base_url):
    real_hosts()
    response = TestClient(api_main.app, base_url=base_url).get("/health")
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/", "/api/config", "/api/notebooks"])
def test_rebinding_host_is_refused(real_hosts, path):
    real_hosts()
    client = TestClient(
        api_main.app, base_url="http://rebind.attacker.example:8000"
    )
    response = client.get(path)
    assert response.status_code == 400
    assert response.text == "Invalid host header."


@pytest.mark.parametrize("path", ENDPOINTS)
def test_rebinding_post_is_refused_before_work(client, real_hosts, path):
    _, provider = client
    real_hosts()
    rebound = TestClient(
        api_main.app, base_url="http://rebind.attacker.example:8000"
    )
    response = _post(
        rebound,
        path,
        Origin="http://rebind.attacker.example:8000",
        **{"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 400
    assert provider.calls == []


def test_deploy_host_from_config(real_hosts):
    real_hosts({"app": {"allowed_hosts": ["notebooks.example.com"]}})
    client = TestClient(api_main.app, base_url="https://notebooks.example.com")
    assert client.get("/health").status_code == 200


def test_run_check_needs_a_localhost_host_header():
    from notebook_builder.verify import VerifySettings, verify_allowed

    settings = VerifySettings()
    assert verify_allowed("127.0.0.1", settings, host_is_local=True)
    # From 127.0.0.1 but for another name: DNS rebinding.
    assert not verify_allowed("127.0.0.1", settings, host_is_local=False)
    remote = VerifySettings(allow_remote=True)
    assert verify_allowed("10.0.0.5", remote, host_is_local=False)


def test_config_does_not_offer_run_check_to_rebound_host():
    from api.routes.config import describe_configuration

    local = describe_configuration("127.0.0.1", host_is_local=True)
    rebound = describe_configuration("127.0.0.1", host_is_local=False)
    assert local["verify"]["available"] is True
    assert rebound["verify"]["available"] is False


# ---------------------------------------------------------------------------
# F5: security headers
# ---------------------------------------------------------------------------


def test_security_headers_on_dashboard_and_api(client):
    test_client, _ = client
    for path in ("/", "/api/config", "/static/app.js", "/nope"):
        headers = test_client.get(path).headers
        csp = headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" not in headers


def test_docs_keep_their_cdn_but_cannot_be_framed(client):
    test_client, _ = client
    headers = test_client.get("/docs").headers
    assert headers["content-security-policy"] == "frame-ancestors 'none'"
    assert headers["x-frame-options"] == "DENY"


def test_hsts_only_over_https(real_hosts):
    real_hosts({"app": {"allowed_hosts": ["notebooks.example.com"]}})
    client = TestClient(api_main.app, base_url="https://notebooks.example.com")
    headers = client.get("/health").headers
    assert headers["strict-transport-security"] == "max-age=31536000"


def test_headers_set_by_a_route_are_kept():
    import asyncio

    sent = []

    async def app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-frame-options", b"SAMEORIGIN")],
            }
        )

    async def send(message):
        sent.append(message)

    middleware = security.SecurityHeadersMiddleware(app)
    asyncio.run(middleware({"type": "http", "path": "/"}, None, send))
    names = [name for name, _ in sent[0]["headers"]]
    assert names.count(b"x-frame-options") == 1
    assert (b"x-frame-options", b"SAMEORIGIN") in sent[0]["headers"]


def test_non_http_scopes_pass_through():
    import asyncio

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(
        security.SecurityHeadersMiddleware(app)(
            {"type": "lifespan"}, None, None
        )
    )
    asyncio.run(
        security.HostCheckMiddleware(app)({"type": "lifespan"}, None, None)
    )
    assert seen == ["lifespan", "lifespan"]


# ---------------------------------------------------------------------------
# F4: a cap on concurrent conversions
# ---------------------------------------------------------------------------

import api.routes.convert as convert_routes  # noqa: E402


@pytest.mark.parametrize("path", ENDPOINTS)
def test_too_many_conversions_get_429(client, monkeypatch, path):
    test_client, provider = client
    slots = security.ConversionSlots(limit=1)
    slots.active = 1  # one already running
    monkeypatch.setattr(convert_routes, "CONVERSIONS", slots)
    response = _post(test_client, path)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert "already running" in response.json()["detail"]
    assert provider.calls == []


@pytest.mark.parametrize("path", ENDPOINTS)
def test_slot_is_released_after_a_conversion(client, monkeypatch, path):
    test_client, _ = client
    slots = security.ConversionSlots(limit=1)
    monkeypatch.setattr(convert_routes, "CONVERSIONS", slots)
    assert _post(test_client, path).status_code == 200
    assert _post(test_client, path).status_code == 200
    assert slots.active == 0


def test_slot_is_released_after_a_failure(monkeypatch):
    from providers import ProviderError

    slots = security.ConversionSlots(limit=1)
    monkeypatch.setattr(convert_routes, "CONVERSIONS", slots)
    overrides = api_main.app.dependency_overrides
    overrides[api_main.get_llm_provider] = lambda: FakeProvider(
        error=ProviderError("down")
    )
    try:
        response = _post(TestClient(api_main.app), "/convert")
    finally:
        overrides.clear()
    assert response.status_code == 502
    assert slots.active == 0


def test_bad_upload_does_not_take_a_slot(client, monkeypatch):
    test_client, _ = client
    slots = security.ConversionSlots(limit=1)
    monkeypatch.setattr(convert_routes, "CONVERSIONS", slots)
    response = test_client.post(
        "/convert", files={"file": ("x.txt", b"nope", "text/plain")}
    )
    assert response.status_code == 400
    assert slots.active == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(3, 3), (0, 2), (True, 2), ("4", 2)],
)
def test_max_conversions_setting(value, expected):
    config = {"app": {"max_concurrent_conversions": value}}
    assert security.load_max_conversions(config) == expected


def test_shipped_limits():
    assert security.load_max_conversions() == 2
    assert security.load_allowed_hosts() == security.LOOPBACK_HOSTS
    assert security.ConversionSlots().limit == 2


def test_release_never_goes_negative():
    slots = security.ConversionSlots(limit=1)
    slots.release()
    assert slots.active == 0


# ---------------------------------------------------------------------------
# F7: uploads are not read past the limit
# ---------------------------------------------------------------------------


def test_upload_is_read_only_up_to_the_limit(monkeypatch):
    import asyncio

    monkeypatch.setattr(convert_routes, "MAX_UPLOAD_BYTES", 10)
    asked = []

    class Upload:
        filename = "paper.pdf"

        async def read(self, size=-1):
            asked.append(size)
            return b"%PDF-" + b"x" * 100

    with pytest.raises(Exception) as info:
        asyncio.run(convert_routes._read_pdf(Upload()))
    assert asked == [11]  # limit + 1, never the whole file
    assert info.value.status_code == 400

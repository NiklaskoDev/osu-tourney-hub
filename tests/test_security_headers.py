from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

EXPECTED_CSP_DIRECTIVES = [
    "default-src 'self'",
    "img-src 'self' https://a.ppy.sh https://assets.ppy.sh data:",
    "style-src 'self'",
    "script-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self' https://osu.ppy.sh",
]


def test_security_headers_are_present_on_api_responses(client):
    response = client.get("/healthz")

    assert response.headers["Content-Security-Policy"].split("; ") == EXPECTED_CSP_DIRECTIVES
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_security_headers_are_present_on_errors_and_static_files(client):
    for path in ("/does-not-exist", "/static/css/tokens.css"):
        response = client.get(path)

        assert "Content-Security-Policy" in response.headers, path
        assert response.headers["X-Content-Type-Options"] == "nosniff", path


def test_hsts_is_sent_when_public_url_is_https(client):
    assert client.get("/healthz").headers["Strict-Transport-Security"].startswith("max-age=")


def test_hsts_is_not_sent_when_public_url_is_http(valid_env, clock):
    valid_env["PUBLIC_URL"] = "http://localhost:8080"
    settings = Settings.from_env(valid_env)

    with TestClient(create_app(settings, clock)) as http_client:
        response = http_client.get("/healthz")

    assert "Strict-Transport-Security" not in response.headers
    assert "Content-Security-Policy" in response.headers

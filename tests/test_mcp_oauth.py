"""Tests for the self-hosted OAuth 2.1 provider and its login gate.

The provider (``DomcityOAuthProvider``) inherits FastMCP's full in-memory OAuth
contract and only overrides ``authorize()`` to redirect to a login page. These
tests cover that single gap: the txn stash/resume round-trip, expiry/invalid
handling, and the ``/mcp/login`` route's credential check.
"""

import time

import pytest
from fastapi.testclient import TestClient
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

import main
from mcp_oauth import DomcityOAuthProvider

REDIRECT_URI = "http://localhost:9999/callback"


def _make_provider() -> DomcityOAuthProvider:
    return DomcityOAuthProvider(
        base_url="http://testserver/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )


async def _register_client(provider: DomcityOAuthProvider) -> OAuthClientInformationFull:
    client = OAuthClientInformationFull(
        client_id="test-client",
        client_secret="test-secret",
        redirect_uris=[REDIRECT_URI],
    )
    await provider.register_client(client)
    return client


def _params() -> AuthorizationParams:
    return AuthorizationParams(
        state="state-123",
        scopes=None,
        code_challenge="a" * 43,  # PKCE S256 challenge (any URL-safe string)
        redirect_uri=REDIRECT_URI,
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


def test_login_url_is_under_mcp_mount():
    provider = _make_provider()
    assert provider.login_url == "http://testserver/mcp/login"


async def test_authorize_stashes_txn_and_redirects_to_login():
    provider = _make_provider()
    client = await _register_client(provider)

    url = await provider.authorize(client, _params())

    assert url.startswith(provider.login_url + "?txn=")
    txn = url.split("txn=", 1)[1]
    assert txn in provider._pending  # request is parked, no code minted yet


async def test_complete_authorize_resumes_and_mints_code():
    provider = _make_provider()
    client = await _register_client(provider)
    url = await provider.authorize(client, _params())
    txn = url.split("txn=", 1)[1]

    redirect = await provider.complete_authorize(txn)

    assert redirect.startswith(REDIRECT_URI)
    assert "code=" in redirect
    assert "state=state-123" in redirect
    assert txn not in provider._pending  # consumed exactly once


async def test_complete_authorize_is_single_use():
    provider = _make_provider()
    client = await _register_client(provider)
    url = await provider.authorize(client, _params())
    txn = url.split("txn=", 1)[1]
    await provider.complete_authorize(txn)

    with pytest.raises(ValueError):
        await provider.complete_authorize(txn)


async def test_complete_authorize_rejects_unknown_txn():
    provider = _make_provider()
    with pytest.raises(ValueError):
        await provider.complete_authorize("does-not-exist")


async def test_expired_txn_is_swept_and_rejected():
    provider = _make_provider()
    client = await _register_client(provider)
    url = await provider.authorize(client, _params())
    txn = url.split("txn=", 1)[1]

    # Force the stashed entry to be already expired.
    client_id, params, _ = provider._pending[txn]
    provider._pending[txn] = (client_id, params, time.time() - 1)

    with pytest.raises(ValueError):
        await provider.complete_authorize(txn)


# --- Login route (the single credential entry point) ----------------------- #


def test_login_get_renders_form_with_txn():
    with TestClient(main.app) as client:
        r = client.get("/mcp/login?txn=abc123")
        assert r.status_code == 200
        assert 'name="txn" value="abc123"' in r.text
        assert 'name="username"' in r.text
        assert 'name="password"' in r.text


def test_login_get_without_txn_is_bad_request():
    with TestClient(main.app) as client:
        r = client.get("/mcp/login")
        assert r.status_code == 400


def test_login_post_wrong_credentials_rejected():
    with TestClient(main.app) as client:
        r = client.post(
            "/mcp/login",
            data={"txn": "irrelevant", "username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401
        assert "Wrong username or password" in r.text


def test_login_post_missing_txn_is_bad_request():
    with TestClient(main.app) as client:
        r = client.post(
            "/mcp/login",
            data={"txn": "", "username": "admin", "password": "test-pw"},
            follow_redirects=False,
        )
        assert r.status_code == 400

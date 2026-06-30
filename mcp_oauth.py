"""Self-hosted OAuth 2.1 provider for the MCP server.

Claude's web/mobile custom connectors require OAuth 2.1 — static bearer tokens
and basic auth are rejected. FastMCP's ``InMemoryOAuthProvider`` already
implements the full 10-method OAuth contract (Dynamic Client Registration,
PKCE-bound auth codes, opaque access/refresh tokens, revocation) with in-memory
storage. The ONLY gap is that its ``authorize()`` auto-approves every request
with no user interaction.

``DomcityOAuthProvider`` fills that single gap: ``authorize()`` redirects the
browser to our own ``/mcp/login`` page (stashing the pending request under a
random transaction id), and ``complete_authorize()`` resumes the parent flow
once the username/password from ``.env`` have been validated. Everything else
(token issuance, refresh, verification) is inherited unchanged.

Storage is in-process: on restart Claude transparently re-runs Dynamic Client
Registration and a fresh login. No DB schema change — respects the infra lock.
"""

import secrets
import time

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from loguru import logger
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

# Pending authorization requests waiting on the login screen expire quickly —
# this is just the few seconds/minutes between the redirect and the user
# submitting the form.
_PENDING_TTL_SECONDS = 5 * 60


class DomcityOAuthProvider(InMemoryOAuthProvider):
    """In-memory OAuth provider that gates ``authorize()`` behind a login page."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # txn -> (client_id, params, expires_at)
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}

    @property
    def login_url(self) -> str:
        """External URL of the login page, e.g. https://domain/mcp/login."""
        return str(self.base_url).rstrip("/") + "/login"

    def _sweep_pending(self) -> None:
        now = time.time()
        expired = [txn for txn, (_, _, exp) in self._pending.items() if exp < now]
        for txn in expired:
            self._pending.pop(txn, None)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Instead of auto-approving, stash the request and send the browser to
        our login page. Returns the redirect URL the SDK 302s the user to."""
        self._sweep_pending()
        txn = secrets.token_urlsafe(32)
        self._pending[txn] = (client.client_id, params, time.time() + _PENDING_TTL_SECONDS)
        logger.info("MCP OAuth: authorize stashed as txn for client {}", client.client_id)
        return f"{self.login_url}?txn={txn}"

    async def complete_authorize(self, txn: str) -> str:
        """Resume a stashed authorization after successful login. Returns the
        client redirect URL (with code + state) the parent provider mints."""
        self._sweep_pending()
        entry = self._pending.pop(txn, None)
        if entry is None:
            raise ValueError("login session expired or invalid — please retry")
        client_id, params, _ = entry
        client = await self.get_client(client_id)
        if client is None:
            raise ValueError("unknown OAuth client — please retry")
        # Parent mints a PKCE-bound authorization code and returns the client's
        # redirect_uri with code + state. No further user interaction.
        return await super().authorize(client, params)

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from settings import settings

COOKIE_NAME = "domcity_session"
SESSION_VALUE = "ok"
PUBLIC_PATHS = ("/login", "/static", "/healthz", "/favicon.ico")

_serializer = URLSafeSerializer(settings.secret_key, salt="domcity-auth")


def make_session_token() -> str:
    return _serializer.dumps(SESSION_VALUE)


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        return _serializer.loads(token) == SESSION_VALUE
    except BadSignature:
        return False


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)
        token = request.cookies.get(COOKIE_NAME)
        if not verify_session_token(token):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

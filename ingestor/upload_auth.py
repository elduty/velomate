"""Sign-in check for the upload page: ask Grafana who the browser is.

The browser's HTTP Basic Authorization header is forwarded unchanged to
Grafana. Only users whose role in org 1 is Editor or Admin may upload.
Successes are cached for AUTH_CACHE_SECONDS, keyed by the SHA-256 of the
header, so neither the password nor the header is kept. Failures are never
cached.
"""
import base64
import binascii
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

AUTH_CACHE_SECONDS = 300
UPLOAD_ROLES = frozenset({"Editor", "Admin"})
ORG_ID = 1


@dataclass(frozen=True)
class AuthDecision:
    ok: bool
    status: int
    reason: str
    login: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the Authorization header must only go to Grafana."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Unauthorized(Exception):
    pass


class _Unavailable(Exception):
    pass


def _is_basic(header):
    if not header:
        return False
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return False
    try:
        base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


class GrafanaAuth:
    def __init__(self, base_url: str, *,
                 cache_seconds: float = AUTH_CACHE_SECONDS,
                 timeout: float = 5.0, clock=time.monotonic):
        self._base = base_url.rstrip("/")
        self._ttl = cache_seconds
        self._timeout = timeout
        self._clock = clock
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self._cache = {}  # sha256(header) -> (expires, AuthDecision)

    def _get(self, path, authorization):
        req = urllib.request.Request(
            self._base + path, headers={"Authorization": authorization})
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise _Unavailable()
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 401:
                raise _Unauthorized()
            raise _Unavailable()
        except (urllib.error.URLError, http.client.HTTPException, OSError,
                ValueError):
            raise _Unavailable()

    def check(self, authorization: str | None) -> AuthDecision:
        if not _is_basic(authorization):
            return AuthDecision(False, 401, "sign in required", None)
        key = hashlib.sha256(authorization.encode()).hexdigest()
        now = self._clock()
        hit = self._cache.get(key)
        if hit and now < hit[0]:
            return hit[1]
        self._cache.pop(key, None)
        try:
            user = self._get("/api/user", authorization)
            orgs = self._get("/api/user/orgs", authorization)
            login = user["login"]
            if not isinstance(login, str) or not login:
                raise _Unavailable()
            role = next((o.get("role") for o in orgs
                         if o.get("orgId") == ORG_ID), None)
        except _Unauthorized:
            return AuthDecision(False, 401, "wrong username or password", None)
        except (_Unavailable, KeyError, TypeError, AttributeError):
            return AuthDecision(False, 502, "could not confirm sign-in with Grafana", None)
        if role not in UPLOAD_ROLES:
            return AuthDecision(False, 403,
                                "uploading needs the Editor or Admin role",
                                login)
        decision = AuthDecision(True, 200, "ok", login)
        self._cache[key] = (now + self._ttl, decision)
        return decision

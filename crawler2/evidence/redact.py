"""Display forms of URLs for manifests and reports (design §8).

A collected URL may carry a session token or a signed-URL signature. The
original stays in the protected provenance (W4/M3 rows, referenced by id);
exported documents carry a *display* URL with those values replaced, plus the
SHA-256 of the original so a reviewer with access can tie the two together.
Redaction never changes stored evidence.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "REDACTED"
_SECRET_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "code",
        "credential",
        "hash",
        "hmac",
        "jwt",
        "key",
        "pass",
        "passwd",
        "password",
        "phpsessid",
        "policy",
        "secret",
        "session",
        "session_id",
        "sessionid",
        "sid",
        "sig",
        "signature",
        "st",
        "token",
        "jsessionid",
    }
)
_SECRET_PREFIXES = ("x-amz-", "x-goog-", "aws", "token", "auth")


def _secret(name: str) -> bool:
    n = name.lower()
    return n in _SECRET_NAMES or n.startswith(_SECRET_PREFIXES) or n.endswith(("token", "_sig"))


def display_url(url: str) -> tuple[str, bool]:
    """(display form, whether anything was redacted). Userinfo and secret parameters go."""
    parts = urlsplit(url)
    redacted = False
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        redacted = True
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        cleaned = []
        for name, value in pairs:
            if _secret(name) and value:
                cleaned.append((name, REDACTED))
                redacted = True
            else:
                cleaned.append((name, value))
        if redacted:
            query = urlencode(cleaned, safe="/:")
    if parts.fragment:
        redacted = redacted or bool(parts.fragment)
    if not redacted:
        return url, False
    return urlunsplit((parts.scheme, netloc, parts.path, query, "")), True


def url_doc(url: str) -> dict[str, object]:
    """Manifest form of a URL: display value, redaction flag, digest of the original."""
    shown, redacted = display_url(url)
    return {
        "url": shown,
        "redacted": redacted,
        "original_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
    }

"""The NPMPlus API client, and the certificate checks its proxy hosts need.

Both used to live in the core's clients.py. They are the destination's own
business: nothing outside this package talks to the NPMPlus API.
"""

from datetime import datetime, timezone

from ...clients import ApiError, JsonClient


class NpmPlusClient:
    def __init__(self, name, url, identity, secret, verify_tls=True):
        self.name = name
        self.http = JsonClient(url, verify_tls=verify_tls)
        self.identity = identity
        self.secret = secret
        self._authenticated = False

    def _login(self):
        if not self.identity or not self.secret:
            raise ApiError(f"Set credentials for the {self.name} NPMPlus instance")
        try:
            response = self.http.request(
                "/api/tokens",
                method="POST",
                payload={"identity": self.identity, "secret": self.secret},
            )
        except ApiError as error:
            # A rejected sign-in is nearly always the identity: the API expects the
            # account's email address, and a username is refused the same way as a
            # wrong password.
            if error.status in (400, 401):
                raise ApiError(
                    f"{error}. An NPMPlus instance signs in with the email address on the account and its "
                    "password, so check the identity and secret saved for the "
                    f"{self.name} NPMPlus instance",
                    status=error.status,
                ) from error
            raise
        # Cookie-based sessions are handled by the cookie jar; if the API also
        # (or instead) returns a bearer token in the body, send it as well.
        token = response.get("token") if isinstance(response, dict) else None
        if token:
            self.http.extra_headers["authorization"] = f"Bearer {token}"
        self._authenticated = True

    def request(self, path, method="GET", payload=None):
        if not self._authenticated:
            self._login()
        return self.http.request(path, method=method, payload=payload)

    def proxy_hosts(self, expand_certificate=True):
        path = (
            "/api/nginx/proxy-hosts?expand=certificate"
            if expand_certificate
            else "/api/nginx/proxy-hosts"
        )
        return self.request(path)

    def certificates(self):
        return self.request("/api/nginx/certificates")

    def create_proxy_host(self, payload):
        return self.request("/api/nginx/proxy-hosts", method="POST", payload=payload)

    def update_proxy_host(self, host_id, payload):
        return self.request(f"/api/nginx/proxy-hosts/{host_id}", method="PUT", payload=payload)

    def delete_proxy_host(self, host_id):
        return self.request(f"/api/nginx/proxy-hosts/{host_id}", method="DELETE")


def _parse_expiry(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _certificate_covers(names, domain_name):
    """True when a certificate's names cover one hostname: itself or its wildcard."""
    requested = str(domain_name).lower()
    parts = requested.split(".")
    if len(parts) < 2:
        return False
    return requested in names or "*." + ".".join(parts[1:]) in names


def select_certificate(certificates, domain_name):
    """Return the unexpired certificate covering domain_name (latest expiry wins)."""
    return select_shared_certificate(certificates, [domain_name])


def select_shared_certificate(certificates, domain_names):
    """The unexpired certificate covering every one of domain_names, or None.

    NPMPlus gives a proxy host a single certificate, so a host that answers to
    several names - a guest plus its aliases - needs one certificate covering all
    of them, normally the wildcard for the public domain.
    """
    requested = [str(name).lower() for name in domain_names if str(name or "").strip()]
    if not requested:
        return None
    now = datetime.now(timezone.utc)
    best = None
    best_expiry = None
    for certificate in certificates:
        if certificate.get("is_deleted") in (True, 1, "1"):
            continue
        names = {str(name).lower() for name in certificate.get("domain_names", [])}
        if not all(_certificate_covers(names, name) for name in requested):
            continue
        expiry = _parse_expiry(certificate.get("expires_on"))
        if expiry is not None and expiry <= now:
            continue
        sort_key = expiry or datetime.min.replace(tzinfo=timezone.utc)
        if best is None or sort_key > best_expiry:
            best, best_expiry = certificate, sort_key
    return best

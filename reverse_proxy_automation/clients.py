import json
import ssl
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)


class ApiError(RuntimeError):
    """An API call that failed, carrying the HTTP status when there was one."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def unverified_tls_context():
    """A TLS context that accepts a certificate this machine does not trust.

    A LAN NPMPlus or PVE Scripts Local normally serves a certificate signed by a
    private CA, which fails with CERTIFICATE_VERIFY_FAILED before any request is
    sent. This context is used only for connections the user marked as
    self-signed; every other connection keeps verifying.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _is_certificate_failure(reason):
    """True when a connection failure was caused by an untrusted certificate."""
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    text = str(reason).upper()
    return "CERTIFICATE_VERIFY_FAILED" in text or "CERTIFICATE VERIFY FAILED" in text


def _unreachable(url, reason):
    """The ApiError for a failed connection, hinting when TLS is the cause."""
    message = f"Could not reach {url}: {str(reason).strip()}"
    if _is_certificate_failure(reason):
        message += (
            ". This machine does not trust that certificate: turn on the 'self-signed' switch for this "
            "connection, or install the issuing CA on this machine. Check the port as well: NPMPlus "
            "serves its admin API on 81, for example https://host:81"
        )
    return ApiError(message)


def _looks_like_html(content_type, body):
    """True when a reply is a web page rather than an API response."""
    return "html" in (content_type or "").lower() or body.lstrip()[:1] == "<"


_PORT_HINT = (
    "NPMPlus serves its admin API on port 81 (for example https://host:81), while 80 and 443 usually "
    "answer with the proxy host or the default page"
)


def _web_page_hint(content_type, body):
    """The extra sentence for an endpoint that answered with a web page, not the API."""
    if not _looks_like_html(content_type, body):
        return ""
    return ". That is a web page rather than the API: check the URL's port. " + _PORT_HINT


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


class SameOriginRedirectHandler(HTTPRedirectHandler):
    """Refuse redirects that would send a request to a different origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        source = urlsplit(request.full_url)
        destination = urlsplit(urljoin(request.full_url, new_url))

        def origin(parsed_url):
            scheme = parsed_url.scheme.lower()
            port = parsed_url.port
            if port is None:
                port = {"http": 80, "https": 443}.get(scheme)
            return scheme, parsed_url.hostname, port

        source_origin = origin(source)
        destination_origin = origin(destination)
        if source_origin != destination_origin:
            return None
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def _redirect_hint(method, status, location):
    """The extra sentence for a redirect reply urllib refused to replay a body across.

    A 301/302/303 is followed by resending the request as a bodyless GET, and a
    307/308 is not followed at all, so a login answered with a redirect can never
    succeed: it comes back anonymous and every later call looks unauthenticated.
    """
    where = f" to {location}" if location else ""
    return (
        f". That is a redirect ({status}){where}, not an API reply: a {method} is never resent across a "
        "307/308, and across a 301/302/303 it arrives without its body, so the login could only come back "
        "unauthenticated and fail later as a permission error. Use the URL it points at"
    )


def _not_json(url, method, served_url, content_type, body):
    """The ApiError for a body that is not JSON, naming what came back instead."""
    message = f"{method} {url} did not return JSON"
    notes = []
    if served_url and served_url != url:
        notes.append(f"it was redirected to {served_url}")
        if method != "GET":
            notes.append(f"the redirect turned the {method} into a GET and dropped the request body")
    if content_type:
        notes.append(f"content-type {content_type.split(';')[0].strip()}")
    if notes:
        message += " (" + "; ".join(notes) + ")"
    message += _web_page_hint(content_type, body)
    detail = " ".join(body.split())[:140]
    if detail:
        message += f". The reply was: {detail}"
    return ApiError(message)


def _redirect_dropped_body(url, method, served_url):
    """The ApiError for a redirect that silently turns a POST (or PUT) into a GET.

    urllib follows a 301/302/303 by resending the request as a GET without its
    body, so a login POST redirected to another URL can never succeed: it comes
    back as an anonymous GET, and every later call looks unauthenticated.
    """
    return ApiError(
        f"{method} {url} was redirected to {served_url}: following that redirect turns the {method} into "
        "a GET and drops the request body, so the call can never succeed. Use the URL that answers "
        "without redirecting instead. " + _PORT_HINT
    )


class JsonClient:
    def __init__(self, base_url, verify_tls=True):
        self.base_url = base_url.rstrip("/") + "/"
        self.verify_tls = bool(verify_tls)
        handlers = [HTTPCookieProcessor(CookieJar()), SameOriginRedirectHandler()]
        if not self.verify_tls:
            handlers.append(HTTPSHandler(context=unverified_tls_context()))
        self.opener = build_opener(*handlers)
        self.extra_headers = {}

    def request(self, path, method="GET", payload=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"accept": "application/json", **self.extra_headers}
        if body is not None:
            headers["content-type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                # urllib follows redirects by itself, so the reply can come from a
                # different URL than the one asked for; both are reported on failure.
                served_url = getattr(response, "url", url) or url
                content_type = response.headers.get("content-type", "") if response.headers else ""
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").strip()
            error.close()
            content_type = error.headers.get("content-type", "") if error.headers else ""
            if error.code in _REDIRECT_STATUSES:
                location = error.headers.get("location", "") if error.headers else ""
                hint = _redirect_hint(method, error.code, location)
            else:
                hint = _web_page_hint(content_type, detail)
            raise ApiError(
                f"{method} {url} failed ({error.code}): {detail}{hint}",
                status=error.code,
            ) from error
        except URLError as error:
            raise _unreachable(url, error.reason) from error
        except ssl.SSLError as error:
            # A TLS failure urllib did not wrap; the certificate hint still applies.
            raise _unreachable(url, error) from error
        if served_url != url and method not in ("GET", "HEAD"):
            raise _redirect_dropped_body(url, method, served_url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise _not_json(url, method, served_url, content_type, raw) from error


class PveScriptsLocalClient:
    def __init__(self, url, username="", password="", verify_tls=True):
        self.http = JsonClient(url, verify_tls=verify_tls)
        self.username = username
        self.password = password

    def installed_scripts(self, refresh_lxc_config=False, progress_callback=None):
        if bool(self.username) != bool(self.password):
            raise ApiError(
                "Set both the PVE Scripts Local username and password, or leave both blank"
            )
        if self.username:
            self.http.request(
                "/api/auth/login",
                method="POST",
                payload={"username": self.username, "password": self.password},
            )
        scripts = self._read_installed_scripts()
        if refresh_lxc_config:
            fresh_configs = {}
            fresh_ips = {}
            fresh_ports = {}
            candidates = []
            refresh_errors = []
            for script in scripts:
                container_id = str(script.get("container_id") or "").strip()
                server_id = str(script.get("server_id") or "").strip()
                if not container_id or script.get("is_vm"):
                    continue
                if not server_id:
                    refresh_errors.append((
                        script.get("id"), container_id,
                        f"Cannot refresh Web UI IP for PSL record {script.get('id')} "
                        f"(container {container_id}): "
                        "the PSL record has no Proxmox server ID",
                    ))
                    continue
                if script.get("execution_mode") != "ssh":
                    refresh_errors.append((
                        script.get("id"), container_id,
                        f"Cannot refresh Web UI IP for PSL record {script.get('id')} "
                        f"(container {container_id}): "
                        "PSL auto-detection requires an SSH-mode record",
                    ))
                    continue
                try:
                    script_id = int(script.get("id"))
                except (TypeError, ValueError):
                    refresh_errors.append((
                        script.get("id"), container_id,
                        f"Cannot refresh Web UI IP for container {container_id}: "
                        "the PSL record has no valid script ID",
                    ))
                    continue
                try:
                    numeric_server_id = int(server_id)
                except ValueError:
                    refresh_errors.append((
                        script_id, container_id,
                        f"Cannot refresh Web UI IP for PSL record {script_id} "
                        f"(container {container_id}): invalid Proxmox server ID {server_id!r}",
                    ))
                    continue
                try:
                    stored_port = int(script.get("web_ui_port"))
                except (TypeError, ValueError):
                    stored_port = 0
                candidates.append((
                    script_id, server_id, numeric_server_id, container_id, stored_port
                ))

            if candidates:
                server_ids = sorted({candidate[2] for candidate in candidates})
                response = self.http.request(
                    "/api/trpc/installedScripts.getContainerStatuses",
                    method="POST",
                    payload={"json": {"serverIds": server_ids}},
                )
                status_data = response.get("result", {}).get("data", {}) if isinstance(response, dict) else {}
                status_data = status_data.get("json", status_data) if isinstance(status_data, dict) else status_data
                if not isinstance(status_data, dict) or not status_data.get("success"):
                    detail = (
                        status_data.get("error", "unexpected response")
                        if isinstance(status_data, dict)
                        else "unexpected response"
                    )
                    raise ApiError(f"Could not read Proxmox container statuses from PSL: {detail}")
                status_map = status_data.get("statusMap")
                if not isinstance(status_map, dict):
                    raise ApiError("PVE Scripts Local returned no Proxmox container status map")
            else:
                status_map = {}

            refreshable = [
                (script_id, server_id, container_id, stored_port)
                for script_id, server_id, numeric_server_id, container_id, stored_port in candidates
                if str(status_map.get(f"{numeric_server_id}:{container_id}", "")).lower() != "stopped"
            ]

            total_records = len(refreshable)
            for index, (script_id, server_id, container_id, stored_port) in enumerate(refreshable, start=1):
                if progress_callback is not None:
                    progress_callback(index, total_records, script_id, container_id)
                try:
                    trpc_input = quote(
                        json.dumps({"json": {"scriptId": script_id, "forceSync": True}}),
                        safe="",
                    )
                    response = self.http.request(
                        f"/api/trpc/installedScripts.getLXCConfig?input={trpc_input}"
                    )
                    data = response.get("result", {}).get("data", {}) if isinstance(response, dict) else {}
                    data = data.get("json", data) if isinstance(data, dict) else data
                    if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("config"), dict):
                        detail = data.get("error", "unexpected response") if isinstance(data, dict) else "unexpected response"
                        raise ApiError(
                            f"Could not refresh LXC config for PSL record {script_id} "
                            f"(container {container_id}) on PSL server {server_id}: {detail}"
                        )
                    fresh_config = data["config"]

                    response = self.http.request(
                        "/api/trpc/installedScripts.autoDetectWebUI",
                        method="POST",
                        payload={"json": {"id": script_id}},
                    )
                    data = response.get("result", {}).get("data", {}) if isinstance(response, dict) else {}
                    data = data.get("json", data) if isinstance(data, dict) else data
                    if (
                        not isinstance(data, dict)
                        or not data.get("success")
                        or not isinstance(data.get("detectedIp"), str)
                        or not data["detectedIp"].strip()
                    ):
                        detail = data.get("error", "unexpected response") if isinstance(data, dict) else "unexpected response"
                        raise ApiError(
                            f"Could not refresh Web UI IP for PSL record {script_id} "
                            f"(container {container_id}) on PSL server {server_id}: {detail}"
                        )
                    detected_ip = data["detectedIp"].strip()
                    detected_port = data.get("detectedPort")
                    try:
                        detected_port = int(detected_port)
                    except (TypeError, ValueError):
                        detected_port = 0
                    if (
                        1 <= stored_port <= 65535
                        and 1 <= detected_port <= 65535
                        and detected_port != stored_port
                    ):
                        response = self.http.request(
                            "/api/trpc/installedScripts.updateInstalledScript",
                            method="POST",
                            payload={
                                "json": {
                                    "id": script_id,
                                    "web_ui_port": detected_port,
                                }
                            },
                        )
                        update_data = response.get("result", {}).get("data", {}) if isinstance(response, dict) else {}
                        update_data = update_data.get("json", update_data) if isinstance(update_data, dict) else update_data
                        if not isinstance(update_data, dict) or not update_data.get("success"):
                            detail = (
                                update_data.get("error", "unexpected response")
                                if isinstance(update_data, dict)
                                else "unexpected response"
                            )
                            raise ApiError(
                                f"Could not save detected Web UI port {detected_port} "
                                f"for PSL record {script_id} (container {container_id}): {detail}"
                            )
                except ApiError as error:
                    refresh_errors.append((script_id, container_id, str(error)))
                    continue

                fresh_configs[script_id] = fresh_config
                fresh_ips[script_id] = detected_ip
                if 1 <= detected_port <= 65535:
                    fresh_ports[script_id] = detected_port
                elif 1 <= stored_port <= 65535:
                    fresh_ports[script_id] = stored_port

            if fresh_configs or fresh_ips or fresh_ports:
                scripts = self._read_installed_scripts()
                for script in scripts:
                    try:
                        script_id = int(script.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if script_id in fresh_configs:
                        script["lxc_config"] = fresh_configs[script_id]
                    if script_id in fresh_ips:
                        script["web_ui_ip"] = fresh_ips[script_id]
                    if script_id in fresh_ports:
                        script["web_ui_port"] = fresh_ports[script_id]
            for script in scripts:
                script_id = script.get("id")
                container_id = str(script.get("container_id") or "").strip()
                for failed_id, failed_container, error in refresh_errors:
                    same_id = str(script_id) == str(failed_id)
                    same_unidentified_record = (
                        failed_id is None and container_id and container_id == failed_container
                    )
                    if same_id or same_unidentified_record:
                        script["_refresh_error"] = error
                        break
        return scripts

    def _read_installed_scripts(self):
        response = self.http.request("/api/trpc/installedScripts.getAllInstalledScripts")
        payload = response.get("result", {}).get("data", {}) if isinstance(response, dict) else {}
        payload = payload.get("json", payload) if isinstance(payload, dict) else payload
        if not isinstance(payload, dict) or not payload.get("success") or not isinstance(payload.get("scripts"), list):
            detail = payload.get("error", "unexpected response") if isinstance(payload, dict) else "unexpected response"
            raise ApiError(f"PVE Scripts Local did not return installed scripts: {detail}")
        return payload["scripts"]

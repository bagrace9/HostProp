import argparse
import base64
import hmac
import ipaddress
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if __package__ in (None, ""):
    # Launched as a plain file -- `python reverse_proxy_automation/webapp.py`, or an IDE's
    # "Run Python File" -- instead of as the package. PEP 366: put the package's parent on
    # the path and give this module its real package name, so the relative import below
    # resolves to the package's modules instead of dying with "attempted relative import
    # with no known parent package". The package is then imported normally, so clients.py,
    # planner.py, service.py and sync.py are never loaded twice (once as loose top-level
    # modules, once as package members).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "reverse_proxy_automation"

from .service import ConfigStore, SyncService, public_config, unresolved_secret_references


WEB_DIR = Path(__file__).parent / "web"
DEFAULT_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _normalized_host(host_value):
    """Return a normalized Host name, or None for malformed authority values."""
    try:
        parsed = urlparse("//" + host_value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    hostname = hostname.rstrip(".").lower()
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        if not re.fullmatch(r"[a-z0-9.-]+", hostname):
            return None
        return hostname


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, config_store, sync_service, app_password, allowed_hosts=None):
        super().__init__(address, handler)
        self.config_store = config_store
        self.sync_service = sync_service
        self.app_password = app_password
        self.allowed_hosts = set(DEFAULT_ALLOWED_HOSTS)
        for host in allowed_hosts or ():
            normalized = _normalized_host(host)
            if normalized is None:
                raise ValueError(f"Invalid WEB_ALLOWED_HOSTS entry: {host!r}")
            self.allowed_hosts.add(normalized)


class Handler(BaseHTTPRequestHandler):
    server_version = "ReverseProxyAutomation/1.0"

    def log_message(self, format_string, *args):
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def _authorized(self):
        password = self.server.app_password
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            _, supplied = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8"))

    def _trusted_host(self):
        host = _normalized_host(self.headers.get("Host", ""))
        return host is not None and host in self.server.allowed_hosts

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(raw)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("x-frame-options", "DENY")
        self.send_header("referrer-policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload))

    def _body(self):
        content_length = self.headers.get("content-length")
        if content_length is None:
            raise ValueError("Invalid request body length")
        try:
            size = int(content_length)
        except ValueError:
            raise ValueError("Invalid request body length")
        if size <= 0:
            raise ValueError("Invalid request body length")
        if size > 65536:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(size)
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError as error:
            raise ValueError("Request must contain valid JSON") from error
        if not isinstance(data, dict):
            raise ValueError("Request JSON must be an object")
        return data

    def _csrf_ok(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin != "null":
            if urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower():
                return False
        elif origin == "null":
            return False
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        return content_type == "application/json"

    def _serve_index(self):
        asset = WEB_DIR / "index.html"
        try:
            body = asset.read_bytes()
        except OSError:
            self._json(500, {"error": f"Dashboard asset is missing: {asset}"})
            return
        self._send(200, body, "text/html; charset=utf-8")

    def do_GET(self):
        if not self._trusted_host():
            self._json(421, {"error": "Untrusted Host header"})
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Proxy automation"')
            self.send_header("content-length", "0")
            self.end_headers()
            return
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                self._serve_index()
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            elif path == "/api/config":
                self._json(200, public_config(self.server.config_store.load()))
            elif path == "/api/status":
                self._json(200, self.server.sync_service.get_status())
            elif path == "/api/entries":
                self._json(200, {"ok": True, **self.server.sync_service.list_entries()})
            else:
                self._json(404, {"error": "Not found"})
        except (ValueError, OSError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            # Without this a failed read closes the connection with no body at all,
            # and the page can only report a JSON parse failure.
            self._json(502, {"error": str(error)})

    def do_POST(self):
        if not self._trusted_host():
            self._json(421, {"error": "Untrusted Host header"})
            return
        if not self._authorized():
            self._json(401, {"error": "Authentication required"})
            return
        if not self._csrf_ok():
            self._json(403, {"error": "Cross-origin or non-JSON request rejected"})
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                config = self.server.config_store.save(self._body())
                self.server.sync_service.wake_loop()
                self._json(200, {"ok": True, "config": public_config(config)})
            elif path == "/api/preview":
                self._json(200, self.server.sync_service.run_once(
                    apply_changes=False,
                    refresh_lxc_config=True,
                ))
            elif path == "/api/apply":
                config = self.server.config_store.load()
                if not config.get("pve_local_url"):
                    raise ValueError("Save your connection settings before applying")
                self._json(200, self.server.sync_service.run_once(
                    apply_changes=True,
                    refresh_lxc_config=True,
                ))
            elif path == "/api/test":
                self._json(200, self.server.sync_service.test_connection(str(self._body().get("target", ""))))
            elif path == "/api/watch":
                config = self.server.config_store.save(self._body())
                self.server.sync_service.wake_loop()
                self._json(200, {"ok": True, "config": public_config(config)})
            elif path == "/api/entries/ownership":
                body = self._body()
                self._json(200, self.server.sync_service.set_ownership(
                    str(body.get("instance", "")),
                    str(body.get("host_id", "")),
                    str(body.get("action", "")),
                ))
            elif path == "/api/entries/aliases":
                body = self._body()
                self._json(200, self.server.sync_service.set_entry_aliases(
                    str(body.get("instance", "")),
                    str(body.get("host_id", "")),
                    body.get("aliases", []),
                ))
            elif path == "/api/entries/port":
                body = self._body()
                self._json(200, self.server.sync_service.set_entry_port(
                    str(body.get("instance", "")),
                    str(body.get("host_id", "")),
                    body.get("port"),
                ))
            elif path == "/api/entries/delete":
                body = self._body()
                self._json(200, self.server.sync_service.delete_entry(
                    str(body.get("instance", "")),
                    str(body.get("host_id", "")),
                ))
            else:
                self._json(404, {"error": "Not found"})
        except (ValueError, OSError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            self._json(502, {"error": str(error)})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the reverse-proxy automation dashboard and watcher.")
    parser.add_argument("--host", default=os.environ.get("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8088")))
    parser.add_argument("--config", default=os.environ.get("APP_CONFIG_PATH", "./data/app-config.json"))
    parser.add_argument("--state", default=os.environ.get("SYNC_STATE_PATH", "./data/sync-state.json"))
    args = parser.parse_args(argv)

    app_password = os.environ.get("APP_PASSWORD", "")
    allowed_hosts = set(DEFAULT_ALLOWED_HOSTS)
    for configured_host in os.environ.get("WEB_ALLOWED_HOSTS", "").split(","):
        configured_host = configured_host.strip()
        if not configured_host:
            continue
        normalized = _normalized_host(configured_host)
        if normalized is None:
            parser.error(f"Invalid WEB_ALLOWED_HOSTS entry: {configured_host!r}")
        allowed_hosts.add(normalized)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not app_password:
        parser.error("Set APP_PASSWORD before binding to a network interface other than localhost")
    if not (WEB_DIR / "index.html").is_file():
        parser.error(f"Dashboard asset is missing: {WEB_DIR / 'index.html'}")

    config_store = ConfigStore(args.config)
    # A missing variable would otherwise surface only as a failed run, so mention
    # it at startup too. The dashboard still starts, since the variable may be
    # added to the service environment before the next run.
    try:
        saved_config = config_store.load()
    except (OSError, json.JSONDecodeError) as error:
        print(f"Warning: could not read {args.config}: {error}")
    else:
        for description, name in unresolved_secret_references(saved_config):
            print(f"Warning: {description} reads {name} from the environment, which is not set.")
    sync_service = SyncService(config_store, args.state)
    server = DashboardServer(
        (args.host, args.port),
        Handler,
        config_store,
        sync_service,
        app_password,
        allowed_hosts,
    )
    print(f"Dashboard listening at http://{args.host}:{args.port}/")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("APP_PASSWORD authentication is enabled; use HTTPS or a trusted TLS reverse proxy on untrusted networks.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping dashboard.")
    finally:
        sync_service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
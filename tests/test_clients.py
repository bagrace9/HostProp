import json
import ssl
import unittest
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPSHandler

from reverse_proxy_automation.clients import (
    ApiError,
    JsonClient,
    NpmPlusClient,
    PveScriptsLocalClient,
    unverified_tls_context,
)


class BrokenOpener:
    """Stands in for the opener so a connection failure can be simulated."""

    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=None):
        raise self.error


def https_handler(client):
    return next(handler for handler in client.opener.handlers if isinstance(handler, HTTPSHandler))


class TlsContextTests(unittest.TestCase):
    def test_the_shared_context_accepts_an_untrusted_certificate(self):
        context = unverified_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_a_normal_client_keeps_verifying(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81")
        self.assertTrue(client.verify_tls)
        # Whether urllib installed a context of its own or falls back to the standard
        # one, an un-switched client keeps checking the certificate and the hostname.
        context = https_handler(client)._context or ssl.create_default_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_a_self_signed_client_stops_verifying(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81", verify_tls=False)
        self.assertFalse(client.verify_tls)
        context = https_handler(client)._context
        self.assertIsNotNone(context)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)


class ConnectionFailureTests(unittest.TestCase):
    def test_an_untrusted_certificate_is_explained(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81")
        client.opener = BrokenOpener(URLError(ssl.SSLCertVerificationError(
            1, "certificate verify failed: self-signed certificate"
        )))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/tokens", method="POST", payload={})
        message = str(caught.exception)
        self.assertIn("Could not reach https://npmplus-one.internal.example.net:81/api/tokens", message)
        self.assertIn("self-signed", message)
        self.assertIn("self-signed certificate", message)

    def test_an_unwrapped_tls_failure_is_explained_too(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81")
        client.opener = BrokenOpener(ssl.SSLError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/nginx/proxy-hosts")
        self.assertIn("self-signed", str(caught.exception))

    def test_a_wrapped_certificate_message_is_recognised(self):
        client = JsonClient("https://internal.example.net:81")
        client.opener = BrokenOpener(URLError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/nginx/certificates")
        self.assertIn("install the issuing CA", str(caught.exception))

    def test_another_failure_keeps_the_plain_message(self):
        client = JsonClient("http://psl.internal.example.net:3000")
        client.opener = BrokenOpener(URLError(ConnectionRefusedError(10061, "connection refused")))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/trpc/installedScripts.getAllInstalledScripts")
        message = str(caught.exception)
        self.assertIn("Could not reach http://psl.internal.example.net:3000", message)
        self.assertIn("connection refused", message)
        self.assertNotIn("self-signed", message)


class EndpointSwitchTests(unittest.TestCase):
    def test_both_client_types_forward_the_switch(self):
        self.assertFalse(NpmPlusClient("Lab", "https://internal.example.net:81", "a", "s", verify_tls=False).http.verify_tls)
        self.assertTrue(NpmPlusClient("Lab", "https://internal.example.net:81", "a", "s").http.verify_tls)
        self.assertFalse(PveScriptsLocalClient("https://psl.internal.example.net", "u", "p", verify_tls=False).http.verify_tls)
        self.assertTrue(PveScriptsLocalClient("https://psl.internal.example.net").http.verify_tls)


class FakeResponse:
    """Stands in for the response so a reply body and its URL can be simulated."""

    def __init__(self, body, url, content_type="application/json"):
        self.body = body.encode("utf-8")
        self.url = url
        self.headers = {"content-type": content_type}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class RespondingOpener:
    """Stands in for the opener so a reply body can be simulated."""

    def __init__(self, response):
        self.response = response

    def open(self, request, timeout=None):
        return self.response


class SequencedOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return self.responses.pop(0)


def failing_opener(test, error):
    """An opener that raises error, closed again when the test finishes."""
    test.addCleanup(getattr(error, "close", lambda: None))
    return BrokenOpener(error)


class FailedReplyTests(unittest.TestCase):
    def test_a_web_page_reply_points_at_the_port(self):
        client = JsonClient("https://npmplus-one.internal.example.net")
        client.opener = RespondingOpener(FakeResponse(
            "<html><head><title>Default Page</title></head></html>",
            "https://npmplus-one.internal.example.net/api/nginx/proxy-hosts",
            "text/html; charset=utf-8",
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/nginx/proxy-hosts")
        message = str(caught.exception)
        self.assertIn("GET https://npmplus-one.internal.example.net/api/nginx/proxy-hosts did not return JSON", message)
        self.assertIn("content-type text/html", message)
        self.assertIn("web page rather than the API", message)
        self.assertIn("port 81", message)
        self.assertIn("Default Page", message)

    def test_a_redirected_post_is_refused_and_explained(self):
        client = JsonClient("http://npmplus-one.internal.example.net", verify_tls=False)
        client.opener = RespondingOpener(FakeResponse(
            "<html><head><title>Default Page</title></head></html>",
            "https://npmplus-one.internal.example.net/api/tokens",
            "text/html",
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/tokens", method="POST", payload={"identity": "a", "secret": "b"})
        message = str(caught.exception)
        self.assertIn(
            "POST http://npmplus-one.internal.example.net/api/tokens was redirected to "
            "https://npmplus-one.internal.example.net/api/tokens",
            message,
        )
        self.assertIn("drops the request body", message)
        self.assertIn("can never succeed", message)
        self.assertIn("port 81", message)

    def test_a_redirected_post_is_refused_even_when_the_reply_parses(self):
        # A downgraded POST could otherwise look like a successful login.
        client = JsonClient("http://internal.example.net")
        client.opener = RespondingOpener(FakeResponse(
            '{"expires": "1970-01-01T00:00:00.000Z"}', "https://internal.example.net/api/tokens"
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/tokens", method="POST", payload={"identity": "a", "secret": "b"})
        self.assertIn("was redirected to https://internal.example.net/api/tokens", str(caught.exception))
        self.assertIn("turns the POST into a GET", str(caught.exception))

    def test_a_put_is_refused_on_a_redirect_too(self):
        client = JsonClient("http://internal.example.net:81")
        client.opener = RespondingOpener(FakeResponse(
            "{}", "https://internal.example.net:81/api/nginx/proxy-hosts/7"
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/nginx/proxy-hosts/7", method="PUT", payload={"domain_names": ["a.lan"]})
        self.assertIn("turns the PUT into a GET", str(caught.exception))

    def test_a_redirected_get_is_reported_but_still_read(self):
        client = JsonClient("http://internal.example.net:81")
        client.opener = RespondingOpener(FakeResponse(
            "<html><head><title>Default Page</title></head></html>",
            "https://internal.example.net:81/api/nginx/certificates",
            "text/html",
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/nginx/certificates")
        message = str(caught.exception)
        self.assertIn("(it was redirected to https://internal.example.net:81/api/nginx/certificates", message)
        self.assertNotIn("drops the request body", message)

    def test_a_json_reply_still_parses(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81")
        client.opener = RespondingOpener(FakeResponse(
            '{"expires": "1970-01-01T00:00:00.000Z"}',
            "https://npmplus-one.internal.example.net:81/api/tokens",
        ))
        self.assertEqual(
            client.request("/api/tokens", method="POST", payload={}),
            {"expires": "1970-01-01T00:00:00.000Z"},
        )

    def test_an_empty_reply_still_returns_none(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81")
        client.opener = RespondingOpener(FakeResponse(
            "", "https://npmplus-one.internal.example.net:81/api/nginx/proxy-hosts/7"
        ))
        self.assertIsNone(client.request("/api/nginx/proxy-hosts/7", method="DELETE"))

    def test_a_json_error_reply_keeps_the_plain_message(self):
        client = JsonClient("https://npmplus-one.internal.example.net:81")
        client.opener = failing_opener(self, HTTPError(
            "https://npmplus-one.internal.example.net:81/api/tokens",
            400,
            "error",
            {"content-type": "application/json; charset=utf-8"},
            BytesIO(b'{"error":{"code":400,"message":"Invalid email or password"}}'),
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/tokens", method="POST", payload={"identity": "a", "secret": "b"})
        message = str(caught.exception)
        self.assertIn('failed (400): {"error":{"code":400,"message":"Invalid email or password"}}', message)
        self.assertNotIn("web page rather than the API", message)

    def test_an_html_error_reply_names_the_port_too(self):
        client = JsonClient("https://npmplus-one.internal.example.net")
        client.opener = failing_opener(self, HTTPError(
            "https://npmplus-one.internal.example.net/api/tokens",
            405,
            "error",
            {"content-type": "text/html"},
            BytesIO(b"<html><head><title>405 Not Allowed</title></head></html>"),
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/tokens", method="POST", payload={})
        message = str(caught.exception)
        self.assertIn("failed (405): <html>", message)
        self.assertIn("web page rather than the API", message)
        self.assertIn("port 81", message)


    def test_a_login_answered_with_a_308_names_the_url_it_points_at(self):
        # Plain http on the admin port answers GETs but sends a POST round to
        # https with a 308, which urllib will not replay at all.
        client = JsonClient("http://npmplus-one.internal.example.net:81")
        client.opener = failing_opener(self, HTTPError(
            "http://npmplus-one.internal.example.net:81/api/tokens",
            308,
            "Permanent Redirect",
            {
                "content-type": "text/html",
                "location": "https://npmplus-one.internal.example.net:81/api/tokens",
            },
            BytesIO(b"<html><head><title>308 Permanent Redirect</title></head></html>"),
        ))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/tokens", method="POST", payload={"identity": "a", "secret": "b"})
        message = str(caught.exception)
        self.assertIn("failed (308): <html>", message)
        self.assertIn("redirect (308) to https://npmplus-one.internal.example.net:81/api/tokens", message)
        self.assertIn("never resent across a 307/308", message)
        # The port is right in this case; the scheme is what is wrong.
        self.assertNotIn("check the URL's port", message)


class LoginFailureTests(unittest.TestCase):
    def test_a_bearer_token_is_sent_with_subsequent_requests(self):
        client = NpmPlusClient("lab_npmplus", "https://npmplus-one.internal.example.net:81", "me@example.com", "secret")
        opener = SequencedOpener([
            FakeResponse(
                '{"token":"test-bearer-token"}',
                "https://npmplus-one.internal.example.net:81/api/tokens",
            ),
            FakeResponse(
                "[]",
                "https://npmplus-one.internal.example.net:81/api/nginx/proxy-hosts?expand=certificate",
            ),
        ])
        client.http.opener = opener

        self.assertEqual(client.proxy_hosts(), [])

        self.assertEqual(
            opener.requests[1].get_header("Authorization"),
            "Bearer test-bearer-token",
        )

    def test_a_rejected_login_points_at_the_email_identity(self):
        client = NpmPlusClient("lab_npmplus", "https://npmplus-one.internal.example.net:81", "user@example.org", "secret")
        client.http.opener = failing_opener(self, HTTPError(
            "https://npmplus-one.internal.example.net:81/api/tokens",
            400,
            "error",
            {"content-type": "application/json; charset=utf-8"},
            BytesIO(b'{"error":{"code":400,"message":"Invalid email or password"}}'),
        ))
        with self.assertRaises(ApiError) as caught:
            client.proxy_hosts()
        message = str(caught.exception)
        self.assertIn("Invalid email or password", message)
        self.assertIn("email address", message)
        self.assertIn("lab_npmplus", message)
        self.assertEqual(caught.exception.status, 400)

    def test_a_later_failure_is_not_blamed_on_the_identity(self):
        client = NpmPlusClient("lab_npmplus", "https://npmplus-one.internal.example.net:81", "me@example.com", "secret")
        client._authenticated = True
        client.http.opener = failing_opener(self, HTTPError(
            "https://npmplus-one.internal.example.net:81/api/nginx/proxy-hosts",
            500,
            "error",
            {"content-type": "application/json"},
            BytesIO(b'{"error":{"message":"boom"}}'),
        ))
        with self.assertRaises(ApiError) as caught:
            client.proxy_hosts()
        self.assertNotIn("email address", str(caught.exception))
        self.assertEqual(caught.exception.status, 500)

    def test_a_connection_failure_has_no_status(self):
        client = JsonClient("https://internal.example.net:81")
        client.opener = BrokenOpener(URLError(ConnectionRefusedError(10061, "connection refused")))
        with self.assertRaises(ApiError) as caught:
            client.request("/api/nginx/proxy-hosts")
        self.assertIsNone(caught.exception.status)


class PslRefreshTests(unittest.TestCase):
    @staticmethod
    def trpc_response(payload, url):
        return FakeResponse(json.dumps({"result": {"data": {"json": payload}}}), url)

    def test_refresh_reads_each_record_config_and_redetects_each_record_ip(self):
        initial = [
            {
                "id": 1, "server_id": 4, "container_id": "101", "status": "success",
                "execution_mode": "ssh", "is_vm": False,
                "lxc_config": {"hostname": "photos", "net_vlan": 1},
            },
            {
                "id": 2, "server_id": 4, "container_id": "101", "status": "success",
                "execution_mode": "ssh", "is_vm": False,
                "lxc_config": {"hostname": "photos", "net_vlan": 1},
            },
            {
                "id": 3, "server_id": 4, "container_id": "102", "status": "success",
                "is_vm": True, "lxc_config": {},
            },
            {
                "id": 99, "server_id": 4, "container_id": "103", "status": "failed",
                "execution_mode": "ssh", "is_vm": False,
                "lxc_config": {"hostname": "photos", "net_vlan": 1},
            },
        ]
        fresh = {"hostname": "photos", "net_vlan": 20, "net_ip": "192.0.2.8/24"}
        fresh_scripts = [dict(script) for script in initial]
        for script in fresh_scripts:
            script["web_ui_ip"] = "192.0.2.5"
            script["web_ui_port"] = 80
        base = "https://psl.example.net"
        responses = [
            self.trpc_response({"success": True, "scripts": initial}, f"{base}/api/trpc/installedScripts.getAllInstalledScripts"),
            self.trpc_response(
                {"success": True, "statusMap": {"4:101": "running", "4:103": "stopped"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
        ]
        for script_id, detected_ip in ((1, "192.0.2.1"), (2, "192.0.2.8")):
            responses.extend([
                self.trpc_response(
                    {"success": True, "config": {**fresh, "record": script_id}},
                    f"{base}/api/trpc/installedScripts.getLXCConfig",
                ),
                self.trpc_response(
                    {"success": True, "detectedIp": detected_ip, "detectedPort": 8090},
                    f"{base}/api/trpc/installedScripts.autoDetectWebUI",
                ),
            ])
            next(script for script in fresh_scripts if script["id"] == script_id)["web_ui_ip"] = detected_ip
        responses.append(self.trpc_response(
            {"success": True, "scripts": fresh_scripts},
            f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
        ))
        opener = SequencedOpener(responses)
        client = PveScriptsLocalClient(base)
        client.http.opener = opener
        progress = []

        scripts = client.installed_scripts(
            refresh_lxc_config=True,
            progress_callback=lambda current, total, script_id, container_id: progress.append(
                (current, total, script_id, container_id)
            ),
        )

        self.assertEqual(scripts[0]["lxc_config"], {**fresh, "record": 1})
        self.assertEqual(scripts[1]["web_ui_ip"], "192.0.2.8")
        self.assertEqual(scripts[1]["web_ui_port"], 8090)
        self.assertEqual(scripts[3]["web_ui_ip"], "192.0.2.5")
        self.assertEqual(len(opener.requests), 7)
        self.assertEqual(json.loads(opener.requests[1].data), {"json": {"serverIds": [4]}})
        query = parse_qs(urlparse(opener.requests[2].full_url).query)
        payload = json.loads(query["input"][0])["json"]
        self.assertEqual(payload, {"scriptId": 1, "forceSync": True})
        self.assertEqual(opener.requests[3].get_method(), "POST")
        self.assertEqual(json.loads(opener.requests[3].data), {"json": {"id": 1}})
        self.assertEqual(json.loads(opener.requests[5].data), {"json": {"id": 2}})
        self.assertEqual(progress, [(1, 2, 1, "101"), (2, 2, 2, "101")])

    def test_refresh_detects_ip_and_uses_detected_port_over_manual_port(self):
        initial = [{
            "id": 1, "server_id": 4, "container_id": "101", "status": "success",
            "execution_mode": "ssh", "is_vm": False,
            "lxc_config": {"hostname": "photos", "net_vlan": 10},
            "web_ui_ip": "192.0.2.5",
            "web_ui_port": 32401,
        }]
        refreshed = [dict(initial[0], lxc_config={"hostname": "photos", "net_vlan": 20})]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response(
                {"success": True, "scripts": initial},
                f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
            ),
            self.trpc_response(
                {"success": True, "statusMap": {"4:101": "running"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
            self.trpc_response(
                {"success": True, "config": {"hostname": "photos", "net_vlan": 20}},
                f"{base}/api/trpc/installedScripts.getLXCConfig",
            ),
            self.trpc_response(
                {"success": True, "detectedIp": "192.0.2.8", "detectedPort": 80},
                f"{base}/api/trpc/installedScripts.autoDetectWebUI",
            ),
            self.trpc_response(
                {"success": True, "message": "Installed script updated successfully"},
                f"{base}/api/trpc/installedScripts.updateInstalledScript",
            ),
            self.trpc_response(
                {"success": True, "scripts": [dict(
                    refreshed[0],
                    web_ui_ip="192.0.2.8",
                    web_ui_port=80,
                )]},
                f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
            ),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        scripts = client.installed_scripts(refresh_lxc_config=True)

        self.assertEqual(scripts[0]["lxc_config"]["net_vlan"], 20)
        self.assertEqual(scripts[0]["web_ui_ip"], "192.0.2.8")
        self.assertEqual(scripts[0]["web_ui_port"], 80)
        self.assertEqual(len(opener.requests), 6)
        self.assertTrue(urlparse(opener.requests[3].full_url).path.endswith("autoDetectWebUI"))
        self.assertTrue(urlparse(opener.requests[4].full_url).path.endswith("updateInstalledScript"))
        self.assertEqual(
            json.loads(opener.requests[4].data),
            {"json": {"id": 1, "web_ui_port": 80}},
        )

    def test_refresh_uses_each_records_detected_port_not_a_shared_saved_port(self):
        initial = [
            {
                "id": 1, "server_id": 4, "container_id": "101", "status": "success",
                "execution_mode": "ssh", "is_vm": False, "web_ui_port": 8080,
            },
            {
                "id": 2, "server_id": 4, "container_id": "102", "status": "success",
                "execution_mode": "ssh", "is_vm": False, "web_ui_port": 8080,
            },
        ]
        detected_ports = {1: 8090, 2: 8081}
        base = "https://psl.example.net"
        responses = [
            self.trpc_response(
                {"success": True, "scripts": initial},
                f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
            ),
            self.trpc_response(
                {"success": True, "statusMap": {"4:101": "running", "4:102": "running"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
        ]
        for script_id, detected_port in detected_ports.items():
            responses.extend([
                self.trpc_response(
                    {"success": True, "config": {"hostname": f"app-{script_id}"}},
                    f"{base}/api/trpc/installedScripts.getLXCConfig",
                ),
                self.trpc_response(
                    {
                        "success": True,
                        "detectedIp": f"192.0.2.{script_id}",
                        "detectedPort": detected_port,
                    },
                    f"{base}/api/trpc/installedScripts.autoDetectWebUI",
                ),
                self.trpc_response(
                    {"success": True},
                    f"{base}/api/trpc/installedScripts.updateInstalledScript",
                ),
            ])
        refreshed = [
            dict(script, web_ui_port=detected_ports[script["id"]])
            for script in initial
        ]
        responses.append(self.trpc_response(
            {"success": True, "scripts": refreshed},
            f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
        ))
        opener = SequencedOpener(responses)
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        scripts = client.installed_scripts(refresh_lxc_config=True)

        self.assertEqual([script["web_ui_port"] for script in scripts], [8090, 8081])
        update_payloads = [
            json.loads(request.data)
            for request in opener.requests
            if urlparse(request.full_url).path.endswith("updateInstalledScript")
        ]
        self.assertEqual(update_payloads, [
            {"json": {"id": 1, "web_ui_port": 8090}},
            {"json": {"id": 2, "web_ui_port": 8081}},
        ])

    def test_refresh_failure_is_attached_to_record_instead_of_using_stale_vlan(self):
        initial = [{
            "id": 1, "server_id": 4, "container_id": "101", "status": "success",
            "execution_mode": "ssh", "is_vm": False,
            "lxc_config": {"hostname": "photos", "net_vlan": 1},
        }]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response({"success": True, "scripts": initial}, f"{base}/api/trpc/installedScripts.getAllInstalledScripts"),
            self.trpc_response(
                {"success": True, "statusMap": {"4:101": "running"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
            self.trpc_response({"success": False, "error": "SSH connection failed"}, f"{base}/api/trpc/installedScripts.getLXCConfig"),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        scripts = client.installed_scripts(refresh_lxc_config=True)
        self.assertIn("container 101", scripts[0]["_refresh_error"])
        self.assertIn("SSH connection failed", scripts[0]["_refresh_error"])
        self.assertEqual(scripts[0]["lxc_config"]["net_vlan"], 1)
        self.assertEqual(len(opener.requests), 3)

    def test_ip_detection_failure_is_attached_to_record_instead_of_using_stale_address(self):
        initial = [{
            "id": 1, "server_id": 4, "container_id": "101", "status": "success",
            "execution_mode": "ssh", "is_vm": False,
            "lxc_config": {"hostname": "photos", "net_vlan": 1},
            "web_ui_ip": "192.0.2.5",
        }]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response({"success": True, "scripts": initial}, f"{base}/api/trpc/installedScripts.getAllInstalledScripts"),
            self.trpc_response(
                {"success": True, "statusMap": {"4:101": "running"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
            self.trpc_response(
                {"success": True, "config": {"hostname": "photos", "net_vlan": 1}},
                f"{base}/api/trpc/installedScripts.getLXCConfig",
            ),
            self.trpc_response(
                {"success": False, "error": "Auto-detect only works for SSH mode scripts"},
                f"{base}/api/trpc/installedScripts.autoDetectWebUI",
            ),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        scripts = client.installed_scripts(refresh_lxc_config=True)
        self.assertIn("Could not refresh Web UI IP for PSL record 1 (container 101)", scripts[0]["_refresh_error"])
        self.assertIn("Auto-detect only works for SSH mode scripts", scripts[0]["_refresh_error"])
        self.assertEqual(scripts[0]["web_ui_ip"], "192.0.2.5")
        self.assertEqual(len(opener.requests), 4)

    def test_one_record_refresh_failure_does_not_stop_other_records(self):
        initial = [
            {
                "id": 1, "server_id": 4, "container_id": "101", "status": "success",
                "execution_mode": "ssh", "is_vm": False,
                "lxc_config": {"hostname": "photos", "net_vlan": 1},
            },
            {
                "id": 2, "server_id": 4, "container_id": "102", "status": "success",
                "execution_mode": "ssh", "is_vm": False,
                "lxc_config": {"hostname": "wiki", "net_vlan": 1},
            },
        ]
        fresh = [dict(script) for script in initial]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response({"success": True, "scripts": initial}, f"{base}/api/trpc/installedScripts.getAllInstalledScripts"),
            self.trpc_response(
                {"success": True, "statusMap": {"4:101": "running", "4:102": "running"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
            self.trpc_response({"success": False, "error": "SSH connection failed"}, f"{base}/api/trpc/installedScripts.getLXCConfig"),
            self.trpc_response(
                {"success": True, "config": {"hostname": "wiki", "net_vlan": 20}},
                f"{base}/api/trpc/installedScripts.getLXCConfig",
            ),
            self.trpc_response(
                {"success": True, "detectedIp": "192.0.2.8", "detectedPort": 8080},
                f"{base}/api/trpc/installedScripts.autoDetectWebUI",
            ),
            self.trpc_response({"success": True, "scripts": fresh}, f"{base}/api/trpc/installedScripts.getAllInstalledScripts"),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        scripts = client.installed_scripts(refresh_lxc_config=True)

        self.assertIn("SSH connection failed", scripts[0]["_refresh_error"])
        self.assertNotIn("_refresh_error", scripts[1])
        self.assertEqual(scripts[1]["lxc_config"]["net_vlan"], 20)
        self.assertEqual(scripts[1]["web_ui_ip"], "192.0.2.8")

    def test_stopped_containers_are_skipped_without_attempting_refresh(self):
        initial = [{
            "id": 7, "server_id": 4, "container_id": "222", "status": "failed",
            "execution_mode": "ssh", "is_vm": False,
            "web_ui_ip": "192.0.2.20",
        }]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response(
                {"success": True, "scripts": initial},
                f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
            ),
            self.trpc_response(
                {"success": True, "statusMap": {"4:222": "stopped"}},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener
        progress = []

        scripts = client.installed_scripts(
            refresh_lxc_config=True,
            progress_callback=lambda *args: progress.append(args),
        )

        self.assertEqual(scripts, initial)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(progress, [])

    def test_status_lookup_failure_does_not_try_refreshing_stale_records(self):
        initial = [{
            "id": 7, "server_id": 4, "container_id": "222", "status": "failed",
            "execution_mode": "ssh", "is_vm": False,
        }]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response(
                {"success": True, "scripts": initial},
                f"{base}/api/trpc/installedScripts.getAllInstalledScripts",
            ),
            self.trpc_response(
                {"success": False, "error": "SSH status check failed"},
                f"{base}/api/trpc/installedScripts.getContainerStatuses",
            ),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        with self.assertRaises(ApiError) as caught:
            client.installed_scripts(refresh_lxc_config=True)
        self.assertIn("Could not read Proxmox container statuses", str(caught.exception))
        self.assertIn("SSH status check failed", str(caught.exception))
        self.assertEqual(len(opener.requests), 2)

    def test_refresh_reports_successful_lxc_records_without_a_server_id(self):
        initial = [{
            "id": 1, "container_id": "101", "status": "success",
            "execution_mode": "ssh", "is_vm": False,
            "web_ui_ip": "192.0.2.5",
        }]
        base = "https://psl.example.net"
        opener = SequencedOpener([
            self.trpc_response({"success": True, "scripts": initial}, f"{base}/api/trpc/installedScripts.getAllInstalledScripts"),
        ])
        client = PveScriptsLocalClient(base)
        client.http.opener = opener

        scripts = client.installed_scripts(refresh_lxc_config=True)
        self.assertIn("no Proxmox server ID", scripts[0]["_refresh_error"])
        self.assertEqual(len(opener.requests), 1)


class ProxyHostListTests(unittest.TestCase):
    def test_proxy_host_list_can_skip_certificate_expansion(self):
        client = NpmPlusClient("lab", "https://internal.example.net:81", "user", "secret")
        client._authenticated = True
        client.http.opener = RespondingOpener(FakeResponse(
            "[]", "https://internal.example.net:81/api/nginx/proxy-hosts"
        ))
        requested = []
        original_request = client.http.request

        def capture_request(path, method="GET", payload=None):
            requested.append(path)
            return original_request(path, method, payload)

        client.http.request = capture_request
        self.assertEqual(client.proxy_hosts(expand_certificate=False), [])
        self.assertEqual(requested, ["/api/nginx/proxy-hosts"])

    def test_proxy_host_list_keeps_certificate_expansion_by_default(self):
        client = NpmPlusClient("lab", "https://internal.example.net:81", "user", "secret")
        client._authenticated = True
        client.http.opener = RespondingOpener(FakeResponse(
            "[]", "https://internal.example.net:81/api/nginx/proxy-hosts?expand=certificate"
        ))
        requested = []
        original_request = client.http.request

        def capture_request(path, method="GET", payload=None):
            requested.append(path)
            return original_request(path, method, payload)

        client.http.request = capture_request
        self.assertEqual(client.proxy_hosts(), [])
        self.assertEqual(requested, ["/api/nginx/proxy-hosts?expand=certificate"])


if __name__ == "__main__":
    unittest.main()

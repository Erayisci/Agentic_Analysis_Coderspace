"""Opt-in real SDK/browser regression fixtures; no external websites are used.

Set WEB_TOOLS_TEST_BROWSER=1 and use the isolated crawler Python environment.
Normal stdlib-only test runs skip these cases without importing optional packages.
"""

import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from backend.extensions.web_tools.egress import BoundedHTTPServer, EgressHandler
from backend.extensions.web_tools.worker import normalize_request, preflight, run_isolated


class FixtureProxy(EgressHandler):
    """Serve a synthetic public origin at a test proxy; never open an upstream."""

    def do_CONNECT(self):
        context = getattr(self.server, "tls_context", None)
        if self.path == "fixture.example.org:443" and context is not None:
            self.server.tls_attempts += 1
            self.send_response(200, "Connection Established")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            try:
                with context.wrap_socket(self.connection, server_side=True) as connection:
                    if connection.recv(8192):
                        self.server.tls_http_requests += 1
                        data = (b"<html><head><title>Untrusted fixture</title></head><body>"
                                + b"This content must never be read from an untrusted TLS server. " * 8
                                + b"</body></html>")
                        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                                           b"Connection: close\r\n"
                                           + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            except OSError:
                # Both preflight and Chromium must reject the certificate.
                pass
            return
        self._json(403, {"error": "External connections are disabled in this fixture."})

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.hostname != "fixture.example.org":
            self.server.unexpected.append(self.path)
            self._json(403, {"error": "Unexpected destination."})
            return
        if parts.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/private")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        body = '<html><head><title>Browser fixture</title></head><body><p id="value">Initial content.</p>'
        body += ("<p>This is a deterministic article used to verify webpage reading. "
                 "It contains enough ordinary text to avoid the upstream detector treating "
                 "a nearly empty document as an access challenge. No external resources are needed.</p>")
        if parts.path == "/js":
            body += '<script>document.getElementById("value").textContent="JavaScript rendered successfully.";</script>'
        elif parts.path == "/private":
            body += '<script>fetch("http://127.0.0.1/private").catch(()=>{});</script>'
        elif parts.path == "/navigation":
            body += '<script>location.href="http://169.254.169.254/latest/meta-data/";</script>'
        data = (body + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf" if parts.path == "/pdf" else "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True


@unittest.skipUnless(os.environ.get("WEB_TOOLS_TEST_BROWSER") == "1", "opt-in: requires isolated Crawl4AI and Chromium")
class BrowserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.server = BoundedHTTPServer(("127.0.0.1", 0), FixtureProxy)
        self.server.unexpected = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def read(self, path, render_js=True):
        request = normalize_request({"url": "http://fixture.example.org" + path, "render_js": render_js})
        return run_isolated(request, f"http://127.0.0.1:{self.server.server_port}", 30)

    def test_real_markdown_and_javascript_rendering(self):
        result = self.read("/js")
        self.assertEqual(result["status"], "ok", result)
        self.assertIn("JavaScript rendered successfully.", result["content"])
        self.assertEqual(result["title"], "Browser fixture")
        self.assertEqual(result["content_type"], "text/html")
        static = self.read("/js", render_js=False)
        self.assertIn("Initial content.", static["content"])
        self.assertNotIn("JavaScript rendered successfully.", static["content"])

    def test_browser_blocks_private_subrequests_and_navigation(self):
        for path in ("/private", "/navigation"):
            with self.subTest(path=path):
                result = self.read(path)
                self.assertEqual(result["error"]["code"], "invalid_url", result)
                self.assertEqual(self.server.unexpected, [])

    def test_private_http_redirect_is_rejected_before_browser_navigation(self):
        result = self.read("/redirect")
        self.assertEqual(result["error"]["code"], "invalid_url", result)
        self.assertEqual(self.server.unexpected, [])

    def test_pdf_is_not_reported_as_readable_html(self):
        result = self.read("/pdf")
        self.assertEqual(result["error"]["code"], "unsupported_content_type", result)
        self.assertEqual(result["content"], "")

    def test_missing_issuer_fallback_still_rejects_an_untrusted_browser_certificate(self):
        # An unknown issuer omitted from the chain produces the same OpenSSL
        # missing-issuer error as BDDK. Chromium must still reject this fixture.
        with tempfile.TemporaryDirectory(prefix="web-tools-tls-fixture-") as directory:
            path = Path(directory)
            commands = [
                ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                 "-keyout", "ca.key", "-out", "ca.pem", "-subj", "/CN=Untrusted test CA",
                 "-addext", "basicConstraints=critical,CA:TRUE"],
                ["req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
                 "-out", "leaf.csr", "-subj", "/CN=fixture.example.org"],
                ["x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-out", "leaf.pem", "-days", "2", "-sha256",
                 "-extfile", "leaf.ext"],
            ]
            (path / "leaf.ext").write_text(
                "subjectAltName=DNS:fixture.example.org\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
            )
            for command in commands:
                subprocess.run(["openssl", *command], cwd=path, capture_output=True,
                               check=True, timeout=10)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(path / "leaf.pem", path / "leaf.key")
            self.server.tls_context = context
            self.server.tls_attempts = self.server.tls_http_requests = 0
            url = "https://fixture.example.org/"
            proxy = f"http://127.0.0.1:{self.server.server_port}"
            with self.assertRaises(ssl.SSLCertVerificationError) as failure:
                preflight(url, proxy)
            self.assertEqual(failure.exception.verify_code, 20)
            result = run_isolated(normalize_request({"url": url}), proxy, 30)
            self.assertEqual(result["error"]["code"], "certificate_error", result)
            self.assertFalse(result["error"]["retryable"])
            self.assertEqual(result["content"], "")
            self.assertGreaterEqual(self.server.tls_attempts, 3)
            self.assertEqual(self.server.tls_http_requests, 0)
            # Binary downloads use the same normally verified browser only to
            # recover intermediate CAs. It must not accept this unknown issuer.
            from backend.extensions.web_tools.asset_download import download
            from backend.extensions.web_tools.asset_common import AssetFailure
            with self.assertRaises(AssetFailure) as asset_failure:
                download(url, proxy, path / "rejected.pdf", 1048576, 30)
            self.assertEqual(asset_failure.exception.code, "certificate_error")
            self.assertEqual(self.server.tls_http_requests, 0)


if __name__ == "__main__":
    unittest.main()

"""Worker input, redirect, format and hard-deadline behavior using fixtures."""

import subprocess
import ssl
import unittest
from unittest.mock import Mock, patch

from backend.extensions.web_tools import worker
from backend.extensions.web_tools.security import UnsafeURL


def response(status=200, **headers):
    result = Mock(status=status)
    result.getheader.side_effect = lambda name: headers.get(name)
    return result


class WorkerTests(unittest.TestCase):
    def test_request_limits_and_no_arbitrary_browser_configuration(self):
        valid = worker.normalize_request({"url": "https://example.com", "max_chars": 30000})
        self.assertEqual(valid, {"url": "https://example.com/", "max_chars": 20000, "render_js": True})
        for values in [{"max_chars": 0}, {"max_chars": True}, {"max_chars": 100001},
                       {"render_js": "true"}, {"js_code": "fetch('private')"},
                       {"proxy": "http://private"}, {"cookies": []}]:
            with self.subTest(values=values), self.assertRaises(worker.ReadFailure):
                worker.normalize_request({"url": "https://example.com", **values})

    def test_formats_and_sizes_are_explicit(self):
        self.assertEqual(worker.check_response(200, "text/html; charset=utf-8", "100"), "text/html")
        self.assertEqual(worker.check_response(200, "application/xhtml+xml"), "application/xhtml+xml")
        for status, media_type, size, code in [
            (403, "text/html", None, "upstream_error"),
            (200, "application/pdf", None, "unsupported_content_type"),
            (200, "image/png", None, "unsupported_content_type"),
            (200, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", None, "unsupported_content_type"),
            (200, "text/plain", None, "unsupported_content_type"),
            (200, "text/html", str(worker.MAX_HTML_BYTES + 1), "content_too_large"),
            (200, "text/html", "bad", "upstream_error"),
        ]:
            with self.subTest(media_type=media_type, status=status), self.assertRaises(worker.ReadFailure) as exc:
                worker.check_response(status, media_type, size)
            self.assertEqual(exc.exception.code, code)

    def test_preflight_follows_public_redirects_through_proxy(self):
        first, second = Mock(), Mock()
        first.getresponse.return_value = response(302, Location="https://example.org/article")
        second.getresponse.return_value = response(**{"Content-Type": "text/html"})
        factory = Mock(side_effect=[first, second])
        result = worker.preflight("https://example.com/", "http://egress:3128", connection_factory=factory)
        self.assertEqual(result, ("https://example.org/article", "text/html"))
        self.assertEqual(factory.call_count, 2)
        first.set_tunnel.assert_called_once_with("example.com", 443)
        second.set_tunnel.assert_called_once_with("example.org", 443)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_preflight_never_connects_to_a_private_redirect(self):
        for location in ["http://127.0.0.1/secret", "http://169.254.169.254/", "file:///etc/passwd"]:
            first = Mock()
            first.getresponse.return_value = response(302, Location=location)
            factory = Mock(return_value=first)
            with self.subTest(location=location), self.assertRaises(UnsafeURL):
                worker.preflight("http://example.com", "http://egress:3128", connection_factory=factory)
            self.assertEqual(factory.call_count, 1)
            first.close.assert_called_once()

    def test_redirects_are_bounded(self):
        connection = Mock()
        connection.getresponse.return_value = response(302, Location="/again")
        factory = Mock(return_value=connection)
        with self.assertRaises(worker.ReadFailure):
            worker.preflight("http://example.com", "http://egress:3128", connection_factory=factory)
        self.assertEqual(factory.call_count, 6)

    def test_only_missing_issuer_errors_defer_to_browser_tls_verification(self):
        for code in (20, 21, 10, 18, 19, 62, 1):
            error = ssl.SSLCertVerificationError("private diagnostic")
            error.verify_code = code
            with self.subTest(code=code), patch.object(worker, "preflight", side_effect=error):
                if code in {20, 21}:
                    worker.preflight_for_browser("https://example.com/", "http://egress:3128", 1)
                else:
                    with self.assertRaises(worker.ReadFailure) as failure:
                        worker.preflight_for_browser("https://example.com/", "http://egress:3128", 1)
                    self.assertEqual(failure.exception.code, "certificate_error")
                    self.assertNotIn("private diagnostic", str(failure.exception))

    def test_browser_preflight_preserves_non_tls_rejections(self):
        for error in (UnsafeURL("private redirect"), worker.ReadFailure("unsupported_content_type"),
                      worker.ReadFailure("content_too_large"), OSError("unreachable")):
            with self.subTest(error=error), patch.object(worker, "preflight", side_effect=error):
                with self.assertRaises(type(error)):
                    worker.preflight_for_browser("https://example.com/", "http://egress:3128", 1)

    def test_truncation_is_in_characters_and_never_fabricates_content(self):
        request = worker.normalize_request({"url": "https://example.com", "max_chars": 100})
        metadata = {"url": "https://example.com/report", "title": "Rapor", "content_type": "text/html"}
        result = worker.format_result(request, metadata, "ş" * 130, partial=True)
        self.assertEqual(result["content"], "ş" * 100)
        self.assertEqual((result["original_chars"], result["returned_chars"]), (130, 100))
        self.assertTrue(result["truncated"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["final_url"], metadata["url"])
        self.assertEqual(worker.format_result(request, metadata, " \n")["status"], "empty")

    def test_timeout_kills_the_browser_process_group(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = [subprocess.TimeoutExpired("child", 0.1), (b"", b"")]
        with patch.object(worker.os, "killpg") as kill:
            result = worker.run_isolated({"url": "https://example.com/"}, "http://egress:3128", 0.1,
                                         popen=Mock(return_value=process))
        self.assertEqual(result["error"]["code"], "timeout")
        kill.assert_called_once_with(12345, worker.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_invalid_child_protocol_does_not_escape_as_an_exception(self):
        process = Mock(pid=12345, returncode=0)
        process.communicate.return_value = (b"not JSON secret-value", None)
        with patch.object(worker.os, "killpg"):
            result = worker.run_isolated({"url": "https://example.com/"}, "http://egress:3128", 2,
                                         popen=Mock(return_value=process))
        self.assertEqual(result["error"]["code"], "crawl_failed")
        self.assertNotIn("secret-value", str(result))

    def test_browser_cannot_disable_tls_verification(self):
        result = worker.secure_browser_args({"headless": True, "args": [
            "--ignore-certificate-errors", "--ignore-certificate-errors-spki-list=abc",
            "--allow-insecure-localhost", "--proxy-server=http://egress:3128", "--disable-quic"]})
        self.assertEqual(result["args"], ["--proxy-server=http://egress:3128", "--disable-quic"])
        self.assertTrue(result["headless"])


if __name__ == "__main__":
    unittest.main()

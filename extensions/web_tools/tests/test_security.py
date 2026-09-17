"""Network boundaries tested without public DNS, browser packages, or websites."""

import http.client
import socket
import threading
import unittest
from unittest.mock import Mock, patch

from backend.extensions.web_tools.egress import BoundedHTTPServer, EgressHandler, connect_public
from backend.extensions.web_tools.security import UnsafeURL, resolve_public_url, validate_url_syntax


def record(address, port=443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    destination = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", destination


class URLSecurityTests(unittest.TestCase):
    def test_forbidden_schemes_credentials_ports_and_local_names(self):
        urls = ["file:///etc/passwd", "ftp://example.com", "javascript:alert(1)",
                "data:text/html,hello", "http://localhost", "http://x.localhost",
                "http://service.internal", "http://router.lan", "http://printer.local",
                "http://user:secret@example.com", "http://@example.com", "http://example.com:22",
                "http://example.com\\@localhost", "http://example.com\r\nX-Test:evil",
                "http://%31%32%37.0.0.1", "http://[fe80::1%25eth0]", "https://", "https:///host",
                "https://example.com/\ud800"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                validate_url_syntax(url)

    def test_private_special_and_transition_addresses_are_blocked(self):
        addresses = ["0.0.0.0", "10.0.0.1", "127.0.0.1", "172.16.0.1", "192.168.1.1",
                     "169.254.169.254", "100.100.100.200", "100.64.0.1", "192.0.0.9",
                     "192.0.2.1", "198.18.0.1", "224.0.0.1", "240.0.0.1",
                     "::", "::1", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
                     "::ffff:127.0.0.1", "64:ff9b::a00:1", "2002:7f00:1::1"]
        for address in addresses:
            authority = f"[{address}]" if ":" in address else address
            with self.subTest(address=address), self.assertRaises(UnsafeURL):
                validate_url_syntax("https://" + authority)

    def test_public_url_normalization(self):
        self.assertEqual(validate_url_syntax("HTTPS://Example.COM:443/rapor?yıl=2026#part"),
                         "https://example.com:443/rapor?y%C4%B1l=2026")
        self.assertEqual(validate_url_syntax("https://8.8.8.8"), "https://8.8.8.8/")
        self.assertEqual(validate_url_syntax("https://[2606:4700:4700::1111]"),
                         "https://[2606:4700:4700::1111]/")

    def test_all_dns_answers_must_be_public(self):
        for records in [[], [record("127.0.0.1")], [record("8.8.8.8"), record("10.0.0.1")],
                        [record("8.8.8.8"), record("::1")]]:
            with self.subTest(records=records), self.assertRaises(UnsafeURL):
                resolve_public_url("https://example.com", resolver=Mock(return_value=records))
        with self.assertRaises(UnsafeURL):
            resolve_public_url("https://example.com", resolver=Mock(side_effect=socket.gaierror()))

    def test_numeric_host_aliases_cannot_bypass_dns_validation(self):
        for url in ["http://127.1", "http://0x7f.0.0.1", "http://0177.0.0.1"]:
            with self.subTest(url=url), self.assertRaises(UnsafeURL):
                resolve_public_url(url, resolver=Mock(return_value=[record("127.0.0.1", 80)]))

    def test_connection_pins_the_validated_ip_without_second_resolution(self):
        resolver = Mock(side_effect=[[record("8.8.8.8")], [record("127.0.0.1")]])
        connection = Mock()
        factory = Mock(return_value=connection)
        normalized, result = connect_public("https://example.com", resolver=resolver, socket_factory=factory)
        self.assertEqual(normalized, "https://example.com/")
        self.assertIs(result, connection)
        resolver.assert_called_once()
        connection.connect.assert_called_once_with(("8.8.8.8", 443))
        # A later connection sees and rejects a rebound answer before opening a socket.
        with self.assertRaises(UnsafeURL):
            connect_public("https://example.com", resolver=resolver, socket_factory=factory)
        self.assertEqual(factory.call_count, 1)

    def test_proxy_blocks_http_and_https_local_targets_on_the_wire(self):
        with BoundedHTTPServer(("127.0.0.1", 0), EgressHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("backend.extensions.web_tools.security.socket.getaddrinfo") as resolver:
                    for method, target in [("GET", "http://127.0.0.1/"),
                                           ("GET", "http://169.254.169.254/latest/meta-data/"),
                                           ("CONNECT", "127.0.0.1:443"),
                                           ("CONNECT", "example.com:22")]:
                        # Connect via a pre-bound socket to keep the DNS mock limited
                        # to the attempted outbound connection in the proxy thread.
                        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                        connection.sock = socket.socket()
                        connection.sock.connect(("127.0.0.1", server.server_port))
                        try:
                            connection.request(method, target)
                            response = connection.getresponse()
                            self.assertEqual(response.status, 403, target)
                            response.read()
                        finally:
                            connection.close()
                    resolver.assert_not_called()
            finally:
                server.shutdown()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()

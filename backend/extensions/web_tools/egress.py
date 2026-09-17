"""Public-web proxy and fixed inbound gateway for the isolated crawler.

The proxy port is private to Compose. Only the gateway is published on host
localhost. HTTPS remains encrypted: the proxy enforces public destination IPs,
ports, byte and lifetime limits, not page MIME.
"""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseRequestHandler
from urllib.parse import urlsplit

from .security import UnsafeURL, resolve_public_url


MAX_TUNNEL_BYTES = 32 * 1024 * 1024
CONNECTION_TIMEOUT = 60


def connect_public(url: str, timeout: float = 10, resolver=None, socket_factory=socket.socket):
    """Connect to a numeric address from the validated DNS snapshot."""
    normalized, addresses = resolve_public_url(url, resolver=resolver)
    deadline = time.monotonic() + timeout
    for family, socktype, protocol, sockaddr in addresses:
        connection = socket_factory(family, socktype, protocol)
        try:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            connection.connect(sockaddr)
            return normalized, connection
        except OSError:
            connection.close()
            if time.monotonic() >= deadline:
                break
    raise OSError("Public destination is unavailable.")


def relay(client, upstream, *, timeout=CONNECTION_TIMEOUT, max_bytes=MAX_TUNNEL_BYTES):
    """Relay a single bounded tunnel without recording content or URLs."""
    deadline = time.monotonic() + timeout
    remaining = max_bytes
    while remaining > 0:
        wait = deadline - time.monotonic()
        if wait <= 0:
            return
        readable, _, _ = select.select([client, upstream], [], [], min(wait, 1))
        for source in readable:
            data = source.recv(min(65536, remaining))
            if not data:
                return
            destination = upstream if source is client else client
            destination.settimeout(max(0.1, deadline - time.monotonic()))
            destination.sendall(data)
            remaining -= len(data)


class BoundedHTTPServer(ThreadingHTTPServer):
    """Reject excess sockets before creating threads, including slow clients."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler, *, max_connections=64):
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Exception tracebacks can contain full sensitive URLs.
        pass


class CrawlerGatewayHandler(BaseRequestHandler):
    """Forward only to the trusted worker, regardless of client HTTP contents."""

    def handle(self):
        self.request.settimeout(10)
        try:
            upstream = socket.create_connection(self.server.upstream_address, timeout=3)
        except OSError:
            data = b'{"error":"Crawler service is unavailable."}'
            self.request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(data)}\r\n\r\n".encode() + data
            )
            return
        with upstream:
            relay(self.request, upstream, timeout=self.server.relay_timeout,
                  max_bytes=3 * 1024 * 1024)


class CrawlerGatewayServer(BoundedHTTPServer):
    """Bounded TCP ingress, with no user-controlled destination or DNS lookup."""

    def __init__(self, address, *, upstream_address=("crawler", 8932), timeout=190):
        self.upstream_address = upstream_address
        # Allow the maximum 180-second crawl deadline plus its HTTP response.
        self.relay_timeout = timeout
        super().__init__(address, CrawlerGatewayHandler, max_connections=16)


class EgressHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PublicWebEgress"
    sys_version = ""
    rbufsize = 0  # CONNECT must not leave TLS bytes in a buffered file.

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "Proxy request rejected."})

    def do_CONNECT(self):
        try:
            if any(char in self.path for char in "/?#@"):
                raise UnsafeURL("Invalid tunnel destination.")
            parts = urlsplit("https://" + self.path)
            if parts.port != 443:
                raise UnsafeURL("HTTPS tunnels must use port 443.")
            _, upstream = connect_public("https://" + self.path)
            with upstream:
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                relay(self.connection, upstream)
        except (UnsafeURL, ValueError):
            self._json(403, {"error": "Destination is not a public web address."})
        except OSError:
            # Do not write another HTTP response into an established TLS tunnel.
            if not self.close_connection:
                self._json(502, {"error": "Public destination is unavailable."})

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "service": "egress"})
            return
        self._forward()

    def do_HEAD(self):
        self._forward()

    def _forward(self):
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                self._json(400, {"error": "Request bodies are not supported."})
                return
            normalized, upstream = connect_public(self.path)
            with upstream:
                parts = urlsplit(normalized)
                if parts.scheme != "http":
                    raise UnsafeURL("Use CONNECT for HTTPS.")
                path = parts.path + ("?" + parts.query if parts.query else "")
                blocked = {"host", "connection", "proxy-connection", "proxy-authorization",
                           "keep-alive", "te", "trailer", "transfer-encoding", "upgrade"}
                blocked.update(item.strip().lower() for item in self.headers.get("Connection", "").split(","))
                headers = [f"{self.command} {path} HTTP/1.1", f"Host: {parts.netloc}", "Connection: close"]
                for key, value in self.headers.items():
                    if key.lower() not in blocked:
                        if "\r" in value or "\n" in value:
                            raise UnsafeURL("Malformed header.")
                        headers.append(f"{key}: {value}")
                upstream.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))
                self.close_connection = True
                relay(self.connection, upstream)
        except (UnsafeURL, ValueError, UnicodeError):
            self._json(403, {"error": "Destination is not a public web address."})
        except OSError:
            if not self.close_connection:
                self._json(502, {"error": "Public destination is unavailable."})


def main():
    port = int(os.environ.get("WEB_EGRESS_PORT", "3128"))
    # Docker does not publish a port from a container attached only to an
    # internal network. Publish ingress here, keeping Chromium isolated.
    with BoundedHTTPServer(("0.0.0.0", port), EgressHandler) as server, \
            CrawlerGatewayServer(("0.0.0.0", 8932), timeout=max(190, min(310,
                int(os.environ.get("WEB_ASSET_TIMEOUT_SECONDS", "120")) + 10))) as gateway:
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        try:
            server.serve_forever()
        finally:
            gateway.shutdown()
            thread.join()


if __name__ == "__main__":
    main()

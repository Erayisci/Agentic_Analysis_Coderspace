"""Exercise localhost ingress without Docker or external network access."""

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler
import json
import socket
import threading
import unittest

from backend.extensions.web_tools.egress import BoundedHTTPServer, CrawlerGatewayServer


@contextmanager
def running(server):
    with server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class WorkerFixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/stall":
            self.server.release.wait(5)
            return
        self.reply({"status": "ok", "service": "crawler", "path": self.path})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.reply({"worker_received": json.loads(body)})

    def reply(self, result):
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GatewayTests(unittest.TestCase):
    def test_health_and_read_body_reach_the_worker(self):
        with running(BoundedHTTPServer(("127.0.0.1", 0), WorkerFixture)) as worker, \
                running(CrawlerGatewayServer(("127.0.0.1", 0),
                        upstream_address=worker.server_address)) as gateway:
            for method, path, body in [("GET", "/health", None),
                                       ("POST", "/read", {"url": "https://example.com/"})]:
                with self.subTest(method=method):
                    client = http.client.HTTPConnection(*gateway.server_address, timeout=2)
                    try:
                        client.request(method, path, None if body is None else json.dumps(body))
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        result = json.loads(response.read())
                        if body is None:
                            self.assertEqual(result["service"], "crawler")
                        else:
                            self.assertEqual(result["worker_received"], body)
                    finally:
                        client.close()

    def test_absolute_url_and_host_cannot_select_another_upstream(self):
        with running(BoundedHTTPServer(("127.0.0.1", 0), WorkerFixture)) as worker, \
                running(CrawlerGatewayServer(("127.0.0.1", 0),
                        upstream_address=worker.server_address)) as gateway:
            client = http.client.HTTPConnection(*gateway.server_address, timeout=2)
            try:
                client.request("GET", "http://169.254.169.254/latest/meta-data/",
                               headers={"Host": "127.0.0.1:22"})
                result = json.loads(client.getresponse().read())
                self.assertEqual(result["service"], "crawler")
                self.assertEqual(result["path"], "http://169.254.169.254/latest/meta-data/")
            finally:
                client.close()

    def test_unavailable_worker_returns_503(self):
        # Reserve a non-listening port so no unrelated process can claim it.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            with running(CrawlerGatewayServer(("127.0.0.1", 0),
                         upstream_address=reserved.getsockname())) as gateway:
                client = http.client.HTTPConnection(*gateway.server_address, timeout=2)
                try:
                    client.request("GET", "/health")
                    response = client.getresponse()
                    self.assertEqual(response.status, 503)
                    self.assertEqual(json.loads(response.read()),
                                     {"error": "Crawler service is unavailable."})
                finally:
                    client.close()

    def test_stalled_worker_connection_expires(self):
        with running(BoundedHTTPServer(("127.0.0.1", 0), WorkerFixture)) as worker:
            worker.release = threading.Event()
            try:
                with running(CrawlerGatewayServer(("127.0.0.1", 0),
                             upstream_address=worker.server_address, timeout=0.1)) as gateway:
                    with socket.create_connection(gateway.server_address, timeout=2) as client:
                        client.sendall(b"GET /stall HTTP/1.1\r\nHost: crawler\r\n\r\n")
                        # EOF must arrive before the client's own timeout.
                        self.assertEqual(client.recv(1), b"")
            finally:
                worker.release.set()


if __name__ == "__main__":
    unittest.main()

"""Bounded file downloads through the existing validating HTTP/CONNECT proxy."""

import asyncio
import base64
import http.client
import ssl
import time
from urllib.parse import urljoin, urlsplit

from .asset_common import AssetFailure
from .security import UnsafeURL, validate_url_syntax


async def browser_intermediates(origin, proxy, timeout):
    """Recover missing intermediates from a TLS-verified Chromium connection.

    Only intermediate CA certificates are returned. Neither leaf certificates
    nor new root trust anchors are installed into Python's trust store.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from playwright.async_api import async_playwright

    verified = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, proxy={"server": proxy},
                                                  args=["--proxy-bypass-list=<-loopback>", "--disable-quic"])
        try:
            context = await browser.new_context(ignore_https_errors=False, java_script_enabled=False,
                                                service_workers="block", accept_downloads=False)
            count = 0

            async def route(request_route):
                nonlocal count
                count += 1
                try:
                    validate_url_syntax(request_route.request.url)
                    if count > 32 or request_route.request.resource_type != "document":
                        await request_route.abort()
                    else:
                        await request_route.continue_()
                except UnsafeURL:
                    await request_route.abort()

            await context.route("**/*", route)
            page = await context.new_page()
            page.on("response", lambda response: verified.append(response.url)
                    if response.url.startswith(origin + "/") else None)
            try:
                await page.goto(origin + "/", wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except Exception:
                if not verified:
                    raise AssetFailure("certificate_error") from None
            session = await context.new_cdp_session(page)
            chain = await session.send("Network.getCertificate", {"origin": origin})
            intermediates = []
            for value in chain.get("tableNames", [])[:10]:
                certificate = x509.load_der_x509_certificate(base64.b64decode(value, validate=True))
                try:
                    ca = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                except x509.ExtensionNotFound:
                    ca = False
                if certificate.issuer != certificate.subject and ca:
                    intermediates.append(certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"))
            if not verified or not intermediates:
                raise AssetFailure("certificate_error")
            return "".join(intermediates)
        finally:
            await browser.close()


def download(url, proxy, destination, maximum, timeout, connection_factory=None):
    """GET a single bounded public asset; redirects never bypass URL validation."""
    deadline = time.monotonic() + timeout
    proxy_parts = urlsplit(proxy)
    contexts = {}
    url = validate_url_syntax(url)
    for _ in range(7):
        parts = urlsplit(url)
        origin = parts.scheme + "://" + parts.netloc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssetFailure("timeout")
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": min(15, remaining)}
        if origin in contexts:
            kwargs["context"] = contexts[origin]
        connection = (connection_factory or cls)(proxy_parts.hostname, proxy_parts.port or 80, **kwargs)
        try:
            target = url
            if parts.scheme == "https":
                connection.set_tunnel(parts.hostname, parts.port or 443)
                target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", target, headers={"Accept-Encoding": "identity", "Connection": "close",
                                                       "User-Agent": "KKB-Optional-DocumentReader/1.0"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise AssetFailure("upstream_error")
                url = validate_url_syntax(urljoin(url, location))
                continue
            if response.status != 200:
                raise AssetFailure("upstream_error")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > maximum):
                raise AssetFailure("asset_too_large")
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if encoding not in {"", "identity"}:
                raise AssetFailure("unsupported_content_type")
            total = 0
            with open(destination, "wb") as output:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AssetFailure("timeout")
                    if connection.sock is not None:
                        connection.sock.settimeout(min(15, remaining))
                    block = response.read(min(65536, maximum - total + 1))
                    if not block:
                        break
                    total += len(block)
                    if total > maximum:
                        raise AssetFailure("asset_too_large")
                    output.write(block)
            if total == 0 or (length is not None and total != int(length)):
                raise AssetFailure("upstream_error")
            return {"final_url": url, "downloaded_bytes": total,
                    "content_type": response.getheader("Content-Type", "").split(";", 1)[0].lower()}
        except ssl.SSLCertVerificationError as error:
            if error.verify_code not in {20, 21} or origin in contexts or connection_factory is not None:
                raise AssetFailure("certificate_error") from None
            pem = asyncio.run(browser_intermediates(origin, proxy, min(15, max(1, deadline - time.monotonic()))))
            context = ssl.create_default_context()
            context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
            context.load_verify_locations(cadata=pem)
            contexts[origin] = context
        finally:
            connection.close()
    raise AssetFailure("upstream_error")

# Optional web tools: upstream software and pins

This product includes software developed by UncleCode (https://x.com/unclecode)
as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

Search, HTML/file extraction, and local OCR use no hosted subscription or model key.
Optional MIA vision/OCR uses the operator's Kloudeks endpoint, key, and model quota.
Hosting resources and network access remain the operator's responsibility.
Search engines and target websites are external services with their own availability
and access rules. Source content is returned as untrusted evidence.

| Component | Selected version | Upstream license / source |
| --- | --- | --- |
| SearXNG | `2026.9.13-32f2da4ef` | [AGPL-3.0-or-later, source at the selected revision](https://github.com/searxng/searxng/tree/32f2da4ef0cef32a2d381f5fee78af6ff81643e9) |
| Crawl4AI | `0.9.3` | [Apache 2.0 with upstream attribution requirement](https://github.com/unclecode/crawl4ai/blob/v0.9.3/LICENSE) |
| Playwright Python | `1.62.0` | [Apache 2.0](https://github.com/microsoft/playwright-python/blob/main/LICENSE) |
| Chromium | Build selected by the locked Playwright package | [BSD-style project license and third-party notices](https://chromium.googlesource.com/chromium/src/+/main/LICENSE) |
| Python | `3.12-slim-bookworm`, immutable image digest | [Python Software Foundation license](https://docs.python.org/3/license.html) and [official image source](https://github.com/docker-library/python) |
| Docker Engine / Compose | Host prerequisites | [Moby source](https://github.com/moby/moby), [Compose source](https://github.com/docker/compose), Apache 2.0 |

The SearXNG image is pulled unchanged. Its published corresponding source and
license are linked above; preserve its license notices when distributing it.
The package and image distributions carry their individual license notices.
`requirements.lock` records the full Python dependency set, including components
the SDK brings for optional capabilities that this worker does not call.
In particular, Crawl4AI requires `unclecode-litellm==1.81.13`, whose dependencies
include the `openai` package. Those packages stay inside the crawler image;
HTML extraction does not use those vendor clients. Optional MIA calls use the
single standard-library Kloudeks client abstraction, without importing a vendor SDK.

## Optional file/image image target

`requirements-assets.lock` adds hash-locked parsers constrained by the existing
crawler lock, only in the `crawler-assets` target:

| Component | Version | Upstream source/license |
| --- | --- | --- |
| pypdf | `6.18.1` | [BSD-3-Clause](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| pdfplumber | `0.11.10` | [MIT](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt) |
| pdfminer.six | `20260107` | [MIT](https://github.com/pdfminer/pdfminer.six/blob/master/LICENSE) |
| pypdfium2 / PDFium | `5.13.0` wrapper | [Wrapper and bundled PDFium license notices](https://github.com/pypdfium2-team/pypdfium2/tree/main/LICENSES) |
| openpyxl | `3.1.5` | [MIT; project metadata](https://pypi.org/project/openpyxl/3.1.5/) |
| xlrd | `2.0.2` | [BSD-style license](https://github.com/python-excel/xlrd/blob/master/LICENSE) |
| defusedxml | `0.7.1` | [PSF license; project metadata](https://pypi.org/project/defusedxml/0.7.1/) |
| Pillow | `12.3.0`, shared with crawler lock | [MIT-CMU license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| Tesseract, English/Turkish data | Debian Bookworm packages at build time | [Apache-2.0 engine](https://github.com/tesseract-ocr/tesseract/blob/main/LICENSE), [language data](https://github.com/tesseract-ocr/tessdata/blob/main/LICENSE) |
| DejaVu fonts (OCR fixtures) | Debian Bookworm package | [License notices](https://github.com/dejavu-fonts/dejavu-fonts/blob/master/LICENSE) |

The MIA request protocol and exact model IDs follow the hackathon guide supplied
by the user. The service's availability, access rights, and quota are independent
of the open-source parser stack. No model weights are redistributed by this project.

## Immutable image references

Verified through public Docker Hub metadata on 2026-09-13:

- SearXNG: `searxng/searxng:2026.9.13-32f2da4ef@sha256:e4d74653dd655e15710221bda37864cb6d6b1634b72196021b20866f91bb730e`
- Python: `python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254`

Both references identify multi-architecture image manifests. The dependency lock
targets CPython 3.12 on Linux x86-64 / manylinux 2.28; this is the documented WSL
target. An ARM host needs its own lock validation before being treated as supported.
Playwright's pinned package fixes the browser revision downloaded during image
build. Debian packages installed by `playwright install --with-deps` follow the
Debian repository snapshot available at build time; they are not byte-for-byte
locked by the Python requirements file.

## Updating the lock

Resolve only in an isolated tooling environment, never the baseline `.venv`:

```bash
python3 -m venv /tmp/kkb-web-tools-lock-env
/tmp/kkb-web-tools-lock-env/bin/python -m pip install uv==0.11.0
/tmp/kkb-web-tools-lock-env/bin/uv pip compile extensions/web_tools/requirements.in \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --generate-hashes --output-file extensions/web_tools/requirements.lock \
  --no-emit-index-url --cache-dir /tmp/kkb-web-tools-uv-cache
/tmp/kkb-web-tools-lock-env/bin/uv pip compile extensions/web_tools/requirements-assets.in \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --generate-hashes --output-file extensions/web_tools/requirements-assets.lock \
  --no-emit-index-url --cache-dir /tmp/kkb-web-tools-uv-cache
```

Review version changes, validate the lock installation, rebuild the crawler,
and rerun the offline tests, readiness check, and real smoke command. Update image
digests deliberately after checking upstream releases. A passing offline test
does not establish that the live services or public search engines are available.

Primary references: [SearXNG container installation](https://docs.searxng.org/admin/installation-docker),
[SearXNG JSON format configuration](https://docs.searxng.org/admin/settings/settings_search.html),
[Crawl4AI 0.9.3 release](https://github.com/unclecode/crawl4ai/releases/tag/v0.9.3),
[Playwright browser installation](https://playwright.dev/python/docs/browsers),
[Docker internal networks](https://docs.docker.com/reference/compose-file/networks/#internal).

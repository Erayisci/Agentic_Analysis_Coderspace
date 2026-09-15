# Verification record

Initial baseline and isolated runtime checks completed on 2026-09-14 in the
existing WSL checkout. Docker deployment, the localhost ingress fix, and the BDDK
certificate-chain handling fix were verified on 2026-09-15 in that same checkout.

## Baseline and isolation

- Original branch: `main`; original working tree: clean.
- Baseline commit: `dde8663d81cffbfe6b102fbadd49d7f47a87f5ee`.
- Feature branch: `feature/open-source-web-tools` in the original checkout;
  no additional worktree, project clone, stash, reset, push, or merge.
- Ubuntu 24.04.4 LTS, WSL2 kernel `6.18.33.2-microsoft-standard-WSL2`, Python 3.12.3.
- No `AGENTS.md` was present in the checkout or parent directories.
  Contributor guidance in `CLAUDE.md`, `README.md`, and `Launch.MD` was read.
- No baseline virtual environment or installed pytest/flake8/data dependencies
  initially existed. Initial documented checks therefore reported missing
  pytest, flake8, openpyxl, and DuckDB. These were environment prerequisites,
  not failing baseline tests.
- Baseline verification used an archive of the baseline commit and a separate
  virtual environment under `/tmp`. The original raw data, generated-data
  directory, Python dependency files, and default startup were not modified.

## Executed checks

| Check | Actual result |
| --- | --- |
| Baseline blocking lint before edits | Passed: 0 errors |
| Baseline cached-workbook rendering before edits | Passed: 1,139 workbooks |
| Baseline lakehouse build before edits | Passed: 135,513 bulletin observations across 17 tables; 25 integrity checks |
| Baseline pytest before edits | 57 passed |
| Baseline rendering/build/pytest with the extension present in an isolated source copy | Passed; 57 tests passed again |
| Blocking lint on final checkout | Passed: 0 errors |
| `web-tools test` with system Python and no crawler/pytest packages | 61 passed; 14 optional cases skipped (9 file/OCR and 5 browser tests, run separately below) |
| Optional browser fixture tests with the locked SDK and Chromium | All 5 passed inside the rebuilt Docker container, including rejection of an untrusted certificate after missing-issuer fallback |
| `pip install --require-hashes -r requirements.lock` in a separate crawler environment | Passed |
| `pip check` in the crawler environment | Passed: no broken requirements |
| Compose configuration validation | Passed with checksum-verified official standalone Compose v2.39.4; no daemon required |
| `web-tools setup` in the actual checkout | Prepared ignored extension `.env`, left the feature disabled, reported missing Docker |
| Setup repeatability, environment preservation, process overrides, secret redaction, scoped lifecycle arguments | Passed using temporary checkouts and a recording Docker fixture |
| Actual `web-tools start` | Initially blocked by missing Docker; on 2026-09-15 both images built and all 3 services became healthy with `up --wait` |
| `web-tools check` from WSL after the gateway fix | Both crawler and SearXNG returned `ok` |
| Docker deployment smoke | Exit 0; 3 search results and successful page extraction; overall `partial` because DuckDuckGo returned CAPTCHA |
| Actual Docker network boundary | Crawler attached only to an internal network; direct public connection blocked; proxy rejected private HTTP/CONNECT destinations |
| `pip check` inside rebuilt crawler image | Passed: no broken requirements |
| BDDK quarterly indicators HTML read after certificate-chain fix | Exit 0; `ok`; all 8,495 extracted Markdown characters returned without truncation |

The baseline environment resolved DuckDB 1.5.5, pandas 3.0.5, openpyxl 3.1.5,
PyArrow 25.0.1, pytest 9.1.1, and flake8 7.3.0 from the unchanged baseline
requirements. These are verification observations, not updates to baseline pins.

Offline tests exercise result parsing, source URLs including fragments, dates,
domain filters, empty/partial/malformed responses, service errors, bounded retries,
real socket parsing with a read deadline, concurrency limits, registration and
invocation, disabled imports, URL normalization, special/private IP ranges,
mixed DNS answers, rebinding protection through pinned socket addresses, actual
HTTP/CONNECT proxy rejection, private redirects, unsupported types, truncation,
subprocess deadline cleanup, TLS browser flags, and repeatable CLI setup. Gateway
tests exercise real sockets for health/read forwarding, fixed upstream selection
despite hostile HTTP targets/headers, unavailable-worker HTTP 503, and a stalled
connection's deadline.

## Actual network and browser results

### Initial isolated runtime checks, 2026-09-14

Crawl4AI 0.9.3, Playwright 1.62.0 and its Chromium revision 1234 were installed in
temporary, isolated verification paths. Required host library packages were
downloaded and extracted under `/tmp` for this test; system packages were not
installed or upgraded. The documented deployment installs those libraries in
the container via `playwright install --with-deps`.

1. The real validating proxy retrieved `http://example.com/` and
   `https://docs.searxng.org/` successfully (HTTP 200).
2. The real worker read `https://example.com/`, returning title **Example Domain**,
   `text/html`, and 165 Markdown characters with `truncated=false`.
3. It read the JavaScript-rendered `https://quotes.toscrape.com/js/`, returning
   1,505 extracted characters, capped at the requested 500 with `truncated=true`.
4. SDK/browser fixtures additionally proved JavaScript changes the returned text,
   disabling it preserves the initial text, private browser subrequests and
   JavaScript navigation are blocked, private HTTP redirects are rejected, and
   a PDF content type is rejected. These fixtures do not use external websites.
5. A temporary local SearXNG service using upstream commit
   `32f2da4ef0cef32a2d381f5fee78af6ff81643e9` and this extension's engine settings
   was started from a source archive in its own virtual environment. The real
   `backend.tools.get_tools()` interface searched for **SearXNG documentation**,
   returned three results, selected `https://docs.searxng.org/`, and read that
   page through the real HTTP worker and Crawl4AI. The reader returned `ok`, its
   title, final URL, and 2,000 characters with explicit truncation. Search status
   was **partial** because DuckDuckGo returned a CAPTCHA; available engines still
   produced usable sources. There was no public SearXNG instance or paid fallback.

Temporary verification listeners bound to localhost/ephemeral ports and were
stopped after the checks. No model credentials or model calls were used.

### Docker deployment and ingress fix, 2026-09-15

The user enabled Docker Desktop WSL integration: Desktop 4.91.0 (239619), Docker
Engine 29.8.0, and integrated Compose v5.5.1. The existing ignored `.env`, project
identity, secret, feature flag, and baseline environment were preserved.

The first Docker run exposed an ingress bug: the crawler was healthy internally
and passed all four browser tests, but WSL could not reach port 8932. Docker inspect
showed a configured `127.0.0.1:8932` binding and an empty active port mapping for
the crawler, which was attached only to `crawler-internal` (`internal: true`).

The published port now belongs to the existing egress container. A bounded TCP
gateway forwards only to `crawler:8932`, while the crawler keeps its internal-only
network. The validating web proxy on 3128 remains unpublished. Dockerfile code
copies now follow dependency installation so code-only rebuilds can reuse the
locked SDK and browser layers.

Actual checks after rebuilding with `web-tools start`:

1. All three containers were healthy. Docker showed active loopback-only port
   mappings for SearXNG (8888) and the gateway (8932); the crawler had no host ports.
2. `web-tools check` from WSL returned `ok` for both services.
3. `web-tools browser-test` passed all four real SDK/browser fixtures in 11.113s.
4. `WEB_TOOLS_ENABLED=true ./extensions/web_tools/web-tools smoke 'SearXNG documentation'`
   exited 0. Brave supplied three results; Crawl4AI read `https://docs.searxng.org/`
   with status `ok`, its final URL and title, and 2,000 of 20,694 Markdown characters
   (`truncated=true`). Overall status was `partial` because DuckDuckGo returned a
   CAPTCHA, not because the reader failed.
5. Docker network inspection confirmed that the crawler's sole network was
   internal. A direct connection to `1.1.1.1:443` failed from the crawler while the
   same destination was reachable from the egress container as a control.
6. From inside the crawler, the real proxy rejected HTTP to `127.0.0.1` and HTTPS
   CONNECT to `169.254.169.254` with HTTP 403. A private read URL submitted through
   the host gateway was rejected by the worker with HTTP 400.
7. `pip check` in the rebuilt crawler image reported no broken requirements.

The services were left running for the user. This Docker verification did not
exercise `stop`; its project-scoping arguments are covered by the CLI tests.

### BDDK certificate-chain handling, 2026-09-15

The user's read of `https://www.bddk.org.tr/Veri/Detay/162` exposed a second issue:
the Python preflight failed with `SSLCertVerificationError`, OpenSSL error 20
(unable to get local issuer certificate). The previous client message incorrectly
described that as an HTTP error. Both the system CA bundle and locked certifi bundle
failed. OpenSSL inspection showed that the server supplied its leaf certificate
without the GlobalSign RSA OV SSL CA 2018 intermediate.

A diagnostic using the same isolated browser, proxy and normal Chromium TLS
verification successfully read the page. The worker now defers only OpenSSL issuer
chain errors 20/21 to Chromium's normal verifier. Hostname, expiry, and other
preflight certificate failures stop immediately. Browser certificate failures
return a sanitized, non-retryable `certificate_error`. No certificate verification
flags were disabled, no trust anchors were added, and no host CA store was changed.
Browser document guards still enforce URL, HTTP status, MIME, and size checks.

After rebuilding the extension:

- All 48 offline tests passed; all 5 optional browser tests passed in 13.787s;
  blocking lint reported 0 errors.
- The new browser fixture presented a leaf signed by an unknown issuer, omitting
  that issuer so the preflight produced the same error 20. The child then attempted
  browser verification and returned `certificate_error`; the fixture observed no
  HTTP request over the untrusted TLS connection and no content was extracted.
- The exact CLI read, with `--max-chars 20000`, exited 0 and returned `status: ok`,
  title `Veri Detay`, the original HTTPS source URL, 8,495 characters,
  `truncated: false`, and `error: null`.
- Both services still passed `web-tools check` from WSL. Dependencies, network
  isolation, baseline files, and the user's ignored configuration were preserved.

## Optional files, images, OCR, and MIA protocol, 2026-09-15

This continuation found the earlier work committed at `c858683` on `perhat`, with
a clean working tree. Remaining fixes and documentation were developed on
`feature/web-assets` based on that commit, then moved to `perhat` at the user's
request. No repository clone or worktree was created; existing local
configuration values were preserved and missing defaults appended with `setup`.
A second setup run left the file byte-for-byte unchanged. It remains Git-ignored.

The optional `crawler-assets` image built successfully from the hash-locked parser
requirements and the existing crawler runtime. All three services became healthy;
`pip check` reported no broken requirements. Parser/OCR packages were not installed
into the baseline environment. Docker Desktop's WSL socket became unavailable
during continuation; starting the existing Desktop installation restored it. The
agent used `sg docker` where needed for the existing group membership.

Actual checks on the final implementation:

| Check | Result |
| --- | --- |
| Offline extension suite | 61 passed; 14 optional integration cases skipped |
| Docker file/OCR fixtures | 9 passed: PDF text and table locations; CSV/XLSX limits; legacy XLS dates and row limits; DOCX paragraphs/tables; PNG/scanned-PDF local OCR; archive/pixel/text caps; isolated download/cache refresh; MIA batching and shared call limits |
| Docker browser fixtures | 5 passed; missing-issuer test also verified that the binary downloader rejected an untrusted certificate before receiving any HTTP content |
| Blocking repository lint | 0 errors |
| Git whitespace check | Passed |
| Original lakehouse regression with current extension source | Cached-workbook rendering and full build passed in `/tmp/kkb-assets-baseline-rm0hjbrm/source`; all 57 pytest tests passed in 3.15s using a separate virtual environment |
| BDDK discovery | 50 links, 8 images; attachment URLs ranked first; `links_truncated=true`, `images_truncated=false`, explicit `partial` status |
| BDDK report download | `https://www.bddk.org.tr/Veri/EkGetir/8?ekId=625`, detected PDF, 2,980,052 bytes, 47 pages |
| Selected PDF pages 1–2 | 899 characters, source locations retained, `partial` because only two pages were requested |
| Repeat PDF read | Cache hit, original fetch timestamp retained |
| Selected PDF pages 5–6 | 6,123 characters, two table sections with page locations, within the requested 8,000-character cap |
| Model transport fixtures | Exact supplied MIA OCR prompt/fields and base64 images; single/multiple-image windows; image/token limits; output truncation; sanitized 401/403/429/500 responses; no automatic retries |
| Restore standard HTML/search image | `start` and `check` passed; documents/images/links/OCR/vision reported false; a direct `/asset` POST returned `feature_disabled`; the default host registry remained empty |
| Live smoke after restoring standard services | 3 search results; successful read of `https://docs.searxng.org/`, bounded to 2,000 characters; overall `partial` due to an unavailable search engine |

The binary downloader initially could not recover BDDK's omitted intermediate
certificate because its DevTools session started after navigation. Enabling the
Network domain before the verified Chromium request makes the verified chain
available. Only non-root CA intermediates are loaded into a fresh Python TLS
context; partial-chain trust is disabled and the chain must reach an existing OS
root. No leaf/root certificates, TLS bypass, or host trust-store changes are used.
The original HTML certificate fallback remains intact.

MIA live vision and OCR remain **unverified**: no actual MIA API key was supplied
to the implementation session. Protocol and budget tests use fixtures; no real
model call or quota expenditure is claimed. Local Tesseract OCR was exercised with
actual image pixels and scanned PDFs, without a model service.

The new capabilities were left disabled, with normal HTML/search services running.
Enable them using [ASSETS.md](ASSETS.md). Test output and downloaded report results
were kept outside Git under `/tmp`; the checkout's baseline environment and generated
data were not modified. The work was initially left uncommitted for review; the
user subsequently requested delivery on `perhat` and removal only of the temporary
branches created during this implementation.

## Remaining platform and agent checks

The Python lock and tested Docker deployment target Linux x86-64 / Python 3.12;
ARM has not been tested. Debian browser support packages are installed
from Debian repositories at image build time, so that OS package layer is not
fully locked. See `THIRD_PARTY.md` for upstream references and update guidance.

There is no baseline agent loop, LangGraph registration layer, or application API
yet. The Kloudeks client and private extension credential configuration now support
optional MIA image/OCR calls. The callable mapping is tested, but an
agent conversation that selects tools and writes a citation cannot be verified
until that planned baseline functionality exists. No replacement agent was introduced.

## Review scope

Relative to the original baseline `dde8663`, the only existing file changed is the
root `README.md`. Added implementation, deployment, locks, tests, and documentation
live under `backend/tools/`, `backend/model_clients/`, `backend/extensions/`, and
`extensions/web_tools/`. The continuation's diff is relative to the web-tools commit
`c858683`; existing implementation work in that commit was preserved.

The baseline dependency files, CI workflow, model settings, ingestion/parsing
code, raw corpora, database, and baseline startup commands remain unchanged.
The extension's actual `.env`, virtual environments, and caches are ignored.
No generated browser files or verification downloads are part of the changes.

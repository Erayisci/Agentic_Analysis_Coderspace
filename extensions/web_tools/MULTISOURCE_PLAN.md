# Multi-source research implementation plan

Progress (2026-09-17): steps 1–5 completed for the standalone extension. All 111
extension tests passed across host and Docker suites, and live multi-source MIA
research passed with supplied URLs and with model-directed search. Baseline checks
previously passed. See [VERIFICATION.md](VERIFICATION.md) for scope and evidence.

Scope: extend the optional `ask` runner on `perhat`. Preserve the existing search,
HTML, document, OCR and vision tools and their default-off configuration.

1. **Track documents and retain evidence.** Separate document IDs (`D1`) from
   citation excerpts (`S1`). Recognize redirected aliases and exact file/content
   duplicates. Preserve every accepted tool result outside the model context;
   allow the model to inspect another part without downloading it again.
2. **Make sufficiency explicit.** Accept several starting URLs, optional required
   questions and a minimum distinct-document count. Ask the model to assess each
   requirement and report conflicting evidence. Validate all evidence references;
   expose unassessed/missing requirements instead of claiming completeness.
3. **Bound the whole run.** Retain tool/model/hourly limits and add distinct URL
   attempts, file-download allocation, retained-output bytes and elapsed-time
   budgets. Return a stop reason and all retained evidence on partial failure.
4. **Explain integration and demonstrate it.** Document the outer agent's role,
   output contract, limits, and a reproducible two-source CLI/Python example.
5. **Verify.** Add deterministic multi-source/duplicate/conflict/budget tests,
   run extension and Docker parser/browser suites, run a bounded live MIA
   multi-source pipeline, and check the baseline build/tests.

Acceptance criteria:

- Several pages of one PDF and identical copies do not satisfy a two-document
  requirement. Distinct documents are not automatically independent publishers.
- Search snippets never become cited evidence. Citation, coverage and conflict
  references must name excerpts actually shown to the model.
- Trimming the prompt never destroys accepted extraction output; saved run JSON
  contains that output. Per-read truncation remains visible and cannot be undone
  without another bounded read.
- Budget exhaustion or a failed source leaves earlier evidence usable and marks
  the result partial. A final answer identifies uncovered requirements/conflicts.
- No new baseline dependency, implicit ingestion or automatic calculation from
  unvalidated tables. Model assessments are labelled as assessments, not proof.

Limits of this increment: sequential reads; exact-duplicate detection only;
no semantic publisher-independence classifier; no recursive site crawl. File
download accounting excludes browser/search transport and conservatively charges
failed reads. The client deadline stops new work and bounds HTTP waits; an
already-running service task remains bounded by its service timeout.

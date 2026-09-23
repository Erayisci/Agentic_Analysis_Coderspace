"""Per-question evidence ledger, separate from the disposable model context."""

from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .asset_common import AssetFailure
from .security import UnsafeURL, validate_url_syntax


def canonical_url(url):
    """Conservative identity: no fragment/default port; preserve query semantics."""
    try:
        parts = urlsplit(validate_url_syntax(url))
    except (UnsafeURL, ValueError, TypeError):
        raise AssetFailure("invalid_url") from None
    host = parts.hostname.lower()
    port = parts.port
    if port and port != {"http": 80, "https": 443}[parts.scheme]:
        host += ":" + str(port)
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))


class EvidenceLedger:
    def __init__(self, maximum):
        self.maximum = maximum
        self.bytes = 0
        self.records, self.documents, self.sources = [], [], []
        self.aliases, self.fingerprints, self.texts = {}, {}, {}

    def root(self, doc_id):
        document = self.documents[int(doc_id[1:]) - 1]
        while document.get("duplicate_of"):
            document = self.documents[int(document["duplicate_of"][1:]) - 1]
        return document["id"]

    def identity(self, url):
        url = canonical_url(url)
        return self.root(self.aliases[url]) if url in self.aliases else url

    def retain(self, name, arguments, output):
        record = {"tool": name, "arguments": deepcopy(arguments), "output": deepcopy(output)}
        size = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
        if self.bytes + size > self.maximum:
            raise AssetFailure("evidence_limit")
        self.records.append(record)
        self.bytes += size
        return len(self.records) - 1

    def add_read(self, requested, evidence, record_index):
        if evidence.get("status") not in {"ok", "partial"} or not evidence.get("content"):
            return []
        final = canonical_url(evidence.get("final_url"))
        aliases = {canonical_url(requested), final}
        fingerprint = evidence.get("content_sha256")
        kind = evidence.get("fingerprint_kind")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            # Never identify two files by a shared, truncated introductory paragraph.
            fingerprint = None
            if evidence.get("status") == "ok" and not evidence.get("truncated"):
                fingerprint = hashlib.sha256(evidence["content"].encode()).hexdigest()
                kind = "complete_extraction"
        fingerprint_key = (kind, fingerprint) if fingerprint else None
        matches = {self.root(self.aliases[url]) for url in aliases if url in self.aliases}
        if fingerprint_key in self.fingerprints:
            matches.add(self.root(self.fingerprints[fingerprint_key]))
        if matches:
            doc_id = min(matches, key=lambda value: int(value[1:]))
            document = self.documents[int(doc_id[1:]) - 1]
            for other in matches - {doc_id}:
                old = self.documents[int(other[1:]) - 1]
                old["duplicate_of"] = doc_id
                document["urls"] = sorted(set(document["urls"] + old["urls"]))
                document["record_indices"] += old["record_indices"]
        else:
            doc_id = f"D{len(self.documents) + 1}"
            document = {"id": doc_id, "url": final, "urls": [], "record_indices": [],
                        "format": evidence.get("format"), "host": urlsplit(final).hostname,
                        "content_sha256": fingerprint, "fingerprint_kind": kind,
                        "independence": "not_assessed"}
            self.documents.append(document)
        document["urls"] = sorted(set(document["urls"]) | aliases)
        document["record_indices"].append(record_index)
        for alias in aliases:
            self.aliases[alias] = doc_id
        if fingerprint_key:
            self.fingerprints[fingerprint_key] = doc_id
        for source in self.sources:
            source["document_id"] = self.root(source["document_id"])
        added = []
        sections = evidence.get("sections") or [{"location": "Content", "method": "text", "text": evidence["content"]}]
        for section in sections:
            text = section.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            existing = next((source for source in self.sources if source["document_id"] == doc_id
                             and source["location"] == section.get("location") and source["method"] == section.get("method")
                             and self.texts[source["id"]] == text and source.get("offset", 0) == 0), None)
            if existing:
                added.append(existing)
                continue
            source = {"id": f"S{len(self.sources) + 1}", "document_id": doc_id, "url": final,
                      "fetched_at": evidence.get("fetched_at"), "location": section.get("location"),
                      "method": section.get("method"), "excerpt": text[:2500], "excerpt_truncated": len(text) > 2500,
                      "status": evidence["status"], "truncated": evidence.get("truncated", False),
                      "record_index": record_index, "offset": 0, "section_chars": len(text),
                      "source_trust": "untrusted_external"}
            self.sources.append(source)
            self.texts[source["id"]] = text
            added.append(source)
        return added

    def inspect(self, source_id, offset=0, max_chars=2000):
        source = next((item for item in self.sources if item["id"] == source_id), None)
        if source is None:
            raise AssetFailure("invalid_citation")
        text = self.texts[source_id]
        if offset >= len(text):
            raise AssetFailure("invalid_request")
        window = dict(source, id=f"S{len(self.sources) + 1}", parent_source_id=source_id,
                      excerpt=text[offset:offset + max_chars], offset=offset,
                      excerpt_truncated=offset > 0 or len(text) > offset + max_chars)
        self.sources.append(window)
        self.texts[window["id"]] = text
        return {"status": "ok", "source": window, "error": None}

    def document_count(self, source_ids=None):
        return len({self.root(s["document_id"]) for s in self.sources
                    if source_ids is None or s["id"] in source_ids})

    def context(self, requirements, history, minimum, usage):
        docs = [{"id": d["id"], "url": d["url"], "format": d["format"],
                 "source_ids": [s["id"] for s in self.sources if s["document_id"] == d["id"]]}
                for d in self.documents if not d.get("duplicate_of")]
        return deepcopy({"sources": self.sources, "documents": docs, "requirements": requirements,
                         "minimum_documents": minimum, "distinct_documents": self.document_count(),
                         "history": history, "usage": usage})


def assess_answer(action, requirements, shown, ledger, minimum):
    """Validate provenance, not semantic entailment or publisher independence."""
    citations = set(action["citations"])
    inline = set(re.findall(r"\[(S[0-9]+)\]", action["answer"]))
    if (citations - shown or citations != inline or not citations
            or re.search(r"https?://", action["answer"], re.I)):
        raise AssetFailure("invalid_citation")
    expected = {item["id"] for item in requirements}
    coverage, conflicts = action.get("coverage", []), action.get("conflicts", [])
    if len({item["requirement_id"] for item in coverage}) != len(coverage):
        raise AssetFailure("invalid_request")
    for item in coverage + conflicts:
        if item["requirement_id"] not in expected or set(item["citations"]) - citations:
            raise AssetFailure("invalid_citation")
    for item in coverage:
        if item["status"] == "supported" and not item["citations"]:
            raise AssetFailure("invalid_citation")
        if item["status"] == "conflicting" and not any(
                c["requirement_id"] == item["requirement_id"] and set(c["citations"]) <= set(item["citations"])
                for c in conflicts):
            raise AssetFailure("invalid_citation")
    for conflict in conflicts:
        if len(set(conflict["citations"])) < 2:
            raise AssetFailure("invalid_citation")
        # Two windows of the very same section are not two conflicting observations.
        locations = {(ledger.root(s["document_id"]), s["record_index"], s["location"], s["method"])
                     for s in ledger.sources if s["id"] in conflict["citations"]}
        if len(locations) < 2:
            raise AssetFailure("invalid_citation")
    by_id = {item["requirement_id"]: item for item in coverage}
    result = []
    for requirement in requirements:
        item = by_id.get(requirement["id"], {"requirement_id": requirement["id"], "status": "not_assessed",
                                           "citations": [], "note": "The model did not assess this requirement."})
        if any(c["requirement_id"] == requirement["id"] for c in conflicts):
            item = dict(item, status="conflicting")
        result.append(dict(item, requirement=requirement["text"],
                           assessment="not_assessed" if item["status"] == "not_assessed" else "model_assessed"))
    count = ledger.document_count(citations)
    gaps = [item["requirement"] for item in result if item["status"] != "supported"]
    if count < minimum:
        gaps.append(f"Required {minimum} distinct cited documents; obtained {count}.")
    return result, conflicts, gaps

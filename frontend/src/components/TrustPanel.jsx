import { useState } from "react";

function VerificationBadge({ verification }) {
  if (!verification) return null;
  const ok = verification.passed;
  return (
    <span className={`badge ${ok ? "badge-ok" : "badge-warn"}`}>
      {verification.scope === "web_provenance"
        ? (ok ? "Kaynaklar eşleşti" : "Araştırma eksik")
        : (ok ? "Doğrulandı" : `${verification.n_errors} sorun`)}
    </span>
  );
}

function Section({ title, count, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="trust-section">
      <button className="trust-section-toggle" onClick={() => setOpen((v) => !v)}>
        <span>{open ? "▾" : "▸"} {title}</span>
        {count !== undefined ? <span className="trust-count">{count}</span> : null}
      </button>
      {open ? <div className="trust-section-body">{children}</div> : null}
    </div>
  );
}

export default function TrustPanel({ citations, verification, audit, timings }) {
  const hasAnything = (citations?.length || verification || audit?.length || timings) ?? false;
  if (!hasAnything) {
    return <p className="empty-hint">Henüz bir doğrulama kaydı yok.</p>;
  }

  return (
    <div className="trust-panel">
      <div className="trust-header">
        <span>Güven katmanı</span>
        <VerificationBadge verification={verification} />
      </div>

      <Section title="Kaynaklar" count={citations?.length ?? 0} defaultOpen>
        {citations && citations.length ? (
          <ul className="citation-list">
            {citations.map((c, i) => (
              <li key={i}>
                <code>{c.table ?? c.source ?? "kaynak"}</code>
                {c.url ? <a href={c.url} target="_blank" rel="noreferrer"> {c.url}</a> : null}
                {c.filters ? (
                  <span className="citation-filters">
                    {" "}
                    {Object.entries(c.filters).map(([k, v]) => `${k}=${v}`).join(", ")}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-hint">Kaynak yok.</p>
        )}
      </Section>

      <Section title="Doğrulama kontrolleri" count={verification?.n_checks ?? 0}>
        {verification ? (
          <ul className="check-list">
            {verification.checks.map((c, i) => (
              <li key={i} className={c.passed ? "check-pass" : `check-${c.severity}`}>
                <span className="check-icon">{c.passed ? "✓" : "✕"}</span>
                <span className="check-name">{c.check}</span>
                <span className="check-detail">{c.detail}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-hint">Doğrulama çalışmadı.</p>
        )}
      </Section>

      <Section title="Çalıştırılan adımlar" count={audit?.length ?? 0}>
        {audit && audit.length ? (
          <ul className="audit-list">
            {audit.map((step, i) => (
              <li key={i} className={step.ok ? "audit-ok" : "audit-fail"}>
                <span className="audit-op">{step.op}</span>
                <span className="audit-detail">{step.detail}</span>
                {typeof step.seconds === "number" ? (
                  <span className="audit-seconds">{step.seconds.toFixed(2)} s</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-hint">Adım çalışmadı.</p>
        )}
      </Section>

      <Section title="Süreler" count={timings ? `${(timings.total ?? 0).toFixed(1)} s` : undefined}>
        {timings ? (
          <ul className="audit-list">
            {Object.entries(timings).map(([stage, seconds]) => (
              <li key={stage} className="audit-ok">
                <span className="audit-op">{stage}</span>
                <span className="audit-detail">{seconds.toFixed(3)} s</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-hint">Süre kaydı yok.</p>
        )}
      </Section>
    </div>
  );
}

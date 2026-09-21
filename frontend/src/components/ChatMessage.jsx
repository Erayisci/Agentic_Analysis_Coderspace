import { useState } from "react";

const COMPOSED_LABEL = {
  model: "model",
  template: "şablon (modelsiz)",
  skipped: "atlandı",
};

// The answer text already carries [K1]/[H1] tags and a "Kaynaklar:" legend
// (appended server-side, so it survives copy-paste and the API alone). This
// panel shows the same legend structured, with the SQL selectable.
function Sources({ sources, summary }) {
  const [open, setOpen] = useState(false);
  const used = (sources || []).filter((s) => (summary || "").includes(`[${s.tag}]`));
  if (!used.length) return null;
  return (
    <div className="sources">
      <button className="sources-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} Kaynaklar ({used.length})
      </button>
      {open ? (
        <ul className="sources-list">
          {used.map((s) => (
            <li key={s.tag}>
              <span className="source-ref">[{s.tag}]</span>
              <span className="source-detail">{s.detail}</span>
              {s.sql ? <code className="source-sql">{s.sql}</code> : null}
              {s.url ? <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// The legend is rendered by <Sources>; strip it from the prose bubble.
function withoutLegend(summary) {
  const at = (summary || "").indexOf("\nKaynaklar:");
  return at === -1 ? summary : summary.slice(0, at).trimEnd();
}

export default function ChatMessage({ turn }) {
  return (
    <div className="turn">
      <div className="bubble bubble-user">{turn.question}</div>
      {turn.error ? (
        <div className="bubble bubble-error">{turn.error}</div>
      ) : (
        <div className="bubble bubble-assistant">
          <p className="bubble-text">{withoutLegend(turn.summary) || "Bir cevap üretilemedi."}</p>
          <Sources sources={turn.sources} summary={turn.summary} />
          <div className="bubble-meta">
            <span className={`source-tag source-${turn.composed_by}`}>
              {COMPOSED_LABEL[turn.composed_by] || turn.composed_by}
            </span>
            {turn.unsupported_numbers?.length ? (
              <span className="source-tag source-warning">
                ⚠ {turn.unsupported_numbers.length} doğrulanamayan sayı
              </span>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}

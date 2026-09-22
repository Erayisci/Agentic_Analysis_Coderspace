import { useEffect, useState } from "react";
import { addExternalColumn, addSource, deleteSource, getSource, listSources } from "../api";

function statusLabel(status) {
  return { ok: "alındı", partial: "kısmen", empty: "seri yok", error: "hata" }[status] || status;
}

function SeriesRow({ series, onAdd, busy }) {
  const verified = series.unit_verified === true;
  const source = series.unit_source;
  return (
    <li className="source-series">
      <div className="source-series-main">
        <span className="source-series-name" title={series.series_key}>{series.name_clean || series.name}</span>
        <span className={`badge ${verified ? "badge-ok" : "badge-warn"}`}>
          {series.unit}{verified ? " ✓" : source === "model" ? " (model)" : " (tahmin)"}
        </span>
        <span className="source-series-meta">
          {series.temporal_semantics} · {series.native_frequency} · {series.n_periods} ay
        </span>
      </div>
      {verified && series.matched_lakehouse_key ? (
        <div className="source-series-match">≈ {series.matched_source}:{series.matched_lakehouse_key} (%{Number(series.match_agreement_pct).toFixed(1)})</div>
      ) : null}
      <button className="btn-secondary btn-small" disabled={busy} onClick={() => onAdd(series)}>Tabloya ekle</button>
    </li>
  );
}

function SourceCard({ source, sessionId, onColumnAdded, onRemoved }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !detail) {
      try {
        setDetail(await getSource(source.source_id));
      } catch (err) {
        setError(err.message);
      }
    }
  }

  async function add(series) {
    setBusy(true);
    setError(null);
    try {
      onColumnAdded(await addExternalColumn(sessionId, series.series_key));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await deleteSource(source.source_id);
      onRemoved(source.source_id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className="source-card">
      <button className="source-toggle" onClick={toggle}>
        <span>{open ? "▾" : "▸"} {source.title || source.url}</span>
        <span className={`badge ${source.status === "ok" ? "badge-ok" : "badge-warn"}`}>
          {statusLabel(source.status)} · {source.n_series} seri
        </span>
      </button>
      {open ? (
        <div className="source-body">
          <div className="source-facts">
            <a href={source.url} target="_blank" rel="noreferrer">{source.url}</a>
            <span>{source.kind} · {source.extraction_route} · {String(source.fetched_at).slice(0, 16)}</span>
          </div>
          {source.warnings ? <p className="ingest-note">{String(source.warnings).split(" | ").slice(0, 4).join(" · ")}</p> : null}
          {detail ? (
            detail.series.length ? (
              <ul className="source-series-list">
                {detail.series.map((s) => <SeriesRow key={s.series_key} series={s} onAdd={add} busy={busy} />)}
              </ul>
            ) : <p className="empty-hint">Bu kaynaktan zaman serisi çıkmadı.</p>
          ) : <p className="empty-hint">Yükleniyor…</p>}
          {error ? <p className="ingest-error">{error}</p> : null}
          <button className="btn-secondary btn-small btn-danger" disabled={busy} onClick={remove}>Kaynağı kaldır</button>
        </div>
      ) : null}
    </li>
  );
}

export default function SourcesPanel({ sessionId, onColumnAdded }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [hint, setHint] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sources, setSources] = useState([]);
  const [lastResult, setLastResult] = useState(null);

  async function refresh() {
    try {
      const data = await listSources();
      setSources(data.sources);
    } catch {
      // the API may not be up yet; the panel stays usable
    }
  }

  useEffect(() => {
    listSources().then((data) => setSources(data.sources)).catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setLastResult(null);
    try {
      const result = await addSource({ url: url.trim(), hint: hint.trim() || undefined });
      setLastResult(result);
      setUrl("");
      await refresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const landed = lastResult
    ? [lastResult, ...(lastResult.children || [])].reduce((n, r) => n + (r.n_series || 0), 0)
    : null;

  return (
    <div className="ingest-panel">
      <button className="ingest-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} Kaynaklar (dış veri) {sources.length ? `· ${sources.length}` : ""}
      </button>
      {open ? (
        <div className="ingest-form">
          <p className="ingest-note">
            Bir URL (Excel, CSV, PDF, görsel veya bunlara link veren sayfa) ver: içindeki tüm tablolar
            lakehouse'a alınır ve seriler burada listelenir. Soruya URL yazmak da aynı işi otomatik yapar.
          </p>
          <form onSubmit={submit}>
            <label>
              URL
              <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://.../rapor.pdf" required />
            </label>
            <label>
              İpucu (opsiyonel)
              <input value={hint} onChange={(e) => setHint(e.target.value)} placeholder="örn. konut kredisi faizi" />
            </label>
            {error ? <p className="ingest-error">{error}</p> : null}
            <button type="submit" className="btn-primary" disabled={loading || !url.trim()}>
              {loading ? "Alınıyor…" : "Lakehouse'a al"}
            </button>
          </form>
          {lastResult ? (
            <p className="ingest-note">
              {lastResult.cache_hit ? "Daha önce alınmıştı: " : "Alındı: "}
              {landed} seri{lastResult.children?.length ? ` (${lastResult.children.length} bağlı dosya dahil)` : ""}
              {lastResult.warnings?.length ? ` · ${lastResult.warnings.length} uyarı` : ""}
            </p>
          ) : null}
          <ul className="source-list">
            {sources.map((s) => (
              <SourceCard key={s.source_id} source={s} sessionId={sessionId}
                          onColumnAdded={onColumnAdded}
                          onRemoved={(id) => setSources((prev) => prev.filter((x) => x.source_id !== id))} />
            ))}
          </ul>
          {!sources.length ? <p className="empty-hint">Henüz alınmış dış kaynak yok.</p> : null}
        </div>
      ) : null}
    </div>
  );
}

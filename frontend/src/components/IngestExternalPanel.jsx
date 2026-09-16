import { useState } from "react";
import { debugIngestExternal } from "../api";

export default function IngestExternalPanel({ sessionId, onIngested }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [valueColumn, setValueColumn] = useState("");
  const [asName, setAsName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data = await debugIngestExternal({
        url: url.trim(),
        value_column: valueColumn.trim(),
        as_name: asName.trim() || undefined,
        session_id: sessionId,
      });
      onIngested(data);
      setUrl("");
      setValueColumn("");
      setAsName("");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="ingest-panel">
      <button className="ingest-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} Dış veri ekle (Excel/CSV)
      </button>
      {open ? (
        <form className="ingest-form" onSubmit={submit}>
          <p className="ingest-note">
            Model (Kloudeks) bağlı değilken planlayıcı bu adımı kendi kararıyla
            çağıramaz — bu form aynı kodu doğrudan çalıştırır, geliştirme/demo amaçlıdır.
          </p>
          <label>
            Dosya URL'si
            <input value={url} onChange={(e) => setUrl(e.target.value)}
                   placeholder="https://.../veri.xlsx" required />
          </label>
          <label>
            Değer sütunu
            <input value={valueColumn} onChange={(e) => setValueColumn(e.target.value)}
                   placeholder="örn. Fiyat" required />
          </label>
          <label>
            Sütun adı (opsiyonel)
            <input value={asName} onChange={(e) => setAsName(e.target.value)} placeholder="örn. altin" />
          </label>
          {error ? <p className="ingest-error">{error}</p> : null}
          <button type="submit" className="btn-primary" disabled={loading}>
            {loading ? "Ekleniyor…" : "Tabloya Ekle"}
          </button>
        </form>
      ) : null}
    </div>
  );
}

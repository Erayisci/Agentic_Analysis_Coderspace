import { useEffect, useState } from "react";
import { getResearch, listResearch } from "../api";

const STATUS = { ok: "Tamamlandı", partial: "Kısmi sonuç", error: "Başarısız", running: "Çalışıyor" };

export default function ResearchPanel({ sessionId, latest }) {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState(null);
  const [listError, setListError] = useState("");
  const latestId = latest?.evidence?.run_id;

  useEffect(() => {
    let active = true;
    listResearch(sessionId).then(({ runs: items }) => {
      if (!active) return;
      setRuns(items);
      setSelected((old) => latestId || old || items[0]?.id || "");
    }).catch((err) => { if (active) setListError(err.message); });
    return () => { active = false; };
  }, [sessionId, latestId]);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    getResearch(sessionId, selected).then((result) => {
      if (active) setDetail({ id: selected, data: result });
    }).catch((err) => { if (active) setDetail({ id: selected, error: err.message }); });
    return () => { active = false; };
  }, [sessionId, selected]);

  const loading = selected && detail?.id !== selected;
  const saved = detail?.id === selected ? detail.data : null;
  const error = listError || (detail?.id === selected ? detail.error : "");
  const data = saved?.response;
  const research = data?.research;
  return (
    <div className="research-panel">
      <h2>Araştırma &amp; kayıtlı kanıtlar</h2>
      <p className="empty-hint">Arama sonuçları ve okunan içerikler otomatik kaydedilir. Kayıtlar sayfa yenilendiğinde ve yeni sohbette korunur.</p>
      <label htmlFor="research-history">Kayıtlı sorular</label>
      <select id="research-history" value={selected} onChange={(event) => setSelected(event.target.value)}>
        {!runs.length && <option value="">Henüz kayıt yok</option>}
        {runs.map((run) => <option key={run.id} value={run.id}>
          {STATUS[run.status] || run.status} · {run.question}
        </option>)}
      </select>
      {error && <p role="alert" className="research-error">{error}</p>}
      {loading ? <p role="status">Kanıtlar yükleniyor…</p> : saved && !error && <>
        <p className="research-save-status">Veritabanına kaydedildi · {saved.tool_results.length} araç sonucu</p>
        <p><span className={`badge ${saved.status === "ok" ? "badge-ok" : "badge-warn"}`}>
          {STATUS[saved.status] || saved.status}
        </span> <small>{new Date(saved.created_at).toLocaleString()}</small></p>
        {research?.usage && <p className="empty-hint">
          {research.usage.model_calls} model çağrısı · {research.usage.tool_calls} araç çağrısı · {research.usage.distinct_documents} belge
        </p>}
        {data?.summary && <details><summary>Kayıtlı cevap</summary><p className="bubble-text">{data.summary}</p></details>}
        {research?.error && <p role="alert" className="research-error">
          {research.error.code}: {research.error.message}
        </p>}
        {!!research?.missing_information?.length && <div>
          <h3>Eksik bilgiler</h3>
          <ul>{research.missing_information.map((item, i) => <li key={i}>{item}</li>)}</ul>
        </div>}
        {!!data?.citations?.length && <div>
          <h3>Kaynaklar</h3>
          {data.citations.map((source, i) => <details key={source.id || i}>
            <summary>{source.id ? `[${source.id}] ` : ""}{source.title || source.url || source.table}
              {source.cited === false ? " (cevapta kullanılmadı)" : ""}</summary>
            {/^https?:\/\//i.test(source.url || "") && <a href={source.url} target="_blank" rel="noreferrer">Kaynağı aç</a>}
            {source.location && <p>{source.location}</p>}
            {source.excerpt && <p className="bubble-text">{source.excerpt}</p>}
            {(source.truncated || source.excerpt_truncated) && <p className="empty-hint">Kısmi içerik; tam araç çıktısını aşağıda inceleyin.</p>}
          </details>)}
        </div>}
        <h3>Çalıştırılan araçlar ve saklanan sonuçlar</h3>
        {!saved.tool_results.length && <p className="empty-hint">Bu soruda web aracı çalıştırılmadı.</p>}
        {saved.tool_results.map((record, i) => <details key={record.id}>
          <summary>{i + 1}. {record.tool} · {record.status}</summary>
          <p className="research-query">{record.arguments.query || record.arguments.url || record.arguments.source_id}</p>
          <pre>{JSON.stringify({ arguments: record.arguments, output: record.output }, null, 2)}</pre>
        </details>)}
        {!!research?.warnings?.length && <details>
          <summary>Sınırlamalar</summary>
          <ul>{research.warnings.map((item, i) => <li key={i}>{item}</li>)}</ul>
        </details>}
      </>}
    </div>
  );
}

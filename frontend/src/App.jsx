import { useEffect, useRef, useState } from "react";
import { ask, health, resetSession } from "./api";
import ChatMessage from "./components/ChatMessage";
import DataTable from "./components/DataTable";
import ChartPanel from "./components/ChartPanel";
import TrustPanel from "./components/TrustPanel";
import SourcesPanel from "./components/SourcesPanel";
import ResearchPanel from "./components/ResearchPanel";
import kkbLogo from "./assets/kkb-logo.png";
import "./App.css";

function sessionIdFromStorage() {
  const existing = localStorage.getItem("kkb_session_id");
  if (existing) return existing;
  const fresh = crypto.randomUUID();
  localStorage.setItem("kkb_session_id", fresh);
  return fresh;
}

const EXAMPLE_QUESTIONS = [
  "Konut kredisi verilerini göster",
  "2021-2025 arası konut kredisi ve faiz oranını göster",
  "Enflasyon nasıl değişti",
];
const RESEARCH_QUESTIONS = [
  "BDDK Türk Bankacılık Sektörü Temel Göstergeleri raporunun resmi sayfasını bul, oku ve kapsamını kaynak göstererek açıkla.",
  "TCMB'nin en son faiz kararını resmi kaynaktan araştır ve kaynak göstererek özetle.",
];

export default function App() {
  const [sessionId] = useState(sessionIdFromStorage);
  const [turns, setTurns] = useState([]);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [modelConfigured, setModelConfigured] = useState(null);
  const [researchConfigured, setResearchConfigured] = useState(null);
  const [mode, setMode] = useState("auto");
  const [activeTab, setActiveTab] = useState("table");
  const [latest, setLatest] = useState(null); // last successful /ask response
  // The chart outlives the turn that drew it: "tablo yap" or "enflasyondan
  // arindir" after a chart keeps it on the Grafik tab. It is replaced when a
  // turn draws a new one, and cleared when a NEW question (not a follow-up)
  // takes the table somewhere else. `latest` alone is one turn deep, which
  // emptied the tab on every follow-up that did not ask for a chart.
  const [figure, setFigure] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    health()
      .then((data) => {
        setModelConfigured(data.model_configured);
        setResearchConfigured(data.research_configured);
      })
      .catch(() => setModelConfigured(false));
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [turns]);

  async function submitQuestion(text) {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setLoading(true);
    setQuestion("");
    setTurns((prev) => [...prev, { question: trimmed, pending: true }]);

    try {
      const data = await ask(trimmed, sessionId, mode);
      setTurns((prev) => prev.map((t, i) =>
        i === prev.length - 1
          ? { question: trimmed, summary: data.summary, composed_by: data.composed_by,
              unsupported_numbers: data.unsupported_numbers, sources: data.sources,
              evidence: data.evidence }
          : t));
      setLatest(data);
      if (data.figure) setFigure(data.figure);
      else if (!data.route?.is_followup) setFigure(null);
      // The side panel follows what the question asked for: a chart only when
      // one was requested, the table only when it was, a research turn shows
      // its saved evidence, otherwise stay put.
      if (data.presentation?.chart && data.figure) setActiveTab("chart");
      else if (data.presentation?.table) setActiveTab("table");
      else if (data.research) setActiveTab("research");
    } catch (err) {
      setTurns((prev) => prev.map((t, i) =>
        i === prev.length - 1 ? { question: trimmed, error: err.message } : t));
    } finally {
      setLoading(false);
    }
  }

  function handleIngested(data) {
    setLatest((prev) => ({ ...(prev || {}), ...data }));
    setActiveTab("table");
  }

  async function startNewChat() {
    try {
      await resetSession(sessionId);
    } catch {
      // a session that never asked anything is not an error either way
    }
    setTurns([]);
    setLatest(null);
    setFigure(null);
    setActiveTab("table");
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <img src={kkbLogo} alt="KKB Hackathon 2026" className="brand-logo" />
          <div className="brand-divider" />
          <div>
            <h1>Agentic Analytics</h1>
            <p className="app-subtitle">Türkiye bankacılık &amp; makro verisi üzerinde doğal dilde analiz</p>
          </div>
        </div>
        <div className="header-right">
          {modelConfigured !== null ? (
            <span className={`badge ${modelConfigured ? "badge-ok" : "badge-warn"}`}>
              {modelConfigured ? "Model bağlı" : "Modelsiz mod"}
            </span>
          ) : null}
          <button className="btn-secondary" onClick={startNewChat}>Yeni Sohbet</button>
        </div>
      </header>

      <main className="app-main">
        <section className="chat-panel">
          <div className="research-controls">
            <label htmlFor="question-mode">Soru modu</label>
            <select id="question-mode" value={mode} disabled={loading} onChange={(e) => setMode(e.target.value)}>
              <option value="auto">Otomatik / veri analizi</option>
              {/* Disabled (not hidden) when the backend reports it off, so the
                  reason is visible in the option itself -- a judge who picks
                  it used to only find out from a hint AFTER selecting it,
                  which read as broken rather than intentionally off. */}
              <option value="research" disabled={researchConfigured === false}>
                Web araştırması{researchConfigured === false ? " (kapalı)" : ""}
              </option>
            </select>
            {mode === "research" ? (
              <p className="empty-hint">
                Model arama yapar, kaynakları okur ve kanıtları veritabanına kaydeder.
              </p>
            ) : null}
          </div>
          <div className="chat-scroll" ref={scrollRef}>
            {turns.length === 0 ? (
              <div className="empty-state">
                <p>Bir soru sorarak başla:</p>
                <div className="example-chips">
                  {(mode === "research" ? RESEARCH_QUESTIONS : EXAMPLE_QUESTIONS).map((q) => (
                    <button key={q} className="chip" onClick={() => submitQuestion(q)}>{q}</button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((turn, i) => (
                turn.pending
                  ? <div className="turn" key={i}>
                      <div className="bubble bubble-user">{turn.question}</div>
                      <div className="bubble bubble-assistant bubble-loading">
                        {mode === "research" ? "Kaynaklar araştırılıyor ve kaydediliyor…" : "Düşünülüyor…"}
                      </div>
                    </div>
                  : <ChatMessage turn={turn} key={i} />
              ))
            )}
          </div>

          <form
            className="chat-input-row"
            onSubmit={(e) => { e.preventDefault(); submitQuestion(question); }}
          >
            <input
              type="text"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Sorunuzu yazın…"
              disabled={loading}
            />
            <button type="submit" className="btn-primary" disabled={loading || !question.trim()}>
              Gönder
            </button>
          </form>
        </section>

        <aside className="side-panel">
          <SourcesPanel sessionId={sessionId} onColumnAdded={handleIngested} />
          <div className="tab-bar">
            <button className={activeTab === "table" ? "tab active" : "tab"} onClick={() => setActiveTab("table")}>Tablo</button>
            <button className={activeTab === "chart" ? "tab active" : "tab"} onClick={() => setActiveTab("chart")}>Grafik</button>
            <button className={activeTab === "trust" ? "tab active" : "tab"} onClick={() => setActiveTab("trust")}>Güven Katmanı</button>
            <button className={activeTab === "research" ? "tab active" : "tab"} onClick={() => setActiveTab("research")}>Araştırma</button>
          </div>
          <div className="tab-content">
            {activeTab === "research" && <ResearchPanel sessionId={sessionId} latest={latest} />}
            {activeTab === "table" && (
              latest && latest.presentation && !latest.presentation.table
                ? <p className="empty-hint">
                    Bu soru tablo istemedi. Sorunuzda "tablo", "veri seti", "sütun" veya "listele"
                    gibi bir kelime geçirirseniz tablo otomatik oluşturulur.
                  </p>
                : <DataTable table={latest?.table} />
            )}
            {activeTab === "chart" && <ChartPanel figure={figure} />}
            {activeTab === "trust" && (
              <TrustPanel
                citations={latest?.citations}
                verification={latest?.verification}
                audit={latest?.audit}
                timings={latest?.timings}
              />
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}
import { useEffect, useRef, useState } from "react";
import { ask, health, resetSession } from "./api";
import ChatMessage from "./components/ChatMessage";
import DataTable from "./components/DataTable";
import ChartPanel from "./components/ChartPanel";
import TrustPanel from "./components/TrustPanel";
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

export default function App() {
  const [sessionId] = useState(sessionIdFromStorage);
  const [turns, setTurns] = useState([]);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [modelConfigured, setModelConfigured] = useState(null);
  const [activeTab, setActiveTab] = useState("table");
  const [latest, setLatest] = useState(null); // last successful /ask response
  const scrollRef = useRef(null);

  useEffect(() => {
    health()
      .then((data) => setModelConfigured(data.model_configured))
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
      const data = await ask(trimmed, sessionId);
      setTurns((prev) => prev.map((t, i) =>
        i === prev.length - 1
          ? { question: trimmed, summary: data.summary, composed_by: data.composed_by,
              unsupported_numbers: data.unsupported_numbers }
          : t));
      setLatest(data);
      if (data.figure) setActiveTab("chart");
    } catch (err) {
      setTurns((prev) => prev.map((t, i) =>
        i === prev.length - 1 ? { question: trimmed, error: err.message } : t));
    } finally {
      setLoading(false);
    }
  }

  async function startNewChat() {
    try {
      await resetSession(sessionId);
    } catch {
      // a session that never asked anything is not an error either way
    }
    setTurns([]);
    setLatest(null);
    setActiveTab("table");
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <h1>KKB Agentic Analytics</h1>
          <p className="app-subtitle">Türkiye bankacılık &amp; makro verisi üzerinde doğal dilde analiz</p>
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
          <div className="chat-scroll" ref={scrollRef}>
            {turns.length === 0 ? (
              <div className="empty-state">
                <p>Bir soru sorarak başla:</p>
                <div className="example-chips">
                  {EXAMPLE_QUESTIONS.map((q) => (
                    <button key={q} className="chip" onClick={() => submitQuestion(q)}>{q}</button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((turn, i) => (
                turn.pending
                  ? <div className="turn" key={i}>
                      <div className="bubble bubble-user">{turn.question}</div>
                      <div className="bubble bubble-assistant bubble-loading">Düşünülüyor…</div>
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
          <div className="tab-bar">
            <button className={activeTab === "table" ? "tab active" : "tab"} onClick={() => setActiveTab("table")}>Tablo</button>
            <button className={activeTab === "chart" ? "tab active" : "tab"} onClick={() => setActiveTab("chart")}>Grafik</button>
            <button className={activeTab === "trust" ? "tab active" : "tab"} onClick={() => setActiveTab("trust")}>Güven Katmanı</button>
          </div>
          <div className="tab-content">
            {activeTab === "table" && <DataTable table={latest?.table} />}
            {activeTab === "chart" && <ChartPanel figure={latest?.figure} />}
            {activeTab === "trust" && (
              <TrustPanel
                citations={latest?.citations}
                verification={latest?.verification}
                audit={latest?.audit}
              />
            )}
          </div>
        </aside>
      </main>
    </div>
  );
}

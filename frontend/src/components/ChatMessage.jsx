const COMPOSED_LABEL = {
  model: "model",
  llm: "model",
  unavailable: "cevap tamamlanamadı",
  template: "şablon (modelsiz)",
  skipped: "atlandı",
};

export default function ChatMessage({ turn }) {
  return (
    <div className="turn">
      <div className="bubble bubble-user">{turn.question}</div>
      {turn.error ? (
        <div className="bubble bubble-error">{turn.error}</div>
      ) : (
        <div className="bubble bubble-assistant">
          <p className="bubble-text">{turn.summary || "Bir cevap üretilemedi."}</p>
          <div className="bubble-meta">
            <span className={`source-tag source-${turn.composed_by}`}>
              {COMPOSED_LABEL[turn.composed_by] || turn.composed_by}
            </span>
            {turn.ingestion?.status === "saved" && <span className="source-tag">
              Veritabanına kaydedildi · {turn.ingestion.tool_results} araç sonucu
            </span>}
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

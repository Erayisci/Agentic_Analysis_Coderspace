const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

async function request(path, options) {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => item.msg).join(", ")
      : body.detail || `İstek başarısız oldu (${response.status})`;
    throw new Error(detail);
  }
  return response.json();
}

export function health() {
  return request("/health");
}

export function ask(question, sessionId, mode = "auto") {
  return request("/ask", {
    method: "POST",
    body: JSON.stringify({ question, session_id: sessionId, mode }),
  });
}

export function listResearch(sessionId) {
  return request(`/session/${encodeURIComponent(sessionId)}/research`);
}

export function getResearch(sessionId, runId) {
  return request(`/session/${encodeURIComponent(sessionId)}/research/${encodeURIComponent(runId)}`);
}

export function getSession(sessionId) {
  return request(`/session/${encodeURIComponent(sessionId)}`);
}

export function resetSession(sessionId) {
  return request(`/session/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

// Bypasses the planner: ingest_external needs a real model to preview a file
// and decide value_column, which the deterministic fallback cannot do. This
// exercises the same executor code directly, for demo/dev without a
// Kloudeks key. See backend/api/main.py's debug_ingest_external docstring.
export function debugIngestExternal(payload) {
  return request("/debug/ingest_external", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

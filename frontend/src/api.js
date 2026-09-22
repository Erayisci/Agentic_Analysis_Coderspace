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
    const error = new Error(detail);
    error.status = response.status;
    throw error;
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

// The external zone: demo-day sources landed in the lakehouse. A URL in a
// question lands automatically; these let the panel do it explicitly, list
// what has landed, and add one landed series to the session's table.
export function listSources() {
  return request("/sources");
}

export function addSource(payload) {
  return request("/sources", { method: "POST", body: JSON.stringify(payload) });
}

export function getSource(sourceId) {
  return request(`/sources/${encodeURIComponent(sourceId)}`);
}

export function deleteSource(sourceId) {
  return request(`/sources/${encodeURIComponent(sourceId)}`, { method: "DELETE" });
}

export function addExternalColumn(sessionId, seriesKey, asName) {
  return request(`/session/${encodeURIComponent(sessionId)}/columns`, {
    method: "POST",
    body: JSON.stringify({ series_key: seriesKey, session_id: sessionId, as_name: asName || undefined }),
  });
}

// Deprecated: the session-scoped, single-column path. Kept for the one case
// where a user names one exact column; see backend/api/main.py.
export function debugIngestExternal(payload) {
  return request("/debug/ingest_external", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

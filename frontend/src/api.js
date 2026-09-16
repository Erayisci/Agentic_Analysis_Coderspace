const BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

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

export function ask(question, sessionId) {
  return request("/ask", {
    method: "POST",
    body: JSON.stringify({ question, session_id: sessionId }),
  });
}

export function getSession(sessionId) {
  return request(`/session/${encodeURIComponent(sessionId)}`);
}

export function resetSession(sessionId) {
  return request(`/session/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

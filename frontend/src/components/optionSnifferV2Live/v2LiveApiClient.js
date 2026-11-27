const resolveLiveApiRoot = () => {
  const host = (typeof window !== 'undefined' && window.location && window.location.hostname) || '127.0.0.1';
  const envBase =
    (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE_URL) ||
    `http://${host}:8000`;
  const clean = String(envBase).replace(/\/+$/, '');
  const apiBase = clean.endsWith('/api') ? clean : `${clean}/api`;
  return `${apiBase}/option-sniffer-advance-v2-live`;
};

export const V2_LIVE_API_ROOT = resolveLiveApiRoot();

async function liveJsonFetch(path, options = {}) {
  const url = `${V2_LIVE_API_ROOT}${path}`;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const msg = (body && (body.detail || body.message)) || `HTTP ${res.status} on ${path}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return body;
}

export async function fetchV2LiveStatus(includePaperBaseline = true) {
  return liveJsonFetch(`/status?include_paper_baseline=${includePaperBaseline ? 'true' : 'false'}`);
}

export async function fetchV2LivePositions() {
  return liveJsonFetch('/positions');
}

export async function fetchV2LiveAuditLog(limit = 50) {
  return liveJsonFetch(`/audit-log?limit=${limit}`);
}

export async function postV2LiveArm(payload) {
  return liveJsonFetch('/arm', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function postV2LiveDisarm(reason = 'HUMAN_UI_DISARM') {
  return liveJsonFetch('/disarm', {
    method: 'POST',
    body: JSON.stringify({ reason }),
  });
}

export async function postV2LiveReconcile() {
  return liveJsonFetch('/reconcile', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export async function postV2LiveExitPosition(livePackageId, payload = {}) {
  return liveJsonFetch(`/positions/${encodeURIComponent(livePackageId)}/exit`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

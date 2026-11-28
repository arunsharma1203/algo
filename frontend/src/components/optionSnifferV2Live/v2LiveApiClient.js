const resolveApiBase = () => {
  const host = (typeof window !== 'undefined' && window.location && window.location.hostname) || '127.0.0.1';
  const envBase =
    (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE_URL) ||
    `http://${host}:8000`;
  const clean = String(envBase).replace(/\/+$/, '');
  return clean.endsWith('/api') ? clean : `${clean}/api`;
};

export const V2_LIVE_API_ROOT = `${resolveApiBase()}/option-sniffer-advance-v2-live`;
export const V2_PAPER_API_ROOT = `${resolveApiBase()}/option-sniffer-advance-v2`;

async function jsonPostFetch(root, path, payload = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (root === V2_LIVE_API_ROOT) {
    headers['X-V2-Operator-Intent'] = 'confirmed';
    try {
      const opToken =
        typeof window !== 'undefined' && window.localStorage
          ? window.localStorage.getItem('v2_operator_token')
          : null;
      if (opToken) {
        headers['X-V2-Operator-Token'] = opToken;
      }
    } catch {
      // Ignore storage read errors in constrained contexts
    }
  }
  const res = await fetch(`${root}${path}`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
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

async function liveJsonFetch(path, options = {}) {
  const res = await fetch(`${V2_LIVE_API_ROOT}${path}`, {
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

export async function fetchV2LiveFundsPreview(forceRefresh = false) {
  return liveJsonFetch(`/funds-preview?force_refresh=${forceRefresh ? 'true' : 'false'}`);
}

export async function fetchV2LivePositions() {
  return liveJsonFetch('/positions');
}

export async function fetchV2LiveAuditLog(limit = 50) {
  return liveJsonFetch(`/audit-log?limit=${limit}`);
}

export async function postV2LiveArmChallenge() {
  return jsonPostFetch(V2_LIVE_API_ROOT, '/arm-challenge', {});
}

export async function postV2LiveArm(payload) {
  return jsonPostFetch(V2_LIVE_API_ROOT, '/arm', payload);
}

export async function postV2LiveDisarm(reason = 'HUMAN_UI_DISARM') {
  return jsonPostFetch(V2_LIVE_API_ROOT, '/disarm', { reason });
}

export async function postV2LiveSosFlatten(reason = 'OPERATOR_SOS_FLATTEN') {
  return jsonPostFetch(V2_LIVE_API_ROOT, '/sos-flatten', { reason });
}

export async function postV2LiveReconcile() {
  return jsonPostFetch(V2_LIVE_API_ROOT, '/reconcile', {});
}

export async function postV2LiveExitPosition(livePackageId, payload = {}) {
  return jsonPostFetch(V2_LIVE_API_ROOT, `/positions/${encodeURIComponent(livePackageId)}/exit`, payload);
}

export async function postV2PaperMode(paperMode, lotsPerBook = undefined) {
  const payload = { paper_mode: paperMode };
  if (lotsPerBook !== undefined && lotsPerBook !== null) {
    payload.lots_per_book = Number(lotsPerBook);
  }
  return jsonPostFetch(V2_PAPER_API_ROOT, '/control/paper-mode', payload);
}

export async function postV2StartCollector() {
  return jsonPostFetch(V2_PAPER_API_ROOT, '/control/start-collector', {});
}

export async function postV2ShutdownCollector() {
  return jsonPostFetch(V2_PAPER_API_ROOT, '/control/shutdown-v2-only', {});
}

export async function postV2ReconcileBookingFault() {
  return jsonPostFetch(V2_PAPER_API_ROOT, '/control/reconcile-booking-fault', {});
}

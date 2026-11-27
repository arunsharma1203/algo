import { API_BASE } from '../../services/api.js';

// API_BASE already ends with `/api` (e.g. http://localhost:8000/api).
export const V2_API_ROOT = `${API_BASE}/option-sniffer-advance-v2`;

export const V2_ENDPOINTS = Object.freeze({
  overview: '/overview',
  positions: '/positions',
  dailyComparison: '/daily-comparison',
  replayAudit: '/replay-audit',
  counterfactuals: '/counterfactuals',
  diagnostics: '/diagnostics',
  opportunities: '/opportunities',
});

export function nowIstClockLabel() {
  try {
    return `${new Intl.DateTimeFormat('en-IN', {
      timeZone: 'Asia/Kolkata',
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }).format(new Date())} IST`;
  } catch {
    return new Date().toISOString();
  }
}

export function buildV2EndpointUrl(path, queryParams = null) {
  const cleanPath = String(path).startsWith('/') ? String(path) : `/${String(path)}`;
  if (!queryParams) {
    return `${V2_API_ROOT}${cleanPath}`;
  }
  const qs = new URLSearchParams();
  Object.entries(queryParams).forEach(([k, v]) => {
    if (v !== undefined && v !== null) {
      qs.set(k, String(v));
    }
  });
  const encoded = qs.toString();
  return encoded ? `${V2_API_ROOT}${cleanPath}?${encoded}` : `${V2_API_ROOT}${cleanPath}`;
}

async function fetchOneReadOnlyEndpoint(key, url, { fetchFn, signal, prevEntry }) {
  try {
    const res = await fetchFn(url, { method: 'GET', signal });
    if (!res.ok) {
      const errLabel = `HTTP ${res.status} from ${url}`;
      if (prevEntry && prevEntry.data) {
        return [
          key,
          {
            data: prevEntry.data,
            status: 'stale',
            stale: true,
            unavailable: false,
            httpStatus: res.status,
            asOfIst: prevEntry.asOfIst || null,
            error: errLabel,
          },
        ];
      }
      return [
        key,
        {
          data: null,
          status: 'unavailable',
          stale: false,
          unavailable: true,
          httpStatus: res.status,
          asOfIst: null,
          error: errLabel,
        },
      ];
    }
    const json = await res.json();
    const fetchedAt = nowIstClockLabel();
    return [
      key,
      {
        data: json,
        status: 'fresh',
        stale: false,
        unavailable: false,
        httpStatus: res.status,
        asOfIst: fetchedAt,
        error: null,
      },
    ];
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw err;
    }
    const errMsg = (err && err.message) || `Request failed for ${url}`;
    if (prevEntry && prevEntry.data) {
      return [
        key,
        {
          data: prevEntry.data,
          status: 'stale',
          stale: true,
          unavailable: false,
          httpStatus: null,
          asOfIst: prevEntry.asOfIst || null,
          error: errMsg,
        },
      ];
    }
    return [
      key,
      {
        data: null,
        status: 'unavailable',
        stale: false,
        unavailable: true,
        httpStatus: null,
        asOfIst: null,
        error: errMsg,
      },
    ];
  }
}

/**
 * Fetches all read-only V2 endpoints in parallel without fabricating placeholder
 * metrics on 404 or network failure. Partial failures retain per-endpoint cached state
 * marked `stale: true` with timestamp, or `unavailable: true` (`data: null`).
 */
export async function fetchAllV2EndpointsReadOnly({
  fetchFn = fetch,
  signal = undefined,
  includeCollateral = false,
  collateralYieldPa = 6.0,
  haircutPct = 10.0,
  previousState = {},
} = {}) {
  const overviewQuery = {
    include_collateral: includeCollateral ? 'true' : 'false',
    collateral_yield_pa: collateralYieldPa,
    haircut_pct: haircutPct,
  };
  const targets = [
    ['overview', buildV2EndpointUrl(V2_ENDPOINTS.overview, overviewQuery)],
    ['positions', buildV2EndpointUrl(V2_ENDPOINTS.positions)],
    ['dailyComparison', buildV2EndpointUrl(V2_ENDPOINTS.dailyComparison)],
    ['replayAudit', buildV2EndpointUrl(V2_ENDPOINTS.replayAudit)],
    ['counterfactuals', buildV2EndpointUrl(V2_ENDPOINTS.counterfactuals)],
    ['diagnostics', buildV2EndpointUrl(V2_ENDPOINTS.diagnostics)],
    ['opportunities', buildV2EndpointUrl(V2_ENDPOINTS.opportunities)],
  ];

  const settled = await Promise.allSettled(
    targets.map(([key, url]) =>
      fetchOneReadOnlyEndpoint(key, url, {
        fetchFn,
        signal,
        prevEntry: previousState[key],
      })
    )
  );

  const nextState = {};
  let aborted = false;
  settled.forEach((item, idx) => {
    const key = targets[idx][0];
    if (item.status === 'fulfilled') {
      nextState[item.value[0]] = item.value[1];
    } else if (item.reason && item.reason.name === 'AbortError') {
      aborted = true;
    } else {
      nextState[key] = previousState[key]?.data
        ? {
            data: previousState[key].data,
            status: 'stale',
            stale: true,
            unavailable: false,
            httpStatus: null,
            asOfIst: previousState[key].asOfIst || null,
            error: item.reason?.message || 'Endpoint fetch failed',
          }
        : {
            data: null,
            status: 'unavailable',
            stale: false,
            unavailable: true,
            httpStatus: null,
            asOfIst: null,
            error: item.reason?.message || 'Endpoint fetch failed',
          };
    }
  });

  if (aborted) {
    const err = new Error('Aborted');
    err.name = 'AbortError';
    throw err;
  }

  return nextState;
}

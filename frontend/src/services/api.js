import axios from 'axios';

const getHost = () => (typeof window !== 'undefined' && window.location.hostname) ? window.location.hostname : 'localhost';
export const API_URL = `http://${getHost()}:8000/api`;
export const API_BASE = API_URL;
export const QLIB_API_URL = `http://${getHost()}:8001/api`;

export const runBacktest = async (data) => {
    try {
        const response = await axios.post(`${API_URL}/backtest/`, data);
        return response.data;
    } catch (error) {
        console.error("Backtest Error:", error);
        throw error;
    }
};

export const getLatestData = async (ticker) => {
    try {
        const response = await axios.get(`${API_URL}/market/latest/${ticker}`);
        return response.data;
    } catch (error) {
        console.error("Market Data Error:", error);
        throw error;
    }
};

export const searchTickers = async (query) => {
    try {
        const response = await axios.get(`${API_URL}/market/search?q=${query}`);
        return response.data;
    } catch (error) {
        console.error("Search Error:", error);
        return [];
    }
};

export const getDashboardIntelligence = async (forceRefresh = false) => {
    try {
        const response = await axios.get(`${API_URL}/dashboard/intelligence?force_refresh=${forceRefresh}`);
        return response.data;
    } catch (error) {
        console.error("Dashboard Intelligence Error:", error);
        throw error;
    }
};

export const downloadDashboardReportPdf = async (forceRefresh = false) => {
    try {
        const response = await axios.get(`${API_URL}/dashboard/report/pdf?force_refresh=${forceRefresh}`, {
            responseType: 'blob'
        });
        return response.data;
    } catch (error) {
        console.error("Dashboard PDF Download Error:", error);
        throw error;
    }
};

export const triggerTelegramReport = async (force = false) => {
    try {
        const response = await axios.post(`${API_URL}/dashboard/report/telegram/send?force=${force}`);
        return response.data;
    } catch (error) {
        console.error("Trigger Telegram Report Error:", error);
        throw error;
    }
};

export const getReportDeliveryStatus = async () => {
    try {
        const response = await axios.get(`${API_URL}/dashboard/report/status`);
        return response.data;
    } catch (error) {
        console.error("Report Delivery Status Error:", error);
        return null;
    }
};

// ── Watchlist Backend API (Zero LocalStorage Single Source of Truth) ──
export const getWatchlist = async () => {
    try {
        const response = await axios.get(`${API_URL}/watchlist`);
        return response.data;
    } catch (error) {
        console.error("Get Watchlist Error:", error);
        throw error;
    }
};

export const addToWatchlist = async (ticker, notes = '') => {
    try {
        const response = await axios.post(`${API_URL}/watchlist`, { ticker, notes });
        return response.data;
    } catch (error) {
        console.error("Add To Watchlist Error:", error);
        throw error;
    }
};

export const removeFromWatchlist = async (ticker) => {
    try {
        const response = await axios.delete(`${API_URL}/watchlist/${encodeURIComponent(ticker)}`);
        return response.data;
    } catch (error) {
        console.error("Remove From Watchlist Error:", error);
        throw error;
    }
};

export const reorderWatchlist = async (order) => {
    try {
        const response = await axios.put(`${API_URL}/watchlist/reorder`, { order });
        return response.data;
    } catch (error) {
        console.error("Reorder Watchlist Error:", error);
        throw error;
    }
};

export const migrateLegacyWatchlist = async (tickers) => {
    try {
        const response = await axios.post(`${API_URL}/watchlist/migrate`, { tickers });
        return response.data;
    } catch (error) {
        console.error("Migrate Watchlist Error:", error);
        throw error;
    }
};

export const getWatchlistPresets = async () => {
    try {
        const response = await axios.get(`${API_URL}/watchlist/presets`);
        return response.data;
    } catch (error) {
        console.error("Get Presets Error:", error);
        throw error;
    }
};

export const scanWatchlistBatch = async (preset = 'WATCHLIST', tickers = null) => {
    try {
        const response = await axios.post(`${API_URL}/watchlist/scan`, { preset, tickers });
        return response.data;
    } catch (error) {
        console.error("Batch Scan Error:", error);
        throw error;
    }
};

// ==========================================
// AUTONOMOUS RESEARCH LAB / AUTOPILOT APIS
// ==========================================

export const getResearchStatus = async (missionId = null) => {
    const url = missionId ? `${API_URL}/research-autopilot/status?mission_id=${missionId}` : `${API_URL}/research-autopilot/status`;
    const res = await axios.get(url);
    return res.data;
};

export const getResearchMissions = async () => {
    const res = await axios.get(`${API_URL}/research-autopilot/missions`);
    return res.data;
};

export const createResearchMission = async (payload) => {
    const res = await axios.post(`${API_URL}/research-autopilot/mission/create`, payload);
    return res.data;
};

export const startResearchMission = async (missionId) => {
    const res = await axios.post(`${API_URL}/research-autopilot/mission/start?mission_id=${missionId}`);
    return res.data;
};

export const pauseResearchMission = async (missionId) => {
    const res = await axios.post(`${API_URL}/research-autopilot/mission/pause?mission_id=${missionId}`);
    return res.data;
};

export const resumeResearchMission = async (missionId) => {
    const res = await axios.post(`${API_URL}/research-autopilot/mission/resume?mission_id=${missionId}`);
    return res.data;
};

export const stopResearchMission = async (missionId) => {
    const res = await axios.post(`${API_URL}/research-autopilot/mission/stop?mission_id=${missionId}`);
    return res.data;
};

export const runResearchIteration = async (missionId, batchSize = 4) => {
    const res = await axios.post(`${API_URL}/research-autopilot/iteration?mission_id=${missionId}&batch_size=${batchSize}`);
    return res.data;
};

export const getResearchQueue = async (missionId) => {
    const res = await axios.get(`${API_URL}/research-autopilot/queue?mission_id=${missionId}`);
    return res.data;
};

export const getResearchExperiment = async (experimentId) => {
    const res = await axios.get(`${API_URL}/research-autopilot/experiment/${experimentId}`);
    return res.data;
};

export const downloadExperimentReportPDF = async (experimentId) => {
    try {
        const response = await axios.get(`${API_URL}/research-autopilot/experiment/${experimentId}/report.pdf`, {
            responseType: 'blob'
        });
        return response.data;
    } catch (error) {
        console.error("Experiment PDF Download Error:", error);
        throw error;
    }
};

export const getResearchLeaderboard = async (missionId = null) => {
    const url = missionId ? `${API_URL}/research-autopilot/leaderboard?mission_id=${missionId}` : `${API_URL}/research-autopilot/leaderboard`;
    const res = await axios.get(url);
    return res.data;
};

export const getResearchFrontier = async (missionId = null) => {
    const url = missionId ? `${API_URL}/research-autopilot/frontier?mission_id=${missionId}` : `${API_URL}/research-autopilot/frontier`;
    const res = await axios.get(url);
    return res.data;
};

export const getResearchLineage = async (missionId) => {
    const res = await axios.get(`${API_URL}/research-autopilot/lineage?mission_id=${missionId}`);
    return res.data;
};

export const getResearchVault = async (missionId = null) => {
    const url = missionId ? `${API_URL}/research-autopilot/vault?mission_id=${missionId}` : `${API_URL}/research-autopilot/vault`;
    const res = await axios.get(url);
    return res.data;
};

export const runUniverseTransfer = async (candidateId, targetUniverse = 'RESEARCH_100') => {
    const res = await axios.post(`${API_URL}/research-autopilot/vault/universe-transfer`, {
        candidate_id: candidateId,
        target_universe: targetUniverse
    });
    return res.data;
};

export const getMissionRuntime = async (missionId) => {
    const res = await axios.get(`${API_URL}/research-autopilot/mission/${missionId}/runtime`);
    return res.data;
};

export const getResearchTelemetrySSEUrl = (missionId = null, lastEventId = null) => {
    let url = `${API_URL}/research-autopilot/telemetry`;
    const params = [];
    if (missionId) params.push(`mission_id=${encodeURIComponent(missionId)}`);
    if (lastEventId) params.push(`last_event_id=${encodeURIComponent(lastEventId)}`);
    if (params.length > 0) url += `?${params.join('&')}`;
    return url;
};

export const runAutonomousResearch = async (payload = {}) => {
    const res = await axios.post(`${API_URL}/research-autopilot/run-autonomous`, payload);
    return res.data;
};

export const getLegacyRuntimeHealth = async () => {
    try {
        const response = await axios.get(`${API_URL}/scheduler/status`, { timeout: 3000 });
        return {
            status: response.data.status === 'RUNNING' ? 'RUNNING' : 'OFFLINE',
            data: response.data
        };
    } catch (err) {
        return {
            status: 'OFFLINE',
            error: err.message,
            data: null
        };
    }
};

export const getQlibRuntimeHealth = async () => {
    try {
        const response = await axios.get(`${QLIB_API_URL}/qlib/runtime-status`, { timeout: 3000 });
        return {
            status: response.data.status === 'RUNNING' ? 'RUNNING' : 'OFFLINE',
            data: response.data
        };
    } catch (err) {
        try {
            const fallback = await axios.get(`${API_URL}/qlib/status`, { timeout: 2000 });
            return {
                status: 'RUNNING',
                data: {
                    ...fallback.data,
                    runtime_status: 'ONLINE (PORT 8000)',
                    port: 8000
                }
            };
        } catch (e) {
            return {
                status: 'OFFLINE',
                error: err.message,
                data: null
            };
        }
    }
};




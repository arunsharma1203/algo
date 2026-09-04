import axios from 'axios';

const getHost = () => (typeof window !== 'undefined' && window.location.hostname) ? window.location.hostname : 'localhost';
export const API_URL = `http://${getHost()}:8000/api`;
export const API_BASE = API_URL;

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


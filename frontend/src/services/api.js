import axios from 'axios';

const API_URL = 'http://localhost:8000/api';

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

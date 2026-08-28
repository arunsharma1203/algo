# AlgoTrader Pro - Algorithmic Trading Platform

A full-fledged algorithmic trading research and backtesting platform for Indian/NSE stocks, built with React and Python.

## Prerequisites

- Python 3.9+
- Node.js 18+

## Setup & Installation

### 1. Backend (Python/FastAPI)

Navigate to the `backend` directory and set up your virtual environment:

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

To start the FastAPI server:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
The backend API will be available at http://localhost:8000/docs.

### 2. Frontend (React/Vite)

Open a new terminal and navigate to the `frontend` directory:

```bash
cd frontend
npm install
```

To start the Vite development server:

```bash
npm run dev
```
The React dashboard will be available at http://localhost:5173.

## How it works

- **Architecture:** The React frontend allows users to build strategies visually (or select presets). It converts these into a JSON schema and sends a request to the FastAPI backend.
- **Data:** The backend uses `yfinance` to fetch historical stock data.
- **Indicators:** The backend uses the `ta` library to compute technical indicators (EMA, RSI, MACD, etc.).
- **Backtesting Engine:** A custom sequential engine processes the dataframe row-by-row, evaluating the entry and exit conditions defined in the strategy JSON, generating simulated trades, applying transaction costs, and tracking portfolio equity.

## Features

- **Dashboard:** Overview of active strategies and current stock indicators.
- **Strategy Library:** Pre-defined templates for popular strategies like EMA Crossovers and Mean Reversion.
- **Custom Strategy Builder:** A visual UI to combine indicators (e.g., RSI < 30 AND Close > 200 EMA) to form entry and exit rules. No coding required.
- **Backtest Results:** Interactive charts displaying equity curves, drawdowns, and a detailed trade history.

## Disclaimer

Past performance does not guarantee future results. Backtests are simulations and may differ from live trading due to execution prices, liquidity, slippage, and other factors.

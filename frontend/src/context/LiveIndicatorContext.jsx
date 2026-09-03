import React, { createContext, useState, useContext, useCallback } from 'react';
import { API_BASE } from '../services/api';

const LiveIndicatorContext = createContext();

export function LiveIndicatorProvider({ children }) {
  const [isFetching, setIsFetching] = useState(false);
  const [lastFetched, setLastFetched] = useState(null);
  const [metadata, setMetadata] = useState(null);

  const triggerFetchIndicator = useCallback((meta = null) => {
    setIsFetching(true);
    if (meta) setMetadata(meta);
    setTimeout(() => {
      setIsFetching(false);
      setLastFetched(new Date().toLocaleTimeString());
    }, 800); // simulate a slight visual delay for the indicator
  }, []);

  const forceRealtimeFetch = async (ticker) => {
    if (!ticker) return;
    setIsFetching(true);
    try {
      await fetch(`${API_BASE}/market/clear-cache/${ticker}`, { method: 'POST' });
      // Reload the page to trigger fresh data fetch
      window.location.reload();
    } catch (err) {
      console.error("Failed to clear cache", err);
    } finally {
      setIsFetching(false);
    }
  };

  return (
    <LiveIndicatorContext.Provider value={{ isFetching, lastFetched, metadata, triggerFetchIndicator }}>
      {children}

      {/* Bottom Status Bar */}
      <div className="fixed bottom-0 left-64 right-0 h-10 bg-slate-900 text-gray-300 border-t border-slate-700 text-xs flex items-center px-4 justify-between shadow-[0_-2px_10px_rgba(0,0,0,0.2)] z-40">
        <div className="flex items-center space-x-4">
          <span className="flex items-center"><span className="h-2 w-2 rounded-full bg-green-500 mr-2"></span> System: Online</span>
          <span className="text-slate-600">|</span>
          <span className="flex items-center">
            Source: <span className="text-blue-400 font-medium ml-1">{metadata?.source || "Yahoo Finance"}</span>
            {metadata?.source?.includes("Local Database") && metadata?.ticker && (
              <button 
                onClick={() => forceRealtimeFetch(metadata.ticker)}
                className="ml-3 bg-blue-600 hover:bg-blue-500 text-white px-2 py-0.5 rounded text-[10px] font-bold tracking-wider transition uppercase shadow-sm"
              >
                Force Real-Time
              </button>
            )}
          </span>
          {metadata?.data_points && (
            <>
              <span className="text-slate-600">|</span>
              <span>Rows: <span className="font-medium text-white">{metadata.data_points}</span></span>
            </>
          )}
        </div>
        <div className="flex items-center space-x-4">
          {metadata?.execution_time_ms && (
            <span>Latency: <span className="font-medium text-white">{metadata.execution_time_ms}ms</span></span>
          )}
          {lastFetched && (
            <>
              <span className="text-slate-600">|</span>
              <span>Last Synced: <span className="font-medium text-white">{lastFetched}</span></span>
            </>
          )}
        </div>
      </div>
    </LiveIndicatorContext.Provider>
  );
}

export const useLiveIndicator = () => useContext(LiveIndicatorContext);

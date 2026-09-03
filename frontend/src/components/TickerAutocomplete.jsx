import React, { useState, useEffect, useRef } from 'react';
import { searchTickers } from '../services/api';

const POPULAR_TICKERS = [
  'RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS', 'ICICIBANK.NS',
  'SBIN.NS', 'MAZDOCK.NS', 'BEL.NS', 'HAL.NS', 'TRENT.NS'
];

export default function TickerAutocomplete({
  value = '',
  onChange,
  placeholder = "e.g. RELIANCE, TCS or MAZDOCK.NS, BEL.NS",
  className = ""
}) {
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const wrapperRef = useRef(null);
  const debounceTimerRef = useRef(null);

  // Close dropdown on click outside
  useEffect(() => {
    function handleClickOutside(event) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target)) {
        setShowSuggestions(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [wrapperRef]);

  // Extract the active token (text after the last comma)
  const getActiveToken = (text) => {
    const parts = text.split(',');
    return parts[parts.length - 1].trim();
  };

  const handleInputChange = (e) => {
    const newVal = e.target.value.toUpperCase();
    onChange(newVal);

    const activeToken = getActiveToken(newVal);
    if (!activeToken || activeToken.length < 1) {
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }

    setIsSearching(true);
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    debounceTimerRef.current = setTimeout(async () => {
      try {
        const results = await searchTickers(activeToken);
        // Prioritize Indian equities
        const filtered = (results || []).filter(item => 
          item.symbol?.endsWith('.NS') || item.symbol?.endsWith('.BO') || !item.symbol?.includes('.')
        );
        setSuggestions(filtered.slice(0, 8));
        setShowSuggestions(filtered.length > 0);
      } catch (err) {
        console.error("Ticker search error:", err);
      } finally {
        setIsSearching(false);
      }
    }, 300);
  };

  const handleSelectSuggestion = (selectedSymbol) => {
    // Standardize selected symbol with .NS if absent
    const cleanSym = selectedSymbol.includes('.') ? selectedSymbol : `${selectedSymbol}.NS`;
    const parts = value.split(',').map(s => s.trim()).filter(Boolean);

    if (parts.length <= 1) {
      onChange(cleanSym);
    } else {
      // Replace the last partial token with the selected symbol
      parts[parts.length - 1] = cleanSym;
      onChange(parts.join(', ') + ', ');
    }
    setShowSuggestions(false);
  };

  const handlePillClick = (sym) => {
    const parts = value.split(',').map(s => s.trim()).filter(Boolean);
    if (parts.includes(sym)) return;

    if (parts.length === 0 || (parts.length === 1 && parts[0] === '')) {
      onChange(sym);
    } else {
      onChange(parts.join(', ') + `, ${sym}`);
    }
  };

  return (
    <div className={`relative ${className}`} ref={wrapperRef}>
      <div className="relative">
        <input
          type="text"
          value={value}
          onChange={handleInputChange}
          onFocus={() => {
            const token = getActiveToken(value);
            if (token && suggestions.length > 0) setShowSuggestions(true);
          }}
          placeholder={placeholder}
          className="w-full bg-slate-950 border border-cyan-500/50 text-white font-mono text-xs rounded-lg px-3 py-2 pr-8 focus:outline-none focus:border-cyan-400 shadow-sm"
        />
        {isSearching && (
          <div className="absolute right-2.5 top-2.5">
            <span className="animate-spin block h-3.5 w-3.5 border-2 border-t-transparent border-cyan-400 rounded-full"></span>
          </div>
        )}
      </div>

      {/* Floating Suggestions Dropdown */}
      {showSuggestions && suggestions.length > 0 && (
        <ul className="absolute z-50 w-full bg-slate-900 border border-cyan-500/40 mt-1 rounded-xl shadow-2xl max-h-60 overflow-y-auto left-0 divide-y divide-slate-800 animate-fade-in">
          {suggestions.map((item, idx) => {
            const sym = item.symbol?.includes('.') ? item.symbol : `${item.symbol}.NS`;
            return (
              <li
                key={idx}
                onClick={() => handleSelectSuggestion(sym)}
                className="px-3 py-2 hover:bg-slate-800/80 cursor-pointer transition flex items-center justify-between group"
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-bold text-xs text-cyan-300 group-hover:text-cyan-200">{sym}</span>
                    <span className="text-[10px] bg-slate-800 text-slate-400 px-1.5 py-0.5 rounded font-mono">
                      {item.exchange || 'NSE'}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-400 truncate max-w-xs">{item.name || sym}</p>
                </div>
                <span className="text-[10px] text-cyan-400 opacity-0 group-hover:opacity-100 transition flex items-center gap-0.5">
                  Select
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {/* Quick Select Benchmark Pills */}
      <div className="mt-1.5 flex flex-wrap items-center gap-1">
        <span className="text-[9px] text-slate-500 mr-0.5">Presets:</span>
        {POPULAR_TICKERS.map((t) => {
          const isSelected = value.includes(t);
          return (
            <button
              type="button"
              key={t}
              onClick={() => handlePillClick(t)}
              className={`text-[9px] font-mono px-1.5 py-0.5 rounded border transition cursor-pointer ${
                isSelected 
                  ? 'bg-cyan-950/60 border-cyan-500/50 text-cyan-300 font-bold' 
                  : 'bg-slate-900 border-slate-800 text-slate-400 hover:border-slate-700 hover:text-slate-300'
              }`}
            >
              {t.replace('.NS', '')}
            </button>
          );
        })}
      </div>
    </div>
  );
}

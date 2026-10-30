import React, { useState, useEffect, useRef, useId } from 'react';
import { searchTickers } from '../services/api';
import { Search, Check, AlertCircle } from 'lucide-react';

const POPULAR_TICKERS = [
  'RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS', 'ICICIBANK.NS',
  'SBIN.NS', 'MAZDOCK.NS', 'BEL.NS', 'HAL.NS', 'TRENT.NS'
];

export default function TickerAutocomplete({
  value = '',
  onChange,
  onSubmit,
  placeholder = "e.g. RELIANCE, TCS or MAZDOCK.NS, BEL.NS",
  className = "",
  multiSelect = true,
  showPresets = true,
  variant = "dark", // "dark" | "light"
  id: customId
}) {
  const generatedId = useId();
  const componentId = customId || `ticker-ac-${generatedId.replace(/:/g, '')}`;
  
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [isSearching, setIsSearching] = useState(false);
  const wrapperRef = useRef(null);
  const inputRef = useRef(null);
  const listboxRef = useRef(null);
  const debounceTimerRef = useRef(null);

  // Close dropdown on click outside
  useEffect(() => {
    function handleClickOutside(event) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target)) {
        setShowSuggestions(false);
        setSelectedIndex(-1);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Scroll active item into view
  useEffect(() => {
    if (selectedIndex >= 0 && listboxRef.current) {
      const activeEl = listboxRef.current.children[selectedIndex];
      if (activeEl) {
        activeEl.scrollIntoView({ block: 'nearest' });
      }
    }
  }, [selectedIndex]);

  // Extract the active token (text after the last comma if multiSelect, else entire text)
  const getActiveToken = (text) => {
    if (!multiSelect) return text.trim();
    const parts = text.split(',');
    return parts[parts.length - 1].trim();
  };

  const handleInputChange = (e) => {
    const newVal = e.target.value.toUpperCase();
    onChange(newVal);
    setSelectedIndex(-1);

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
        setSelectedIndex(-1);
      } catch (err) {
        console.error("Ticker search error:", err);
      } finally {
        setIsSearching(false);
      }
    }, 300);
  };

  const handleSelectSuggestion = (selectedSymbol) => {
    const cleanSym = selectedSymbol.includes('.') ? selectedSymbol : `${selectedSymbol}.NS`;
    
    if (!multiSelect) {
      onChange(cleanSym);
      setShowSuggestions(false);
      setSelectedIndex(-1);
      if (onSubmit) onSubmit(cleanSym);
      return;
    }

    const parts = value.split(',').map(s => s.trim()).filter(Boolean);
    if (parts.length <= 1) {
      onChange(cleanSym);
    } else {
      parts[parts.length - 1] = cleanSym;
      onChange(parts.join(', ') + ', ');
    }
    setShowSuggestions(false);
    setSelectedIndex(-1);
    if (inputRef.current) inputRef.current.focus();
  };

  const handleKeyDown = (e) => {
    if (showSuggestions && suggestions.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedIndex(prev => (prev < suggestions.length - 1 ? prev + 1 : 0));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedIndex(prev => (prev > 0 ? prev - 1 : suggestions.length - 1));
        return;
      }
      if (e.key === 'Enter' && selectedIndex >= 0 && selectedIndex < suggestions.length) {
        e.preventDefault();
        const picked = suggestions[selectedIndex];
        const sym = picked.symbol?.includes('.') ? picked.symbol : `${picked.symbol}.NS`;
        handleSelectSuggestion(sym);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setShowSuggestions(false);
        setSelectedIndex(-1);
        return;
      }
    }

    if (e.key === 'Enter' && onSubmit && selectedIndex === -1) {
      e.preventDefault();
      setShowSuggestions(false);
      onSubmit(value);
    }
  };

  const handlePillClick = (sym) => {
    if (!multiSelect) {
      onChange(sym);
      if (onSubmit) onSubmit(sym);
      return;
    }

    const parts = value.split(',').map(s => s.trim()).filter(Boolean);
    if (parts.includes(sym)) return;

    if (parts.length === 0 || (parts.length === 1 && parts[0] === '')) {
      onChange(sym);
    } else {
      onChange(parts.join(', ') + `, ${sym}`);
    }
  };

  // Analyze tokens for validation feedback
  const tokens = multiSelect ? value.split(',').map(s => s.trim()).filter(Boolean) : [value.trim()].filter(Boolean);

  const isDark = variant === 'dark';

  return (
    <div className={`relative ${className}`} ref={wrapperRef}>
      <div className="relative flex items-center">
        <input
          ref={inputRef}
          id={componentId}
          type="text"
          role="combobox"
          aria-expanded={showSuggestions && suggestions.length > 0}
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-controls={`${componentId}-listbox`}
          aria-activedescendant={selectedIndex >= 0 ? `${componentId}-opt-${selectedIndex}` : undefined}
          value={value}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onFocus={() => {
            const token = getActiveToken(value);
            if (token && suggestions.length > 0) setShowSuggestions(true);
          }}
          placeholder={placeholder}
          className={`w-full font-mono text-xs rounded-lg px-3 py-2 pr-9 focus:outline-none transition shadow-xs ${
            isDark
              ? 'bg-slate-950 border border-cyan-500/50 text-white focus:border-cyan-400 placeholder:text-slate-500'
              : 'bg-white border border-slate-300 text-slate-900 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 placeholder:text-slate-400'
          }`}
        />
        {isSearching ? (
          <div className="absolute right-2.5">
            <span className={`animate-spin block h-3.5 w-3.5 border-2 border-t-transparent rounded-full ${isDark ? 'border-cyan-400' : 'border-blue-500'}`}></span>
          </div>
        ) : (
          <div className={`absolute right-2.5 pointer-events-none ${isDark ? 'text-slate-500' : 'text-slate-400'}`}>
            <Search size={13} />
          </div>
        )}
      </div>

      {/* Floating Suggestions Dropdown */}
      {showSuggestions && suggestions.length > 0 && (
        <ul
          id={`${componentId}-listbox`}
          ref={listboxRef}
          role="listbox"
          className={`absolute z-50 w-full mt-1 rounded-xl shadow-2xl max-h-60 overflow-y-auto left-0 divide-y animate-fade-in ${
            isDark
              ? 'bg-slate-900 border border-cyan-500/40 divide-slate-800 text-slate-100'
              : 'bg-white border border-slate-200 divide-slate-100 text-slate-900'
          }`}
        >
          {suggestions.map((item, idx) => {
            const sym = item.symbol?.includes('.') ? item.symbol : `${item.symbol}.NS`;
            const isHighlighted = idx === selectedIndex;
            return (
              <li
                key={idx}
                id={`${componentId}-opt-${idx}`}
                role="option"
                aria-selected={isHighlighted}
                onMouseEnter={() => setSelectedIndex(idx)}
                onClick={() => handleSelectSuggestion(sym)}
                className={`px-3 py-2 cursor-pointer transition flex items-center justify-between group ${
                  isHighlighted
                    ? isDark ? 'bg-cyan-950/70 text-cyan-200' : 'bg-blue-50 text-blue-900'
                    : isDark ? 'hover:bg-slate-800/80 text-slate-200' : 'hover:bg-slate-50 text-slate-700'
                }`}
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className={`font-mono font-bold text-xs ${
                      isDark ? 'text-cyan-300 group-hover:text-cyan-200' : 'text-blue-700 group-hover:text-blue-800'
                    }`}>
                      {sym}
                    </span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-mono ${
                      isDark ? 'bg-slate-800 text-slate-400' : 'bg-slate-100 text-slate-600'
                    }`}>
                      {item.exchange || 'NSE'}
                    </span>
                  </div>
                  <p className={`text-[11px] truncate max-w-xs ${isDark ? 'text-slate-400' : 'text-slate-500'}`}>
                    {item.name || sym}
                  </p>
                </div>
                <span className={`text-[10px] opacity-0 group-hover:opacity-100 transition flex items-center gap-0.5 font-bold ${
                  isDark ? 'text-cyan-400' : 'text-blue-600'
                }`}>
                  Select ↵
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {/* Token Validation Feedback (Multi-select) */}
      {multiSelect && tokens.length > 0 && (
        <div className="mt-1 flex items-center justify-between text-[10px] px-0.5">
          <span className={isDark ? 'text-cyan-400/80 font-mono' : 'text-blue-600 font-mono'}>
            ✓ {tokens.length} symbol{tokens.length > 1 ? 's' : ''} queued
          </span>
          <span className={isDark ? 'text-slate-500 font-mono' : 'text-slate-400 font-mono'}>
            Enter comma to separate
          </span>
        </div>
      )}

      {/* Quick Select Benchmark Pills */}
      {showPresets && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1">
          <span className={`text-[9px] mr-0.5 ${isDark ? 'text-slate-500' : 'text-slate-400'}`}>Presets:</span>
          {POPULAR_TICKERS.map((t) => {
            const isSelected = value.includes(t);
            return (
              <button
                type="button"
                key={t}
                onClick={() => handlePillClick(t)}
                className={`text-[9px] font-mono px-1.5 py-0.5 rounded border transition cursor-pointer ${
                  isSelected 
                    ? isDark 
                      ? 'bg-cyan-950/60 border-cyan-500/50 text-cyan-300 font-bold' 
                      : 'bg-blue-100 border-blue-400 text-blue-800 font-bold'
                    : isDark 
                      ? 'bg-slate-900 border-slate-800 text-slate-400 hover:border-slate-700 hover:text-slate-300'
                      : 'bg-slate-100 border-slate-200 text-slate-600 hover:bg-slate-200'
                }`}
              >
                {t.replace('.NS', '')}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

import React, { useState, useEffect, useRef } from 'react';
import { Search } from 'lucide-react';
import { searchTickers } from '../services/api';

export default function TickerSearch({ value, onChange, onSubmit, placeholder = "Search Ticker (e.g. GRSE)", className = "" }) {
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [isSearching, setIsSearching] = useState(false);
  const wrapperRef = useRef(null);
  const debounceTimerRef = useRef(null);

  useEffect(() => {
    function handleClickOutside(event) {
      if (wrapperRef.current && !wrapperRef.current.contains(event.target)) {
        setShowSuggestions(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [wrapperRef]);

  const handleInputChange = (e) => {
    const val = e.target.value.toUpperCase();
    onChange(val);
    
    if (val.trim() === '') {
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
        const results = await searchTickers(val);
        setSuggestions(results);
        setShowSuggestions(true);
      } catch (err) {
        console.error(err);
      } finally {
        setIsSearching(false);
      }
    }, 400); // 400ms debounce
  };

  const handleSelectSuggestion = (ticker) => {
    onChange(ticker);
    setShowSuggestions(false);
    if (onSubmit) onSubmit(ticker);
  };

  const handleSubmit = (e) => {
    if (e) e.preventDefault();
    setShowSuggestions(false);
    if (onSubmit) onSubmit(value);
  };

  return (
    <div className={`relative ${className}`} ref={wrapperRef}>
      <form onSubmit={handleSubmit} className="flex shadow-sm">
        <div className="relative flex-1">
          <input 
            type="text" 
            value={value}
            onChange={handleInputChange}
            onFocus={() => {if (suggestions.length > 0) setShowSuggestions(true)}}
            placeholder={placeholder}
            className="w-full border border-gray-300 rounded-l-md px-4 py-2 focus:outline-none focus:ring-1 focus:ring-blue-500 font-medium"
          />
          {isSearching && (
            <div className="absolute right-3 top-2.5">
              <span className="animate-spin block h-5 w-5 border-2 border-t-transparent border-blue-500 rounded-full"></span>
            </div>
          )}
        </div>
        <button type="submit" className="bg-blue-600 text-white px-4 py-2 rounded-r-md hover:bg-blue-700 transition">
          <Search size={20} />
        </button>
      </form>
      
      {showSuggestions && suggestions.length > 0 && (
        <ul className="absolute z-10 w-full bg-white border border-gray-200 mt-1 rounded-md shadow-xl max-h-72 overflow-auto left-0">
          {suggestions.map((suggestion, index) => (
            <li 
              key={index}
              onClick={() => handleSelectSuggestion(suggestion.symbol)}
              className="px-4 py-3 hover:bg-gray-50 cursor-pointer border-b border-gray-100 last:border-0"
            >
              <div className="flex justify-between items-center">
                <span className="font-bold text-gray-800">{suggestion.symbol}</span>
                <span className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded font-semibold">{suggestion.exchange}</span>
              </div>
              <p className="text-xs text-gray-500 mt-0.5 truncate">{suggestion.name}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

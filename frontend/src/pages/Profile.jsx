import React, { useState, useEffect } from 'react';
import { User, Key, Shield, HardDrive, DollarSign, Activity, DatabaseZap } from 'lucide-react';
import axios from 'axios';

export default function Profile() {
  const [profile, setProfile] = useState({
    name: '',
    apiKey: '',
    simulationMode: true,
    defaultCapital: 100000,
    maxRiskPerTrade: 2.0,
    dataSource: 'yfinance',
    dataApiKey: ''
  });
  
  const [savedMessage, setSavedMessage] = useState(false);
  const [hoardLogs, setHoardLogs] = useState([]);
  const [isHoarding, setIsHoarding] = useState(false);

  useEffect(() => {
    // Load from local storage on mount
    const saved = localStorage.getItem('swing_profile');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        if (!parsed.dataSource) parsed.dataSource = 'yfinance';
        setProfile(parsed);
      } catch(e) {}
    } else {
      // Backwards compatibility with the modal
      const oldKey = localStorage.getItem('indmoney_api_token');
      if (oldKey) {
        setProfile(p => ({ ...p, apiKey: oldKey }));
      }
    }
  }, []);

  useEffect(() => {
    // Auto-save whenever profile changes, but don't overwrite if it's the initial empty state
    if (profile.name || profile.apiKey || profile.dataSource !== 'yfinance') {
      localStorage.setItem('swing_profile', JSON.stringify(profile));
      localStorage.setItem('indmoney_api_token', profile.apiKey);
    }
  }, [profile]);

  const handleChange = (field, value) => {
    setProfile(p => ({ ...p, [field]: value }));
  };

  const handleSave = () => {
    localStorage.setItem('swing_profile', JSON.stringify(profile));
    localStorage.setItem('indmoney_api_token', profile.apiKey);
    
    setSavedMessage(true);
    setTimeout(() => setSavedMessage(false), 3000);
  };

  return (
    <div className="max-w-4xl mx-auto pb-10">
      <div className="mb-8 flex justify-between items-end">
        <div>
          <h1 className="text-3xl font-black text-gray-900 tracking-tight">System Settings & Profile</h1>
          <p className="text-gray-500 mt-2 font-medium">Manage your execution environment, API keys, and risk parameters.</p>
        </div>
        
        <button 
          onClick={handleSave}
          className="bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3 px-8 rounded-lg shadow-md transition transform hover:scale-105"
        >
          {savedMessage ? 'Settings Saved!' : 'Save Changes'}
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        
        {/* User Identity */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
          <div className="bg-gray-50 px-6 py-4 border-b border-gray-200 flex items-center">
            <User size={20} className="text-gray-500 mr-2" />
            <h2 className="font-bold text-gray-800">Trader Profile</h2>
          </div>
          <div className="p-6 space-y-4">
            <div>
              <label className="block text-sm font-bold text-gray-700 mb-2">Display Name</label>
              <input 
                type="text" 
                value={profile.name}
                onChange={(e) => handleChange('name', e.target.value)}
                placeholder="e.g. Arun Sharma"
                className="w-full border border-gray-300 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>
            
            <div className="pt-4 border-t border-gray-100">
              <label className="block text-sm font-bold text-gray-700 mb-2">Default Base Capital (₹)</label>
              <div className="relative">
                <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                  <span className="text-gray-500 font-bold">₹</span>
                </div>
                <input 
                  type="number" 
                  value={profile.defaultCapital}
                  onChange={(e) => handleChange('defaultCapital', parseFloat(e.target.value))}
                  className="w-full border border-gray-300 rounded-lg py-3 pl-8 pr-3 focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono"
                />
              </div>
              <p className="text-xs text-gray-500 mt-2">Used by the ML Scanner to calculate default trade quantities.</p>
            </div>
          </div>
        </div>

        {/* Execution Engine */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
          <div className="bg-gray-50 px-6 py-4 border-b border-gray-200 flex items-center justify-between">
            <div className="flex items-center">
              <Key size={20} className="text-gray-500 mr-2" />
              <h2 className="font-bold text-gray-800">INDmoney Execution</h2>
            </div>
            {profile.simulationMode ? (
              <span className="bg-blue-100 text-blue-800 text-xs font-bold px-2 py-1 rounded">SIMULATION</span>
            ) : (
              <span className="bg-red-100 text-red-800 text-xs font-bold px-2 py-1 rounded flex items-center">
                <Activity size={12} className="mr-1" /> LIVE TRADING
              </span>
            )}
          </div>
          
          <div className="p-6 space-y-6">
            <div>
              <label className="block text-sm font-bold text-gray-700 mb-2">INDstocks API Developer Token</label>
              <input 
                type="password" 
                value={profile.apiKey}
                onChange={(e) => handleChange('apiKey', e.target.value)}
                placeholder="Paste your Bearer Token here..."
                className="w-full border border-gray-300 rounded-lg p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>

            <div className="bg-gray-50 p-4 rounded-lg border border-gray-200">
              <label className="flex items-center cursor-pointer">
                <div className="relative">
                  <input 
                    type="checkbox" 
                    className="sr-only" 
                    checked={profile.simulationMode}
                    onChange={(e) => handleChange('simulationMode', e.target.checked)}
                  />
                  <div className={`block w-14 h-8 rounded-full transition ${profile.simulationMode ? 'bg-blue-400' : 'bg-red-500'}`}></div>
                  <div className={`dot absolute left-1 top-1 bg-white w-6 h-6 rounded-full transition transform ${profile.simulationMode ? 'translate-x-6' : ''}`}></div>
                </div>
                <div className="ml-4">
                  <span className={`block font-bold ${profile.simulationMode ? 'text-blue-800' : 'text-red-600'}`}>
                    {profile.simulationMode ? 'Simulation Mode Active' : 'Live Trading Enabled'}
                  </span>
                </div>
              </label>
            </div>
          </div>
        </div>

        {/* Data Architecture */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden md:col-span-2">
          <div className="bg-gray-50 px-6 py-4 border-b border-gray-200 flex items-center">
            <DatabaseZap size={20} className="text-gray-500 mr-2" />
            <h2 className="font-bold text-gray-800">Data Architecture & Maintenance</h2>
          </div>
          
          <div className="p-6 border-b border-gray-100 bg-white">
            <h3 className="font-bold text-gray-800 mb-4">Historical Data Provider</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">Primary Data Source</label>
                <select 
                  value={profile.dataSource || 'yfinance'}
                  onChange={(e) => handleChange('dataSource', e.target.value)}
                  className="w-full border border-gray-300 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option value="yfinance">Yahoo Finance (Free, 60-Day Limit)</option>
                  <option value="groww">Groww Trading API (₹499/mo, Deep History)</option>
                  <option value="upstox">Upstox Uplink (Free, Deep History)</option>
                  <option value="dhan">DhanHQ (Free, Deep History)</option>
                </select>
              </div>
              
              {(profile.dataSource || 'yfinance') !== 'yfinance' && (
                <div>
                  <label className="block text-sm font-bold text-gray-700 mb-2">
                    {(profile.dataSource || 'yfinance').charAt(0).toUpperCase() + (profile.dataSource || 'yfinance').slice(1)} API Key
                  </label>
                  <input 
                    type="password" 
                    value={profile.dataApiKey || ''}
                    onChange={(e) => handleChange('dataApiKey', e.target.value)}
                    placeholder="Enter Provider API Key..."
                    className="w-full border border-gray-300 rounded-lg p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
              )}
            </div>
          </div>

          <div className="p-6 flex flex-col md:flex-row justify-between items-center bg-gray-50">
            <div className="mb-4 md:mb-0">
              <h3 className="font-bold text-gray-800">Force Data Hoarder Sync</h3>
              <p className="text-sm text-gray-500 mt-1 max-w-xl">
                Force the background worker to fetch all missing 15m data for the Nifty 100 right now.
              </p>
            </div>
            <button 
              onClick={async () => {
                setIsHoarding(true);
                setHoardLogs([{ type: 'system', message: 'Connecting to Data Hoarder...' }]);
                try {
                  const response = await fetch('http://localhost:8000/api/hoarder/trigger', { 
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ data_source: profile.dataSource || 'yfinance', api_key: profile.dataApiKey || '' })
                  });
                  const reader = response.body.getReader();
                  const decoder = new TextDecoder('utf-8');
                  let buffer = '';
                  
                  while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    
                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop(); 
                    
                    for (const line of lines) {
                      if (!line.trim()) continue;
                      try {
                        const data = JSON.parse(line);
                        setHoardLogs(prev => {
                           const newLogs = [...prev, data];
                           // auto-scroll logic natively or keep small array
                           return newLogs;
                        });
                      } catch(e) {}
                    }
                  }
                } catch(err) {
                  setHoardLogs(prev => [...prev, { type: 'error', message: 'Sync connection failed.' }]);
                } finally {
                  setIsHoarding(false);
                }
              }}
              disabled={isHoarding}
              className={`font-bold py-3 px-6 rounded-lg shadow transition whitespace-nowrap text-white ${isHoarding ? 'bg-gray-500 cursor-not-allowed' : 'bg-indigo-600 hover:bg-indigo-700'}`}
            >
              {isHoarding ? 'Syncing in Progress...' : 'Force Sync Now'}
            </button>
          </div>
          
          {/* ONLY ONE TERMINAL, BELOW THE BUTTON */}
          {(isHoarding || hoardLogs.length > 0) && (
            <div className="bg-gray-900 border-t border-gray-800 p-4 h-64 overflow-y-auto font-mono text-sm flex flex-col space-y-1">
              {hoardLogs.map((log, i) => (
                <div key={i} className={
                  log.type === 'error' ? 'text-red-400' : 
                  log.type === 'success' ? 'text-green-400 font-bold' : 
                  log.type === 'system' ? 'text-blue-400' : 'text-gray-300'
                }>
                  <span className="text-gray-600 mr-2">[{new Date().toLocaleTimeString()}]</span>
                  {log.message}
                </div>
              ))}
            </div>
          )}

        </div>
      </div>
    </div>
  );
}

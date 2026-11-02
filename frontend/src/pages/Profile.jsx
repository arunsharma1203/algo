import React, { useState, useEffect } from 'react';
import { User, Key, Shield, HardDrive, DollarSign, Activity, DatabaseZap, Zap, CheckCircle, AlertCircle, AlertTriangle, ExternalLink, Globe, Copy, Check, Info, X, Clock, Timer, RefreshCw } from 'lucide-react';
import axios from 'axios';
import { API_BASE } from '../services/api';

export default function Profile() {
  const [telegram, setTelegram] = useState({ bot_token: '', chat_id: '' });
  const [upstox, setUpstox] = useState({
    api_key: '',
    api_secret: '',
    market_token: '',  // Read-only market data / analytics token
    algo_token: '',    // Read + Trade algo execution token
    redirect_uri: `${API_BASE}/settings/upstox/callback`
  });
  const [upstoxMarketStatus, setUpstoxMarketStatus] = useState(null);
  const [upstoxAlgoStatus, setUpstoxAlgoStatus] = useState(null);
  const [testingMarket, setTestingMarket] = useState(false);
  const [testingAlgo, setTestingAlgo] = useState(false);
  const [upstoxRateLimits, setUpstoxRateLimits] = useState(null);
  const [upstoxTokenStatus, setUpstoxTokenStatus] = useState(null);
  const isTokenExpired = upstoxTokenStatus?.status === 'TOKEN_EXPIRED' || (upstoxTokenStatus && upstoxTokenStatus.is_valid === false);

  // Real-time ticking timestamp for live countdown timers
  const [nowTs, setNowTs] = useState(Date.now());

  useEffect(() => {
    const timer = setInterval(() => {
      setNowTs(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  // Static IP helper modal state
  const [ipModalOpen, setIpModalOpen] = useState(false);
  const [ipData, setIpData] = useState({ primary_ip: '', secondary_ip: '' });
  const [ipLoading, setIpLoading] = useState(false);
  const [copiedField, setCopiedField] = useState(null);

  const [profile, setProfile] = useState({
    name: '',
    apiKey: '',
    simulationMode: true,
    defaultCapital: 100000,
    maxRiskPerTrade: 2.0,
    dataSource: 'upstox',
    dataApiKey: ''
  });
  
  const [savedMessage, setSavedMessage] = useState(false);
  const [hoardLogs, setHoardLogs] = useState([]);
  const [isHoarding, setIsHoarding] = useState(false);
  const [testStatus, setTestStatus] = useState('');

  const refreshUpstoxStatus = () => {
    fetch(`${API_BASE}/settings/upstox/token-status`)
      .then(res => res.json())
      .then(data => setUpstoxTokenStatus(data))
      .catch(e => console.error("Upstox token status fetch error:", e));
  };

  const refreshUpstoxRateLimits = () => {
    fetch(`${API_BASE}/settings/upstox`)
      .then(res => res.json())
      .then(data => {
        if (data.rate_limits) {
          setUpstoxRateLimits(data.rate_limits);
        }
      })
      .catch(e => console.error("Upstox rate limits fetch error:", e));
  };

  // 5-second polling interval to keep rate limits telemetry fresh
  useEffect(() => {
    const pollInterval = setInterval(() => {
      refreshUpstoxRateLimits();
      refreshUpstoxStatus();
    }, 5000);
    return () => clearInterval(pollInterval);
  }, []);

  // Helper: Format seconds to HH:MM:SS or Dd HH:MM:SS
  const formatSecondsToCountdown = (totalSec) => {
    if (totalSec === null || totalSec === undefined || isNaN(totalSec)) return '--:--:--';
    if (totalSec <= 0) return '00:00:00';
    const days = Math.floor(totalSec / 86400);
    const hours = Math.floor((totalSec % 86400) / 3600);
    const mins = Math.floor((totalSec % 3600) / 60);
    const secs = totalSec % 60;

    if (days > 0) {
      return `${days}d ${String(hours).padStart(2, '0')}h ${String(mins).padStart(2, '0')}m ${String(secs).padStart(2, '0')}s`;
    }
    return `${String(hours).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  };

  // Helper: Calculate remaining time to token refresh/expiry
  const getTokenRefreshCountdown = () => {
    if (isTokenExpired) {
      return { formatted: '00:00:00', isExpired: true, label: 'Token Expired (Re-auth needed)' };
    }
    if (upstoxTokenStatus?.expires_at) {
      try {
        const expDate = new Date(upstoxTokenStatus.expires_at.replace(' ', 'T'));
        const diffSec = Math.floor((expDate.getTime() - nowTs) / 1000);
        if (diffSec <= 0) {
          return { formatted: '00:00:00', isExpired: true, label: 'Token Expired (Re-auth needed)' };
        }
        return {
          formatted: formatSecondsToCountdown(diffSec),
          isExpired: false,
          label: `Expires: ${upstoxTokenStatus.expires_at}`
        };
      } catch (e) {}
    }
    // Fallback: Upstox daily session reset cutoff (03:30:00 IST every morning)
    const now = new Date(nowTs);
    const reset = new Date(nowTs);
    reset.setHours(3, 30, 0, 0);
    if (now.getTime() >= reset.getTime()) {
      reset.setDate(reset.getDate() + 1);
    }
    const diffSec = Math.floor((reset.getTime() - now.getTime()) / 1000);
    return {
      formatted: formatSecondsToCountdown(diffSec),
      isExpired: false,
      label: 'Daily 03:30 AM IST Cutoff'
    };
  };

  // Helper: Calculate remaining time to 30-min rate limit sliding window replenishment
  const getQuotaWindowCountdown = () => {
    if (upstoxRateLimits?.circuit_open) {
      const rem = upstoxRateLimits.circuit_cooldown_remaining_sec || 0;
      return {
        formatted: `${rem}s`,
        isCooldown: true,
        label: 'Circuit Breaker Cooldown'
      };
    }
    const remSec = upstoxRateLimits?.window_reset_remaining_sec;
    if (remSec && remSec > 0) {
      const mins = Math.floor(remSec / 60);
      const secs = remSec % 60;
      return {
        formatted: `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`,
        isCooldown: false,
        label: 'Rolling slot replenishment'
      };
    }
    return {
      formatted: 'FULL CAPACITY',
      isCooldown: false,
      label: 'All 1,600 slots available'
    };
  };

  const tokenCountdown = getTokenRefreshCountdown();
  const quotaCountdown = getQuotaWindowCountdown();

  useEffect(() => {
    // 1. Fetch Telegram Config from backend
    fetch(`${API_BASE}/settings/telegram`)
      .then(res => res.json())
      .then(data => {
        if (data.bot_token || data.chat_id) {
          setTelegram({ bot_token: data.bot_token || '', chat_id: data.chat_id || '' });
        }
      })
      .catch(e => console.error("Telegram fetch error:", e));

    // 2. Fetch Upstox Settings & Token from backend
    fetch(`${API_BASE}/settings/upstox`)
      .then(res => res.json())
      .then(data => {
        setUpstox(prev => ({
          ...prev,
          api_key: data.api_key || '',
          market_token: data.market_token || '',
          algo_token: data.algo_token || '',
          redirect_uri: data.redirect_uri || prev.redirect_uri
        }));
        if (data.rate_limits) {
          setUpstoxRateLimits(data.rate_limits);
        }
      })
      .catch(e => console.error("Upstox fetch error:", e));

    // 2b. Fetch Upstox Token Status
    refreshUpstoxStatus();

    // 3. Fetch Active Data Source
    fetch(`${API_BASE}/settings/datasource`)
      .then(res => res.json())
      .then(data => {
        if (data.source) {
          setProfile(p => ({ ...p, dataSource: data.source }));
        }
      })
      .catch(e => console.error("DataSource fetch error:", e));

    // 4. Fetch Global Simulation Mode from backend
    fetch(`${API_BASE}/settings/simulation`)
      .then(res => res.json())
      .then(data => {
        if (typeof data.simulation_mode === 'boolean') {
          setProfile(p => ({ ...p, simulationMode: data.simulation_mode }));
        }
      })
      .catch(e => console.error("Simulation mode fetch error:", e));

    // 5. Load from localStorage for client profile preferences
    const saved = localStorage.getItem('swing_profile');
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        setProfile(p => ({ ...p, ...parsed }));
      } catch(e) {}
    } else {
      const oldKey = localStorage.getItem('indmoney_api_token');
      if (oldKey) {
        setProfile(p => ({ ...p, apiKey: oldKey }));
      }
    }
  }, []);

  const handleChange = (field, value) => {
    setProfile(p => ({ ...p, [field]: value }));
  };

  const handleSimulationChange = async (isSimulation) => {
    if (!isSimulation) {
      const confirmLive = window.confirm(
        "⚠️ CAUTION: Switching to LIVE REAL-MONEY TRADING.\n\nOrders placed will route directly to your broker and commit live capital.\n\nAre you sure you want to turn Simulation Mode OFF?"
      );
      if (!confirmLive) return;
    }
    setProfile(p => ({ ...p, simulationMode: isSimulation }));
    
    // Persist to localStorage
    const saved = localStorage.getItem('swing_profile');
    const existing = saved ? JSON.parse(saved) : {};
    localStorage.setItem('swing_profile', JSON.stringify({ ...existing, simulationMode: isSimulation }));
    
    // Persist to backend database
    try {
      await fetch(`${API_BASE}/settings/simulation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ simulation_mode: isSimulation })
      });
    } catch (e) {
      console.error("Failed saving simulation setting:", e);
    }
  };

  const handleDataSourceChange = async (newSource) => {
    setProfile(p => ({ ...p, dataSource: newSource }));
    try {
      await fetch(`${API_BASE}/settings/datasource`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: newSource })
      });
    } catch (e) {
      console.error("Failed saving datasource setting:", e);
    }
  };

  const handleTestTelegram = async () => {
    setTestStatus('Testing...');
    try {
      const response = await fetch(`${API_BASE}/settings/telegram/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(telegram)
      });
      const data = await response.json();
      if (data.status === 'success') {
        setTestStatus('✅ Success! Check your Telegram.');
      } else {
        setTestStatus('❌ Failed: ' + data.message);
      }
    } catch (e) {
      setTestStatus('❌ Error connecting to server.');
    }
    setTimeout(() => setTestStatus(''), 5000);
  };

  const handleTestUpstoxMarket = async () => {
    setTestingMarket(true);
    setUpstoxMarketStatus(null);
    try {
      // First save the upstox configuration
      await fetch(`${API_BASE}/settings/upstox`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(upstox)
      });

      const response = await fetch(`${API_BASE}/settings/upstox/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...upstox, test_type: 'market' })
      });
      const data = await response.json();
      setUpstoxMarketStatus(data);
      
      if (data.status === 'success') {
        // Automatically switch data source to Upstox
        setProfile(p => ({ ...p, dataSource: 'upstox' }));
      }
    } catch (e) {
      setUpstoxMarketStatus({ status: 'error', message: 'Connection to backend failed.' });
    } finally {
      refreshUpstoxStatus();
      setTestingMarket(false);
    }
  };

  const handleSaveAndReconnectMarketToken = async () => {
    setTestingMarket(true);
    setUpstoxMarketStatus({ status: 'info', message: 'Saving and validating fresh market data token...' });
    try {
      await fetch(`${API_BASE}/settings/upstox`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(upstox)
      });
      await fetch(`${API_BASE}/settings/datasource`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: 'upstox' })
      });
      const response = await fetch(`${API_BASE}/settings/upstox/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...upstox, test_type: 'market' })
      });
      const data = await response.json();
      setUpstoxMarketStatus(data);
      if (data.status === 'success') {
        setProfile(p => ({ ...p, dataSource: 'upstox' }));
      }
    } catch (e) {
      setUpstoxMarketStatus({ status: 'error', message: 'Connection to backend failed.' });
    } finally {
      refreshUpstoxStatus();
      setTestingMarket(false);
    }
  };

  const handleTestUpstoxAlgo = async () => {
    setTestingAlgo(true);
    setUpstoxAlgoStatus(null);
    try {
      // Save configuration
      await fetch(`${API_BASE}/settings/upstox`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(upstox)
      });

      const response = await fetch(`${API_BASE}/settings/upstox/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...upstox, test_type: 'algo' })
      });
      const data = await response.json();
      setUpstoxAlgoStatus(data);
    } catch (e) {
      setUpstoxAlgoStatus({ status: 'error', message: 'Connection to backend failed.' });
    } finally {
      setTestingAlgo(false);
    }
  };

  const handleOpenIpModal = async () => {
    setIpModalOpen(true);
    setIpLoading(true);
    try {
      const res = await fetch(`${API_BASE}/settings/my-ip`);
      if (res.ok) {
        const data = await res.json();
        setIpData(data);
      }
    } catch (e) {
      console.error("IP detect error:", e);
    } finally {
      setIpLoading(false);
    }
  };

  const copyToClipboard = (text, field) => {
    if (!text) return;
    navigator.clipboard.writeText(text);
    setCopiedField(field);
    setTimeout(() => setCopiedField(null), 2500);
  };

  const handleGenerateAuthUrl = async () => {
    // Save current API key first
    await fetch(`${API_BASE}/settings/upstox`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(upstox)
    });

    const res = await fetch(`${API_BASE}/settings/upstox/auth-url`);
    const data = await res.json();
    if (data.status === 'success' && data.auth_url) {
      window.open(data.auth_url, '_blank');
    } else {
      alert(data.message || "Please enter your Upstox API Key (Client ID) first.");
    }
  };

  const handleSave = async () => {
    localStorage.setItem('swing_profile', JSON.stringify(profile));
    localStorage.setItem('indmoney_api_token', profile.apiKey);

    // Save Simulation Mode
    await fetch(`${API_BASE}/settings/simulation`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ simulation_mode: profile.simulationMode })
    }).catch(e => console.error(e));

    // Save Telegram config to backend
    if (telegram.bot_token || telegram.chat_id) {
      await fetch(`${API_BASE}/settings/telegram`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(telegram)
      }).catch(e => console.error(e));
    }

    // Save Upstox config to backend
    await fetch(`${API_BASE}/settings/upstox`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(upstox)
    }).catch(e => console.error(e));

    // Save Data Source setting to backend
    await fetch(`${API_BASE}/settings/datasource`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: profile.dataSource || 'upstox' })
    }).catch(e => console.error(e));
    
    refreshUpstoxStatus();
    setSavedMessage(true);
    setTimeout(() => setSavedMessage(false), 3000);
  };

  return (
    <div className="max-w-4xl mx-auto pb-10 max-w-full">
      <div className="mb-6 sm:mb-8 flex flex-col sm:flex-row justify-between items-start sm:items-end gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-black text-gray-900 tracking-tight">System Settings &amp; Profile</h1>
          <p className="text-gray-500 mt-2 font-medium text-xs sm:text-sm">Manage your execution environment, API keys, data providers, and risk parameters.</p>
        </div>
        
        <button 
          onClick={handleSave}
          className="w-full sm:w-auto bg-indigo-600 hover:bg-indigo-700 text-white font-bold py-3 px-6 sm:px-8 rounded-lg shadow-md transition transform hover:scale-105 cursor-pointer text-sm shrink-0"
        >
          {savedMessage ? '✅ Settings Saved!' : 'Save All Changes'}
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

            <div className="pt-4 border-t border-gray-100 mt-4">
              <label className="block text-sm font-bold text-gray-700 mb-2">Max Risk Per Trade (%)</label>
              <div className="relative">
                <div className="absolute inset-y-0 right-0 pr-4 flex items-center pointer-events-none">
                  <span className="text-gray-500 font-bold">%</span>
                </div>
                <input 
                  type="number" 
                  step="0.1"
                  value={profile.maxRiskPerTrade || 2.0}
                  onChange={(e) => handleChange('maxRiskPerTrade', parseFloat(e.target.value))}
                  className="w-full border border-gray-300 rounded-lg py-3 pl-4 pr-10 focus:outline-none focus:ring-2 focus:ring-indigo-500 font-mono"
                />
              </div>
              <p className="text-xs text-gray-500 mt-2">Calculates exact position sizing based on your Stop Loss so you never lose more than this percentage of your capital.</p>
            </div>
          </div>
        </div>

        {/* Execution Mode & Safety Safeguards */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
          <div className={`px-6 py-4 border-b flex items-center justify-between ${profile.simulationMode ? 'bg-emerald-50 border-emerald-200' : 'bg-amber-50 border-amber-200'}`}>
            <div className="flex items-center">
              <Shield size={20} className={`mr-2 ${profile.simulationMode ? 'text-emerald-600' : 'text-amber-600'}`} />
              <h2 className="font-bold text-gray-800">Order Execution &amp; Safety Safeguards</h2>
            </div>
            <span className={`text-xs font-bold px-2.5 py-1 rounded-full ${profile.simulationMode ? 'bg-emerald-100 text-emerald-800 border border-emerald-200' : 'bg-rose-100 text-rose-800 border border-rose-200 animate-pulse'}`}>
              {profile.simulationMode ? '🛡️ SIMULATION (PAPER)' : '⚠️ LIVE (REAL MONEY)'}
            </span>
          </div>

          <div className="p-6">
            <p className="text-sm text-gray-600 mb-4">
              Control the execution safety layer. When Simulation Mode is active, 1-Click execution is completely risk-free and paper-traded.
            </p>

            <div className="space-y-3">
              {/* Option 1: Simulation Mode */}
              <div 
                onClick={() => handleSimulationChange(true)}
                className={`p-3.5 rounded-xl border-2 cursor-pointer transition flex items-start space-x-3 ${profile.simulationMode ? 'border-emerald-500 bg-emerald-50/50' : 'border-gray-200 hover:border-gray-300'}`}
              >
                <input 
                  type="radio" 
                  name="simulationMode" 
                  checked={profile.simulationMode === true} 
                  onChange={() => handleSimulationChange(true)}
                  className="mt-1 text-emerald-600 focus:ring-emerald-500"
                />
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="font-bold text-sm text-gray-900">Simulation Mode (Paper Trading)</span>
                    <span className="text-[10px] font-bold bg-emerald-100 text-emerald-800 px-2 py-0.5 rounded">RECOMMENDED</span>
                  </div>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Zero real money risk. Orders, stops, and dynamic targets are logged and tracked with virtual capital.
                  </p>
                </div>
              </div>

              {/* Option 2: Live Real-Money Trading */}
              <div 
                onClick={() => handleSimulationChange(false)}
                className={`p-3.5 rounded-xl border-2 cursor-pointer transition flex items-start space-x-3 ${!profile.simulationMode ? 'border-amber-500 bg-amber-50/50' : 'border-gray-200 hover:border-gray-300'}`}
              >
                <input 
                  type="radio" 
                  name="simulationMode" 
                  checked={profile.simulationMode === false} 
                  onChange={() => handleSimulationChange(false)}
                  className="mt-1 text-amber-600 focus:ring-amber-500"
                />
                <div>
                  <div className="flex items-center space-x-2">
                    <span className="font-bold text-sm text-gray-900">Live Broker Execution (Real Money)</span>
                    <span className="text-[10px] font-bold bg-amber-100 text-amber-800 px-2 py-0.5 rounded">REAL CAPITAL</span>
                  </div>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Direct order routing to your broker. Orders will commit live funds from your account.
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Autonomous Bot Telegram Integration */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
          <div className="bg-blue-50 px-6 py-4 border-b border-blue-200 flex items-center justify-between">
            <div className="flex items-center">
              <svg className="w-5 h-5 text-blue-500 mr-2" fill="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.892-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>
              <h2 className="font-bold text-gray-800">Telegram Bot Integration</h2>
            </div>
            <span className="bg-blue-100 text-blue-800 text-xs font-bold px-2 py-1 rounded">PUSH ALERTS</span>
          </div>
          
          <div className="p-6">
            <p className="text-sm text-gray-600 mb-6">
              Connect the Autonomous AI to your Telegram. When the bot finds a new trade or triggers an Early Exit, it will send a push notification directly to your phone.
            </p>
            
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-bold text-gray-700 mb-2">Telegram Bot Token</label>
                <input 
                  type="password" 
                  value={telegram.bot_token}
                  onChange={(e) => setTelegram({...telegram, bot_token: e.target.value})}
                  className="w-full border border-gray-300 rounded-lg py-3 px-4 focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono text-sm"
                  placeholder="1234567890:ABCdefGHIjklMNOpqrs..."
                />
                <p className="text-xs text-gray-500 mt-1">Get this from @BotFather on Telegram.</p>
              </div>
              
              <div>
                <div className="flex justify-between items-center mb-2">
                  <label className="block text-sm font-bold text-gray-700">Your Chat ID</label>
                  <button 
                    onClick={handleTestTelegram}
                    className="text-xs font-bold text-blue-600 bg-blue-100 hover:bg-blue-200 px-2 py-1 rounded transition cursor-pointer"
                  >
                    Test Connection
                  </button>
                </div>
                <input 
                  type="text" 
                  value={telegram.chat_id}
                  onChange={(e) => setTelegram({...telegram, chat_id: e.target.value})}
                  className="w-full border border-gray-300 rounded-lg py-3 px-4 focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono text-sm"
                  placeholder="123456789"
                />
                <p className="text-xs text-gray-500 mt-1">Get this from @userinfobot on Telegram.</p>
                {testStatus && (
                  <p className={`text-xs font-bold mt-2 ${testStatus.includes('✅') ? 'text-green-600' : testStatus.includes('❌') ? 'text-red-600' : 'text-blue-600'}`}>
                    {testStatus}
                  </p>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Real-Time Market Data Source Switcher & Upstox Integration */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden md:col-span-2">
          <div className="bg-gradient-to-r from-purple-900 to-indigo-900 text-white px-6 py-4 flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Zap size={22} className="text-yellow-400" />
              <h2 className="font-black text-lg tracking-tight">Market Data Provider &amp; Upstox Real-Time Feed</h2>
            </div>
            <span className={`text-xs font-bold px-3 py-1 rounded-full ${isTokenExpired ? 'bg-rose-600 text-white animate-pulse' : 'bg-emerald-500 text-white'}`}>
              {isTokenExpired ? '🔴 TOKEN EXPIRED / DEFENSIVE HALT' : '🟢 UPSTOX REAL-TIME (ACTIVE)'}
            </span>
          </div>

          <div className="p-6">
            {/* Prominent Red Banner if Upstox token is expired or revoked */}
            {isTokenExpired && (
              <div className="mb-6 bg-rose-50 border-2 border-rose-500 rounded-xl p-4 text-rose-900 flex items-start space-x-3 shadow-sm animate-fade-in">
                <AlertTriangle className="text-rose-600 shrink-0 mt-0.5" size={24} />
                <div className="flex-1">
                  <h3 className="font-black text-sm uppercase tracking-wide text-rose-800">
                    ⚠️ Action Required: Upstox Access Token Expired or Revoked
                  </h3>
                  <p className="text-xs text-rose-700 mt-1">
                    The Upstox market data token is invalid or expired. To safeguard capital from unmonitored execution, 
                    <strong> emergency defensive liquidation was triggered across active positions</strong> and scanning has halted.
                    Yahoo Finance fallback is permanently disabled across Qlib, Legacy, and Consensus pipelines.
                  </p>
                  <p className="text-xs font-bold text-rose-900 mt-2">
                    👉 Please enter your fresh Upstox Market Data Token below and click "Save & Reconnect Market Token" to resume live operations.
                  </p>
                </div>
              </div>
            )}

            <div className="mb-6">
              <label className="block text-sm font-bold text-gray-800 mb-2">Active Market Data Engine</label>
              <div className="p-4 rounded-xl border-2 border-purple-600 bg-purple-50/50 shadow-sm flex items-start space-x-3">
                <div className="w-3 h-3 rounded-full bg-purple-600 mt-1 shrink-0" />
                <div className="flex-1">
                  <div className="flex items-center space-x-2">
                    <span className="font-black text-gray-900">Pure Upstox API v2 Feed</span>
                    <span className="text-[10px] font-bold bg-emerald-100 text-emerald-800 px-2 py-0.5 rounded">SOLE SYSTEM PROVIDER</span>
                  </div>
                  <p className="text-xs text-gray-600 mt-1">
                    Direct exchange connection powering Qlib, Legacy, and Consensus pipelines. Delivers <strong>sub-second live ticks</strong> and real-time 15m OHLCV candles.
                    Yahoo Finance has been completely decommissioned. If Upstox tokens expire or fail, the platform strictly fails closed defensively.
                  </p>
                </div>
              </div>
            </div>

            {/* Upstox API Credentials Configuration Box */}
            <div className="bg-slate-50 border border-slate-200 rounded-xl p-6">
              <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 mb-4">
                <div>
                  <h3 className="font-bold text-gray-900 flex items-center">
                    <Key size={16} className="text-purple-600 mr-2" /> Upstox API v2 Credentials
                  </h3>
                  <p className="text-xs text-gray-500 mt-0.5">Enter your Upstox Developer App credentials and daily access tokens.</p>
                </div>
                <div className="flex items-center space-x-2">
                  <button
                    type="button"
                    onClick={handleOpenIpModal}
                    className="text-xs font-bold text-indigo-700 bg-indigo-100 hover:bg-indigo-200 px-3 py-1.5 rounded-lg flex items-center transition cursor-pointer shadow-2xs"
                  >
                    <Globe size={13} className="mr-1.5 text-indigo-600" /> My Static / Whitelist IP
                  </button>
                  <button
                    type="button"
                    onClick={handleGenerateAuthUrl}
                    className="text-xs font-bold text-purple-700 bg-purple-100 hover:bg-purple-200 px-3 py-1.5 rounded-lg flex items-center transition cursor-pointer shadow-2xs"
                  >
                    <ExternalLink size={12} className="mr-1" /> Generate Login URL
                  </button>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-xs font-bold text-gray-700 mb-1">API Key (Client ID)</label>
                  <input 
                    type="text" 
                    value={upstox.api_key}
                    onChange={(e) => setUpstox({...upstox, api_key: e.target.value})}
                    placeholder="Enter Upstox API Key..."
                    className="w-full border border-gray-300 rounded-lg p-2.5 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-purple-500 bg-white"
                  />
                </div>

                <div>
                  <label className="block text-xs font-bold text-gray-700 mb-1">API Secret</label>
                  <input 
                    type="password" 
                    value={upstox.api_secret}
                    onChange={(e) => setUpstox({...upstox, api_secret: e.target.value})}
                    placeholder="Enter Upstox API Secret..."
                    className="w-full border border-gray-300 rounded-lg p-2.5 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-purple-500 bg-white"
                  />
                </div>

                {/* Token 1: Market Data / Read-Only Analytics */}
                <div className={`md:col-span-2 rounded-xl p-4 shadow-2xs transition ${
                  isTokenExpired 
                    ? 'bg-rose-50/50 border-2 border-rose-500 ring-2 ring-rose-200' 
                    : 'bg-white border border-purple-100'
                }`}>
                  <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 mb-1.5">
                    <div>
                      <span className="text-xs font-bold text-gray-800 flex items-center">
                        <span className={`w-2 h-2 rounded-full mr-2 ${isTokenExpired ? 'bg-rose-600 animate-ping' : 'bg-emerald-500'}`}></span>
                        1. Market Data &amp; Analytics Token (Read-Only)
                      </span>
                      <span className="text-[11px] text-gray-500 block">Powers sub-second live market ticks, OHLCV candle streams, and AI scanner feeds.</span>
                    </div>
                    <div className="flex items-center space-x-2">
                      <button
                        type="button"
                        onClick={handleSaveAndReconnectMarketToken}
                        disabled={testingMarket}
                        className={`text-xs font-bold px-3 py-1.5 rounded-lg transition cursor-pointer shadow-sm ${
                          isTokenExpired 
                            ? 'bg-rose-600 hover:bg-rose-700 text-white animate-pulse' 
                            : 'bg-indigo-600 hover:bg-indigo-700 text-white'
                        }`}
                      >
                        {testingMarket ? 'Saving & Verifying...' : isTokenExpired ? 'Save & Reconnect Market Token' : 'Save & Verify'}
                      </button>
                      <button
                        type="button"
                        onClick={handleTestUpstoxMarket}
                        disabled={testingMarket}
                        className="text-xs font-bold bg-purple-600 hover:bg-purple-700 text-white px-3 py-1.5 rounded-lg transition cursor-pointer"
                      >
                        {testingMarket ? 'Verifying...' : 'Test Market Feed'}
                      </button>
                    </div>
                  </div>
                  <input 
                    type="password" 
                    value={upstox.market_token}
                    onChange={(e) => setUpstox({...upstox, market_token: e.target.value})}
                    placeholder="Paste Upstox Market Data (Read-Only) Access Token..."
                    className={`w-full border rounded-lg p-2.5 font-mono text-xs focus:outline-none focus:ring-2 bg-white mt-1 ${
                      isTokenExpired 
                        ? 'border-rose-400 focus:ring-rose-500 text-rose-900 font-semibold' 
                        : 'border-gray-300 focus:ring-purple-500'
                    }`}
                  />
                  
                  {upstoxMarketStatus && (
                    <div className={`mt-2.5 p-2.5 rounded-lg text-xs font-semibold flex items-center ${upstoxMarketStatus.status === 'success' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'}`}>
                      {upstoxMarketStatus.status === 'success' ? (
                        <CheckCircle size={15} className="mr-2 text-emerald-600 shrink-0" />
                      ) : (
                        <AlertCircle size={15} className="mr-2 text-rose-600 shrink-0" />
                      )}
                      <span>{upstoxMarketStatus.message}</span>
                    </div>
                  )}
                </div>

                {/* Token 2: Algo Trading / Read + Trade Execution */}
                <div className="md:col-span-2 bg-white border border-amber-100 rounded-xl p-4 shadow-2xs">
                  <div className="flex justify-between items-center mb-1.5">
                    <div>
                      <span className="text-xs font-bold text-gray-800 flex items-center">
                        <span className="w-2 h-2 rounded-full bg-amber-500 mr-2"></span>
                        2. Algo Trading &amp; Execution Token (Read + Trade)
                      </span>
                      <span className="text-[11px] text-gray-500 block">Dedicated token for 1-Click order execution with buy/sell routing permissions.</span>
                    </div>
                    <button
                      type="button"
                      onClick={handleTestUpstoxAlgo}
                      disabled={testingAlgo}
                      className="text-xs font-bold bg-amber-600 hover:bg-amber-700 text-white px-3 py-1.5 rounded-lg transition cursor-pointer"
                    >
                      {testingAlgo ? 'Verifying...' : 'Test Trade Permissions'}
                    </button>
                  </div>
                  <input 
                    type="password" 
                    value={upstox.algo_token}
                    onChange={(e) => setUpstox({...upstox, algo_token: e.target.value})}
                    placeholder="Paste Upstox Algo Trading (Read+Trade) Access Token..."
                    className="w-full border border-gray-300 rounded-lg p-2.5 font-mono text-xs focus:outline-none focus:ring-2 focus:ring-amber-500 bg-white mt-1"
                  />
                  
                  {upstoxAlgoStatus && (
                    <div className={`mt-2.5 p-2.5 rounded-lg text-xs font-semibold flex items-center ${upstoxAlgoStatus.status === 'success' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'}`}>
                      {upstoxAlgoStatus.status === 'success' ? (
                        <CheckCircle size={15} className="mr-2 text-emerald-600 shrink-0" />
                      ) : (
                        <AlertCircle size={15} className="mr-2 text-rose-600 shrink-0" />
                      )}
                      <span>{upstoxAlgoStatus.message}</span>
                    </div>
                  )}
                </div>

                {/* Upstox Rate Limit Guardrails & Protection Telemetry */}
                <div className={`md:col-span-2 bg-gradient-to-br ${upstoxRateLimits?.circuit_open ? 'from-amber-950/40 via-slate-900 to-rose-950/30 border-rose-600/50' : 'from-slate-900 via-slate-850 to-indigo-950 border-slate-700/80'} border rounded-xl p-4 text-white shadow-md`}>
                  <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 mb-3">
                    <div className="flex items-center space-x-2">
                      <div className={`w-2.5 h-2.5 rounded-full ${upstoxRateLimits?.circuit_open ? 'bg-amber-400 animate-ping' : 'bg-emerald-400 animate-pulse'}`}></div>
                      <span className="text-xs font-bold text-slate-100 tracking-wide">
                        Upstox Rate Limiter Active (Zero Token Blocking Protection)
                      </span>
                    </div>
                    {upstoxRateLimits?.circuit_open ? (
                      <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-rose-950/90 text-rose-300 border border-rose-600/70 animate-pulse">
                        CIRCUIT OPEN ({upstoxRateLimits.circuit_cooldown_remaining_sec}s COOLDOWN)
                      </span>
                    ) : (
                      <span className="text-[10px] font-mono font-semibold px-2 py-0.5 rounded bg-emerald-950/80 text-emerald-300 border border-emerald-700/50">
                        STATUS: {upstoxRateLimits?.status || 'NORMAL'}
                      </span>
                    )}
                  </div>

                  {upstoxRateLimits?.circuit_open && (
                    <div className="mb-3 p-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-[11px] text-amber-200 flex items-center justify-between">
                      <span>Rate limit cooldown active. Network calls fail closed defensively with local SQLite cache to safeguard trading integrity.</span>
                      <span className="font-mono font-bold text-amber-300 ml-2 whitespace-nowrap">{upstoxRateLimits.circuit_cooldown_remaining_sec}s left</span>
                    </div>
                  )}

                  <p className="text-[11px] text-slate-300 mb-3">
                    Enforces official Upstox API request ceilings with 150ms leaky-bucket inter-request pacing, bulk quote batching (up to 500 symbols/call), and automated 429 circuit breaker.
                  </p>

                  {/* Real-Time Live Timers: Token Refresh & Quota Window Reset */}
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 mb-3">
                    {/* Timer 1: Upstox Access Token Expiration / Daily Refresh Countdown */}
                    <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3 flex items-center justify-between shadow-xs">
                      <div className="flex items-center space-x-2.5">
                        <div className={`p-2 rounded-lg ${tokenCountdown.isExpired ? 'bg-rose-950/80 text-rose-400 border border-rose-700/60' : 'bg-cyan-950/80 text-cyan-400 border border-cyan-700/60'}`}>
                          <Clock size={16} className={tokenCountdown.isExpired ? "animate-pulse" : ""} />
                        </div>
                        <div>
                          <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400">
                            Token Refresh Timer
                          </div>
                          <div className="text-[11px] font-medium text-slate-300 truncate max-w-[170px]" title={tokenCountdown.label}>
                            {tokenCountdown.label}
                          </div>
                        </div>
                      </div>
                      <div className="text-right pl-2">
                        <div className={`text-sm font-mono font-black tracking-tight ${tokenCountdown.isExpired ? 'text-rose-400 animate-pulse' : 'text-cyan-300'}`}>
                          {tokenCountdown.formatted}
                        </div>
                        <div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">
                          {tokenCountdown.isExpired ? '🔴 EXPIRED' : '🟢 ACTIVE TOKEN'}
                        </div>
                      </div>
                    </div>

                    {/* Timer 2: 30-Min Quota Sliding Window Reset */}
                    <div className="bg-slate-800/90 border border-slate-700/80 rounded-lg p-3 flex items-center justify-between shadow-xs">
                      <div className="flex items-center space-x-2.5">
                        <div className={`p-2 rounded-lg ${quotaCountdown.isCooldown ? 'bg-amber-950/80 text-amber-400 border border-amber-700/60' : 'bg-purple-950/80 text-purple-400 border border-purple-700/60'}`}>
                          <Timer size={16} />
                        </div>
                        <div>
                          <div className="text-[10px] uppercase font-bold tracking-wider text-slate-400">
                            {quotaCountdown.isCooldown ? "Circuit Cooldown" : "30-Min Quota Reset"}
                          </div>
                          <div className="text-[11px] font-medium text-slate-300">
                            {quotaCountdown.label}
                          </div>
                        </div>
                      </div>
                      <div className="text-right pl-2">
                        <div className={`text-sm font-mono font-black tracking-tight ${quotaCountdown.isCooldown ? 'text-amber-400 animate-pulse' : 'text-purple-300'}`}>
                          {quotaCountdown.formatted}
                        </div>
                        <div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">
                          {quotaCountdown.isCooldown ? '⚡ COOLDOWN' : '⏱️ SLOTS RESET'}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="grid grid-cols-3 gap-2 text-center">
                    <div className="bg-slate-800/80 border border-slate-700 rounded-lg p-2">
                      <div className="text-[10px] text-slate-400 font-semibold">Per Second</div>
                      <div className="text-sm font-mono font-bold text-emerald-400 mt-0.5">
                        {upstoxRateLimits?.current_1s ?? 0} / {upstoxRateLimits?.max_1s ?? 10}
                      </div>
                      <div className="text-[9px] text-slate-500">Auto Micro-Paced</div>
                    </div>

                    <div className="bg-slate-800/80 border border-slate-700 rounded-lg p-2">
                      <div className="text-[10px] text-slate-400 font-semibold">Per Minute</div>
                      <div className="text-sm font-mono font-bold text-indigo-300 mt-0.5">
                        {upstoxRateLimits?.current_1m ?? 0} / {upstoxRateLimits?.max_1m ?? 500}
                      </div>
                      <div className="text-[9px] text-slate-500">Sliding Window</div>
                    </div>

                    <div className="bg-slate-800/80 border border-slate-700 rounded-lg p-2">
                      <div className="text-[10px] text-slate-400 font-semibold">Per 30 Minutes</div>
                      <div className="text-sm font-mono font-bold text-purple-300 mt-0.5">
                        {upstoxRateLimits?.current_30m ?? 0} / {upstoxRateLimits?.max_30m ?? 1600}
                      </div>
                      <div className="text-[9px] text-slate-500">
                        {upstoxRateLimits?.window_reset_remaining_sec > 0 
                          ? `Resets in ${formatSecondsToCountdown(upstoxRateLimits.window_reset_remaining_sec)}`
                          : "Hard Failover Cap"}
                      </div>
                    </div>
                  </div>

                  <div className="mt-2.5 pt-2 border-t border-slate-800/80 flex items-center justify-between text-[10px] text-slate-400">
                    <span>Total Session Calls: <strong className="text-slate-200">{upstoxRateLimits?.total_requests ?? 0}</strong></span>
                    <span>Paced: <strong className="text-amber-300">{upstoxRateLimits?.throttled_count ?? 0}</strong></span>
                    <span>Fail-Closed Protection: <strong className="text-emerald-400">{upstoxRateLimits?.rejected_count ?? 0}</strong></span>
                  </div>
                </div>

              </div>
            </div>

          </div>
        </div>

      </div>

      {/* Upstox IP Whitelist Helper Modal */}
      {ipModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-xs p-4 animate-fade-in">
          <div className="bg-slate-900 border border-slate-700 text-white rounded-2xl shadow-2xl max-w-md w-full overflow-hidden flex flex-col">
            {/* Header */}
            <div className="px-6 py-4 border-b border-slate-800 flex justify-between items-center bg-gradient-to-r from-slate-900 via-indigo-950/60 to-slate-900">
              <div className="flex items-center space-x-2.5">
                <div className="w-8 h-8 rounded-lg bg-indigo-500/20 border border-indigo-500/30 flex items-center justify-center text-indigo-400">
                  <Globe size={18} />
                </div>
                <div>
                  <h3 className="font-bold text-sm text-white">Upstox Static IP Helper</h3>
                  <p className="text-[10px] text-slate-400">Network IPs for Upstox App whitelist configuration</p>
                </div>
              </div>
              <button onClick={() => setIpModalOpen(false)} className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition cursor-pointer">
                <X size={18} />
              </button>
            </div>

            {/* Body */}
            <div className="p-6 space-y-4 font-sans text-xs">
              <p className="text-slate-300 text-xs leading-relaxed">
                When creating your Upstox Algo App, Upstox asks for <strong>Primary Static IP</strong> and <strong>Secondary IP</strong>. Click copy to grab your current values:
              </p>

              {/* Primary IP Card */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-3.5 space-y-1.5">
                <div className="flex justify-between items-center">
                  <span className="font-bold text-slate-300 text-xs flex items-center">
                    <span className="w-2 h-2 rounded-full bg-emerald-400 mr-2"></span>
                    Primary Static IP (Your Public IP)
                  </span>
                  <span className="text-[10px] font-bold bg-emerald-950 text-emerald-300 px-2 py-0.5 rounded border border-emerald-800">
                    REQUIRED
                  </span>
                </div>
                
                <div className="flex items-center justify-between bg-slate-900 border border-slate-700/80 rounded-lg px-3 py-2">
                  <span className="font-mono text-sm font-bold text-white tracking-wider">
                    {ipLoading ? 'Detecting...' : (ipData.primary_ip || 'Unavailable')}
                  </span>
                  <button
                    type="button"
                    onClick={() => copyToClipboard(ipData.primary_ip, 'primary')}
                    className={`text-[11px] font-bold px-2.5 py-1 rounded transition flex items-center space-x-1 cursor-pointer ${
                      copiedField === 'primary' 
                        ? 'bg-emerald-600 text-white' 
                        : 'bg-indigo-600 hover:bg-indigo-500 text-white'
                    }`}
                  >
                    {copiedField === 'primary' ? <Check size={12} /> : <Copy size={12} />}
                    <span>{copiedField === 'primary' ? 'Copied!' : 'Copy'}</span>
                  </button>
                </div>
                <p className="text-[10px] text-slate-500">Paste this into the <strong>Primary IP</strong> field in Upstox Developer Portal.</p>
              </div>

              {/* Secondary IP Card */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-3.5 space-y-1.5">
                <div className="flex justify-between items-center">
                  <span className="font-bold text-slate-300 text-xs flex items-center">
                    <span className="w-2 h-2 rounded-full bg-blue-400 mr-2"></span>
                    Secondary IP (Local / Host IP)
                  </span>
                  <span className="text-[10px] font-bold bg-slate-800 text-slate-400 px-2 py-0.5 rounded">
                    OPTIONAL
                  </span>
                </div>
                
                <div className="flex items-center justify-between bg-slate-900 border border-slate-700/80 rounded-lg px-3 py-2">
                  <span className="font-mono text-sm font-bold text-slate-200 tracking-wider">
                    {ipLoading ? 'Detecting...' : (ipData.secondary_ip || '127.0.0.1')}
                  </span>
                  <button
                    type="button"
                    onClick={() => copyToClipboard(ipData.secondary_ip, 'secondary')}
                    className={`text-[11px] font-bold px-2.5 py-1 rounded transition flex items-center space-x-1 cursor-pointer ${
                      copiedField === 'secondary' 
                        ? 'bg-emerald-600 text-white' 
                        : 'bg-slate-700 hover:bg-slate-600 text-white'
                    }`}
                  >
                    {copiedField === 'secondary' ? <Check size={12} /> : <Copy size={12} />}
                    <span>{copiedField === 'secondary' ? 'Copied!' : 'Copy'}</span>
                  </button>
                </div>
                <p className="text-[10px] text-slate-500">Paste this into <strong>Secondary IP</strong> or leave it blank if not required.</p>
              </div>

              <div className="p-3 bg-indigo-950/40 border border-indigo-800/40 rounded-lg text-[11px] text-indigo-300/90 leading-relaxed">
                💡 <strong>Dynamic IP note:</strong> If your Wi-Fi/ISP changes your public IP after a router restart, you can open this popup anytime to view your new IP and update Upstox.
              </div>
            </div>

            {/* Footer */}
            <div className="px-6 py-3 bg-slate-950 border-t border-slate-800 flex justify-end">
              <button
                type="button"
                onClick={() => setIpModalOpen(false)}
                className="px-4 py-1.5 bg-slate-800 hover:bg-slate-700 text-white font-bold text-xs rounded-lg transition cursor-pointer"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

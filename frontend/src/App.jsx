import React, { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Target, Activity, Search, BookmarkPlus, FolderOpen, BrainCircuit, Database, Network, Settings, TrendingUp, Zap } from 'lucide-react';
import Dashboard from './pages/Dashboard';
import CustomStrategy from './pages/CustomStrategy';
import StrategyLibrary from './pages/StrategyLibrary';
import WatchlistScanner from './pages/WatchlistScanner';
import SavedStrategies from './pages/SavedStrategies';
import IntradayScanner from './pages/IntradayScanner';
import SwingScanner from './pages/SwingScanner';
import DataDump from './pages/DataDump';
import MLLab from './pages/MLLab';
import DataLab from './pages/DataLab';
import Profile from './pages/Profile';
import { LiveIndicatorProvider } from './context/LiveIndicatorContext';

function LinkItem({ to, icon: Icon, label }) {
  const location = useLocation();
  const isActive = location.pathname === to;
  return (
    <Link
      to={to}
      className={`flex items-center px-4 py-3 mb-2 rounded-xl transition-all duration-200 ${
        isActive
          ? 'bg-gradient-to-r from-indigo-500 to-indigo-600 text-white shadow-md'
          : 'text-gray-400 hover:bg-gray-800 hover:text-white'
      }`}
    >
      <Icon size={20} className={`mr-3 ${isActive ? 'text-white' : 'opacity-70'}`} />
      <span className="font-medium text-sm tracking-wide">{label}</span>
    </Link>
  );
}

function App() {
  const [activeMonitors, setActiveMonitors] = useState([]);
  const [lastScan, setLastScan] = useState(null);
  const [dataSource, setDataSource] = useState('yfinance');
  const [simulationMode, setSimulationMode] = useState(true);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        // 1. Monitored positions
        const res = await fetch('http://localhost:8000/api/ml/active-monitors');
        if (res.ok) {
          const data = await res.json();
          setActiveMonitors(data);
          setLastScan(new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'}));
        }
        
        // 2. Active Market Data Source
        const dsRes = await fetch('http://localhost:8000/api/settings/datasource');
        if (dsRes.ok) {
          const dsData = await dsRes.json();
          if (dsData.source) setDataSource(dsData.source);
        }

        // 3. Global Simulation Mode
        const simRes = await fetch('http://localhost:8000/api/settings/simulation');
        if (simRes.ok) {
          const simData = await simRes.json();
          if (typeof simData.simulation_mode === 'boolean') {
            setSimulationMode(simData.simulation_mode);
          }
        }
      } catch (err) {}
    };
    fetchStatus();
    const interval = setInterval(fetchStatus, 8000);
    return () => clearInterval(interval);
  }, []);

  return (
    <LiveIndicatorProvider>
      <Router>
        <div className="flex h-screen bg-gray-50 font-sans text-gray-900 overflow-hidden relative">
          
          <nav className="w-64 bg-gray-900 text-white flex flex-col shadow-xl z-10 relative overflow-hidden">
            <div className="flex-1 overflow-y-auto p-4 custom-scrollbar">
            <div className="flex items-center justify-center mb-6 mt-4">
              <div className="w-10 h-10 bg-indigo-500 rounded-lg flex items-center justify-center shadow-lg transform -rotate-6 mr-3">
                <Target size={24} className="text-white transform rotate-6" />
              </div>
              <h1 className="text-xl font-black tracking-tight text-white">SWING<span className="text-indigo-400">AI</span></h1>
            </div>

            {/* Live Data Source Indicator */}
            <div className="mx-4 mb-4 flex items-center justify-center">
              <span className={`text-[10px] font-bold px-2.5 py-1 rounded-full flex items-center space-x-1.5 ${dataSource === 'upstox' ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/50' : 'bg-gray-800 text-gray-300 border border-gray-700'}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${dataSource === 'upstox' ? 'bg-emerald-400 animate-pulse' : 'bg-yellow-400'}`}></span>
                <span>{dataSource === 'upstox' ? 'UPSTOX REALTIME (0ms)' : 'YAHOO FINANCE (15m)'}</span>
              </span>
            </div>
            
            {/* Global Active Monitor Badge */}
            {activeMonitors.length > 0 && (
              <div className="mx-4 mb-8 bg-gray-800 border border-gray-700 rounded-xl p-3 flex flex-col">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-bold text-gray-400 uppercase tracking-wider">AI Guard</span>
                  <div className="flex items-center space-x-1">
                    <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse"></span>
                    <span className="text-[10px] font-bold text-green-400">ACTIVE</span>
                  </div>
                </div>
                <div className="text-sm font-medium text-white mb-1">
                  Monitoring {activeMonitors.length} Trades
                </div>
                {lastScan && (
                  <div className="text-[10px] text-gray-400 mb-2 font-mono flex items-center">
                    <Zap size={10} className="mr-1 text-yellow-500" /> Last Sweep: {lastScan}
                  </div>
                )}
                <div className="flex flex-wrap gap-1 mt-1">
                  {activeMonitors.slice(0, 3).map((m, i) => (
                    <span key={i} className="text-[10px] px-1.5 py-0.5 bg-gray-700 text-gray-300 rounded">
                      {m.ticker.replace('.NS', '')}
                    </span>
                  ))}
                  {activeMonitors.length > 3 && <span className="text-[10px] px-1.5 py-0.5 text-gray-400">+{activeMonitors.length - 3}</span>}
                </div>
              </div>
            )}
            {activeMonitors.length === 0 && (
              <div className="mb-8"></div>
            )}
            
            <div className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-3 px-2">Market Engine</div>
            <LinkItem to="/" icon={LayoutDashboard} label="Dashboard" />
            <LinkItem to="/scanner" icon={Search} label="Watchlist Scanner" />
            
            <div className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-3 mt-8 px-2">Backtester</div>
            <LinkItem to="/strategy/new" icon={Activity} label="Custom Strategy" />
            <LinkItem to="/strategy/library" icon={BookmarkPlus} label="Strategy Library" />
            <LinkItem to="/saved" icon={FolderOpen} label="Saved Strategies" />

            <div className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-3 mt-8 px-2 text-indigo-400">AI Tools</div>
            <LinkItem to="/ai-scan" icon={BrainCircuit} label="Intraday ML Scan" />
            <LinkItem to="/swing-scan" icon={Target} label="Swing ML Scan" />
            <LinkItem to="/ml-lab" icon={Network} label="AI Brain & Lab" />
            <LinkItem to="/data-lab" icon={Database} label="10Y Research Data Lab" />
            
            <div className="mt-auto pt-8">
              <div className="border-t border-gray-800 pt-4">
                <LinkItem to="/data-dump" icon={Database} label="System Cache Dump" />
                <LinkItem to="/profile" icon={Settings} label="Settings & Profile" />
              </div>
            </div>
            </div>
          </nav>
          
          <div className="flex-1 flex flex-col overflow-hidden bg-[#f4f7f9] relative">
            <div className="flex-1 overflow-y-auto p-10 pb-24">
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/strategy/new" element={<CustomStrategy />} />
                <Route path="/strategy/library" element={<StrategyLibrary />} />
                <Route path="/scanner" element={<WatchlistScanner />} />
                <Route path="/saved" element={<SavedStrategies />} />
                <Route path="/ai-scan" element={<IntradayScanner />} />
                <Route path="/swing-scan" element={<SwingScanner />} />
                <Route path="/ml-lab" element={<MLLab />} />
                <Route path="/data-lab" element={<DataLab />} />
                <Route path="/data-dump" element={<DataDump />} />
                <Route path="/profile" element={<Profile />} />
              </Routes>
            </div>

            {/* Persistent Live Telemetry & Bottom Status Bar */}
            <div className="h-10 bg-slate-900 border-t border-slate-800 text-slate-300 text-xs px-6 flex items-center justify-between z-30 shadow-2xl font-mono shrink-0 select-none">
              
              {/* Left: Data Source & Last Sweep */}
              <div className="flex items-center space-x-4">
                <div className="flex items-center space-x-2">
                  <span className={`w-2 h-2 rounded-full ${dataSource === 'upstox' ? 'bg-emerald-400 animate-pulse' : 'bg-yellow-400'}`}></span>
                  <span className="font-bold text-white text-[11px]">
                    {dataSource === 'upstox' ? 'UPSTOX REAL-TIME (0ms)' : 'YAHOO FINANCE (15m Delay)'}
                  </span>
                </div>
                
                {lastScan && (
                  <span className="text-[10px] text-slate-400 hidden sm:inline">
                    ⚡ Last Swept: <strong className="text-slate-200">{lastScan}</strong>
                  </span>
                )}
              </div>

              {/* Center: Environment & Safeguard */}
              <div className="flex items-center space-x-3 text-[11px]">
                <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${simulationMode ? 'bg-emerald-950/60 text-emerald-300 border-emerald-800' : 'bg-rose-950/80 text-rose-300 border-rose-700 animate-pulse'}`}>
                  {simulationMode ? '🛡️ SIMULATION (PAPER)' : '⚠️ LIVE REAL MONEY'}
                </span>
                
                <span className="text-slate-400 hidden md:inline">
                  👁️ {activeMonitors.length} Positions Under Watch
                </span>
              </div>

              {/* Right: AI Brain Engine & Quick Config */}
              <div className="flex items-center space-x-3 text-[10px]">
                <span className="text-indigo-400 font-bold hidden lg:inline">
                  🧠 4-Layer Ensemble Active
                </span>
                <Link to="/profile" className="text-slate-400 hover:text-white transition flex items-center space-x-1">
                  <Settings size={12} />
                  <span>Config</span>
                </Link>
              </div>

            </div>

          </div>
        </div>
      </Router>
    </LiveIndicatorProvider>
  );
}

export default App;

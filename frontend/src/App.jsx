import React, { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Target, Activity, Search, BookmarkPlus, FolderOpen, BrainCircuit, Database, Network, Settings, TrendingUp, Zap, Menu, X, ShieldCheck } from 'lucide-react';
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
import SystemAudit from './pages/SystemAudit';
import { LiveIndicatorProvider } from './context/LiveIndicatorContext';
import { API_BASE } from './services/api';

function LinkItem({ to, icon: Icon, label, onClick }) {
  const location = useLocation();
  const isActive = location.pathname === to;
  return (
    <Link
      to={to}
      onClick={onClick}
      className={`flex items-center px-4 py-3 mb-2 rounded-xl transition-all duration-200 ${
        isActive
          ? 'bg-gradient-to-r from-indigo-500 to-indigo-600 text-white shadow-md'
          : 'text-gray-400 hover:bg-gray-800 hover:text-white'
      }`}
    >
      <Icon size={20} className={`mr-3 shrink-0 ${isActive ? 'text-white' : 'opacity-70'}`} />
      <span className="font-medium text-sm tracking-wide truncate">{label}</span>
    </Link>
  );
}

function App() {
  const [activeMonitors, setActiveMonitors] = useState([]);
  const [lastScan, setLastScan] = useState(null);
  const [dataSource, setDataSource] = useState('yfinance');
  const [simulationMode, setSimulationMode] = useState(true);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        // 1. Monitored positions
        const res = await fetch(`${API_BASE}/ml/active-monitors`);
        if (res.ok) {
          const data = await res.json();
          setActiveMonitors(data);
          setLastScan(new Date().toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'}));
        }
        
        // 2. Active Market Data Source
        const dsRes = await fetch(`${API_BASE}/settings/datasource`);
        if (dsRes.ok) {
          const dsData = await dsRes.json();
          if (dsData.source) setDataSource(dsData.source);
        }

        // 3. Global Simulation Mode
        const simRes = await fetch(`${API_BASE}/settings/simulation`);
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
          
          {/* Mobile Backdrop Overlay */}
          {mobileNavOpen && (
            <div 
              onClick={() => setMobileNavOpen(false)}
              className="fixed inset-0 bg-black/60 backdrop-blur-xs z-40 lg:hidden animate-fade-in"
            />
          )}

          {/* Navigation Sidebar (Desktop static / Mobile sliding drawer) */}
          <nav className={`fixed inset-y-0 left-0 z-50 w-64 bg-gray-900 text-white flex flex-col shadow-2xl transition-transform duration-300 ease-in-out lg:static lg:translate-x-0 ${mobileNavOpen ? 'translate-x-0' : '-translate-x-full'} overflow-hidden shrink-0`}>
            <div className="flex-1 overflow-y-auto p-4 custom-scrollbar">
              <div className="flex items-center justify-between mb-6 mt-4">
                <div className="flex items-center">
                  <div className="w-10 h-10 bg-indigo-500 rounded-lg flex items-center justify-center shadow-lg transform -rotate-6 mr-3 shrink-0">
                    <Target size={24} className="text-white transform rotate-6" />
                  </div>
                  <h1 className="text-xl font-black tracking-tight text-white">SWING<span className="text-indigo-400">AI</span></h1>
                </div>
                <button 
                  onClick={() => setMobileNavOpen(false)}
                  className="lg:hidden p-1.5 text-gray-400 hover:text-white rounded-lg hover:bg-gray-800 transition"
                  aria-label="Close menu"
                >
                  <X size={20} />
                </button>
              </div>

              {/* Live Data Source Indicator */}
              <div className="mx-2 mb-4 flex items-center justify-center">
                <span className={`text-[10px] font-bold px-2.5 py-1 rounded-full flex items-center space-x-1.5 ${dataSource === 'upstox' ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/50' : 'bg-gray-800 text-gray-300 border border-gray-700'}`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${dataSource === 'upstox' ? 'bg-emerald-400 animate-pulse' : 'bg-yellow-400'}`}></span>
                  <span>{dataSource === 'upstox' ? 'UPSTOX REALTIME (0ms)' : 'YAHOO FINANCE (15m)'}</span>
                </span>
              </div>
              
              {/* Global Active Monitor Badge */}
              {activeMonitors.length > 0 && (
                <div className="mx-2 mb-6 bg-gray-800 border border-gray-700 rounded-xl p-3 flex flex-col">
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
                <div className="mb-6"></div>
              )}
              
              <div className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-3 px-2">Market Engine</div>
              <LinkItem to="/" icon={LayoutDashboard} label="Dashboard" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/scanner" icon={Search} label="Watchlist Scanner" onClick={() => setMobileNavOpen(false)} />
              
              <div className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-3 mt-6 px-2">Backtester</div>
              <LinkItem to="/strategy/new" icon={Activity} label="Custom Strategy" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/strategy/library" icon={BookmarkPlus} label="Strategy Library" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/saved" icon={FolderOpen} label="Saved Strategies" onClick={() => setMobileNavOpen(false)} />

              <div className="text-xs font-semibold text-gray-600 uppercase tracking-wider mb-3 mt-6 px-2 text-indigo-400">AI Tools</div>
              <LinkItem to="/ai-scan" icon={BrainCircuit} label="Intraday ML Scan" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/swing-scan" icon={Target} label="Swing ML Scan" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/ml-lab" icon={Network} label="AI Brain & Lab" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/data-lab" icon={Database} label="10Y Research Data Lab" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/audit" icon={ShieldCheck} label="Master Audit Log" onClick={() => setMobileNavOpen(false)} />
              
              <div className="mt-auto pt-6">
                <div className="border-t border-gray-800 pt-4">
                  <LinkItem to="/data-dump" icon={Database} label="System Cache Dump" onClick={() => setMobileNavOpen(false)} />
                  <LinkItem to="/profile" icon={Settings} label="Settings & Profile" onClick={() => setMobileNavOpen(false)} />
                </div>
              </div>
            </div>
          </nav>
          
          <div className="flex-1 flex flex-col overflow-hidden bg-[#f4f7f9] relative min-w-0">
            
            {/* Mobile Top App Bar (visible on < lg screens) */}
            <header className="lg:hidden bg-gray-900 text-white px-4 py-3 flex items-center justify-between shadow-md z-30 shrink-0 select-none">
              <div className="flex items-center space-x-2.5">
                <button
                  onClick={() => setMobileNavOpen(!mobileNavOpen)}
                  className="p-2 bg-gray-800 hover:bg-gray-700 text-gray-200 rounded-lg transition"
                  aria-label="Toggle navigation"
                >
                  <Menu size={20} />
                </button>
                <div className="flex items-center space-x-2">
                  <div className="w-7 h-7 bg-indigo-500 rounded-md flex items-center justify-center shadow">
                    <Target size={16} className="text-white" />
                  </div>
                  <span className="font-black text-base tracking-tight">SWING<span className="text-indigo-400">AI</span></span>
                </div>
              </div>
              
              <div className="flex items-center space-x-2">
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${simulationMode ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' : 'bg-rose-950 text-rose-300 border border-rose-800'}`}>
                  {simulationMode ? 'PAPER' : 'LIVE'}
                </span>
                <Link to="/profile" className="p-1.5 text-gray-400 hover:text-white rounded-lg">
                  <Settings size={18} />
                </Link>
              </div>
            </header>

            {/* Main Content Area */}
            <main className="flex-1 overflow-y-auto overflow-x-hidden p-3.5 sm:p-6 lg:p-10 pb-24 max-w-full">
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
                <Route path="/audit" element={<SystemAudit />} />
                <Route path="/profile" element={<Profile />} />
              </Routes>
            </main>

            {/* Persistent Live Telemetry & Bottom Status Bar */}
            <footer className="h-10 bg-slate-900 border-t border-slate-800 text-slate-300 text-xs px-3 sm:px-6 flex items-center justify-between z-30 shadow-2xl font-mono shrink-0 select-none max-w-full overflow-hidden">
              
              {/* Left: Data Source & Last Sweep */}
              <div className="flex items-center space-x-2 sm:space-x-4 min-w-0">
                <div className="flex items-center space-x-1.5 shrink-0">
                  <span className={`w-2 h-2 rounded-full ${dataSource === 'upstox' ? 'bg-emerald-400 animate-pulse' : 'bg-yellow-400'}`}></span>
                  <span className="font-bold text-white text-[10px] sm:text-[11px] truncate max-w-[120px] sm:max-w-none">
                    {dataSource === 'upstox' ? 'UPSTOX (0ms)' : 'YAHOO (15m)'}
                  </span>
                </div>
                
                {lastScan && (
                  <span className="text-[10px] text-slate-400 hidden sm:inline truncate">
                    ⚡ Swept: <strong className="text-slate-200">{lastScan}</strong>
                  </span>
                )}
              </div>

              {/* Center: Environment & Safeguard */}
              <div className="flex items-center space-x-2 sm:space-x-3 text-[10px] sm:text-[11px] shrink-0">
                <span className={`px-1.5 sm:px-2 py-0.5 rounded text-[9px] sm:text-[10px] font-bold border ${simulationMode ? 'bg-emerald-950/60 text-emerald-300 border-emerald-800' : 'bg-rose-950/80 text-rose-300 border-rose-700 animate-pulse'}`}>
                  {simulationMode ? '🛡️ SIMULATION' : '⚠️ LIVE'}
                </span>
                
                <span className="text-slate-400 hidden md:inline">
                  👁️ {activeMonitors.length} Watch
                </span>
              </div>

              {/* Right: AI Brain Engine & Quick Config */}
              <div className="flex items-center space-x-2 sm:space-x-3 text-[10px] shrink-0">
                <span className="text-indigo-400 font-bold hidden lg:inline">
                  🧠 4-Layer Ensemble
                </span>
                <Link to="/profile" className="text-slate-400 hover:text-white transition flex items-center space-x-1">
                  <Settings size={12} />
                  <span className="hidden sm:inline">Config</span>
                </Link>
              </div>

            </footer>

          </div>
        </div>
      </Router>
    </LiveIndicatorProvider>
  );
}

export default App;

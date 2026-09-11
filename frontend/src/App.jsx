import React, { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Target, Activity, Search, BookmarkPlus, FolderOpen, BrainCircuit, Database, Network, Settings, TrendingUp, Zap, Menu, X, ShieldCheck, HelpCircle, Cpu } from 'lucide-react';
import Dashboard from './pages/Dashboard';
import CustomStrategy from './pages/CustomStrategy';
import StrategyLibrary from './pages/StrategyLibrary';
import WatchlistScanner from './pages/WatchlistScanner';
import SavedStrategies from './pages/SavedStrategies';
import IntradayScanner from './pages/IntradayScanner';
import SwingScanner from './pages/SwingScanner';
import SmartScanner from './pages/SmartScanner';
import DataDump from './pages/DataDump';
import MLLab from './pages/MLLab';
import DataLab from './pages/DataLab';
import Profile from './pages/Profile';
import SystemAudit from './pages/SystemAudit';
import HelpCenter from './pages/HelpCenter';
import AutonomousResearchLab from './pages/AutonomousResearchLab';
import QlibControlRoom from './pages/QlibControlRoom';
import { LiveIndicatorProvider } from './context/LiveIndicatorContext';
import { API_BASE } from './services/api';
import ErrorBoundary from './components/common/ErrorBoundary';


function LinkItem({ to, aliases = [], icon: Icon, label, onClick }) {
  const location = useLocation();
  const isActive = location.pathname === to || aliases.includes(location.pathname);
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
  const [winRateData, setWinRateData] = useState({ win_rate: null, total_closed_trades: 0, wins: 0, display_rate: 'N/A' });

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

        // 4. Production Model Win Rate
        try {
          const wrRes = await fetch(`${API_BASE}/ml/production-win-rate`);
          if (wrRes.ok) {
            const wrData = await wrRes.json();
            setWinRateData(wrData);
          }
        } catch (err) {}
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
              <div className="mx-2 mb-3 flex items-center justify-center">
                <span className={`text-[10px] font-bold px-2.5 py-1 rounded-full flex items-center space-x-1.5 ${dataSource === 'upstox' ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/50' : 'bg-gray-800 text-gray-300 border border-gray-700'}`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${dataSource === 'upstox' ? 'bg-emerald-400 animate-pulse' : 'bg-yellow-400'}`}></span>
                  <span>{dataSource === 'upstox' ? 'UPSTOX REALTIME (0ms)' : 'YAHOO FINANCE (15m)'}</span>
                </span>
              </div>

              {/* Production Model Win Rate Badge */}
              <div className="mx-2 mb-3 bg-slate-800/90 border border-slate-700/80 rounded-xl p-2.5 flex items-center justify-between shadow-xs">
                <div className="flex items-center space-x-2">
                  <div className="w-6 h-6 rounded-lg bg-indigo-500/20 text-indigo-400 flex items-center justify-center shrink-0">
                    <TrendingUp size={13} />
                  </div>
                  <div>
                    <div className="text-[10px] font-bold text-gray-300 uppercase tracking-wider">AI Win Rate</div>
                    <div className="text-[9px] text-gray-400 font-mono">
                      {winRateData.total_closed_trades ? `${winRateData.wins}/${winRateData.total_closed_trades} Closed` : 'Closed Trades'}
                    </div>
                  </div>
                </div>
                <div className="text-right">
                  <span className={`text-xs font-black font-mono px-2 py-0.5 rounded-full ${
                    winRateData.display_rate === 'N/A'
                      ? 'bg-slate-700/80 text-gray-400 border border-slate-600'
                      : (winRateData.win_rate >= 50 
                          ? 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/60' 
                          : 'bg-indigo-950/80 text-indigo-300 border border-indigo-700/60')
                  }`}>
                    {winRateData.display_rate || 'N/A'}
                  </span>
                </div>
              </div>
              
              {/* Cached Memory Telemetry Badge */}
              <Link
                to="/data-dump"
                onClick={() => setMobileNavOpen(false)}
                className="mx-2 mb-3 bg-slate-800/90 hover:bg-slate-800 border border-slate-700/80 rounded-xl p-2.5 flex items-center justify-between shadow-xs transition group cursor-pointer"
                title="View Local Database Dump & Cache Telemetry"
              >
                <div className="flex items-center space-x-2">
                  <div className="w-6 h-6 rounded-lg bg-cyan-500/20 text-cyan-400 flex items-center justify-center shrink-0">
                    <Database size={13} />
                  </div>
                  <div>
                    <div className="text-[10px] font-bold text-gray-300 uppercase tracking-wider group-hover:text-cyan-300 transition">Cached Memory</div>
                    <div className="text-[9px] text-gray-400 font-mono">1.03M Bars (10Y)</div>
                  </div>
                </div>
                <div className="text-right">
                  <span className="text-[10px] font-bold font-mono px-2 py-0.5 rounded-full bg-cyan-950/80 text-cyan-300 border border-cyan-700/60">
                    511 Tickers
                  </span>
                </div>
              </Link>
              
              {/* Global Active Monitor Badge */}
              {activeMonitors.length > 0 && (
                <div className="mx-2 mb-4 bg-gray-800 border border-gray-700 rounded-xl p-3 shadow-inner">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="flex items-center text-xs font-bold text-gray-200">
                      <span className="w-2 h-2 rounded-full bg-emerald-400 mr-2 animate-ping"></span>
                      Active Position Bot
                    </span>
                    <span className="text-[10px] bg-emerald-950 text-emerald-300 border border-emerald-800 px-1.5 py-0.5 rounded font-mono">
                      FAIL-CLOSED
                    </span>
                  </div>
                  <div className="text-xs text-gray-300 font-medium">
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
                <div className="mb-4"></div>
              )}
              
              <div className="text-[10px] font-bold text-gray-500 uppercase tracking-wider mb-3 px-2">Primary Operations</div>
              <LinkItem to="/" icon={LayoutDashboard} label="Command Center" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/scanner" aliases={['/smart-scanner', '/ai-scan', '/swing-scan', '/watchlist-scanner']} icon={Zap} label="Smart AI Scanner" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/research" aliases={['/research-autopilot', '/data-lab']} icon={Cpu} label="Autonomous Research" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/models" aliases={['/ml-lab']} icon={Network} label="Model Lab & Governance" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/system" aliases={['/audit']} icon={ShieldCheck} label="System Operations" onClick={() => setMobileNavOpen(false)} />
              <LinkItem to="/data-dump" icon={Database} label="Cached Memory & Data" onClick={() => setMobileNavOpen(false)} />

              <div className="text-[10px] font-bold text-purple-400 uppercase tracking-wider my-3 px-2 flex items-center justify-between">
                <span>Microsoft Qlib</span>
                <span className="text-[8px] px-1.5 py-0.5 bg-purple-900/60 text-purple-300 rounded border border-purple-500/40">NEW</span>
              </div>
              <LinkItem to="/qlib" aliases={['/qlib/scanner', '/qlib/training', '/qlib/fno', '/qlib/comparison']} icon={Cpu} label="Qlib Control Room" onClick={() => setMobileNavOpen(false)} />
              
              <div className="mt-auto pt-6">
                <div className="border-t border-gray-800 pt-4">
                  <LinkItem to="/profile" icon={Settings} label="Config & Profile" onClick={() => setMobileNavOpen(false)} />
                  <LinkItem to="/help" icon={HelpCircle} label="Help & User Guide" onClick={() => setMobileNavOpen(false)} />
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
              <ErrorBoundary sectionName="Application Router">
                <Routes>
                  {/* ── 5 PRIMARY CANONICAL PLATFORM VIEWS ────────────────── */}
                  <Route path="/" element={<Dashboard />} />
                  <Route path="/scanner" element={<SmartScanner />} />
                  <Route path="/research" element={<AutonomousResearchLab />} />
                  <Route path="/models" element={<MLLab />} />
                  <Route path="/system" element={<SystemAudit />} />
                  <Route path="/qlib" element={<QlibControlRoom />} />
                  <Route path="/qlib/*" element={<QlibControlRoom />} />

                  {/* ── BACKWARD-COMPATIBILITY ROUTE WRAPPERS ─────────────── */}
                  <Route path="/smart-scanner" element={<SmartScanner />} />
                  <Route path="/ai-scan" element={<SmartScanner defaultTimeframe="intraday" />} />
                  <Route path="/swing-scan" element={<SmartScanner defaultTimeframe="swing" />} />
                  <Route path="/research-autopilot" element={<AutonomousResearchLab />} />
                  <Route path="/ml-lab" element={<MLLab />} />
                  <Route path="/audit" element={<SystemAudit />} />
                  <Route path="/data-lab" element={<DataLab />} />
                  <Route path="/data-dump" element={<DataDump />} />
                  <Route path="/profile" element={<Profile />} />
                  <Route path="/help" element={<HelpCenter />} />

                  {/* ── ADVANCED / STRATEGY SUB-ROUTES ─────────────────────── */}
                  <Route path="/strategy/new" element={<CustomStrategy />} />
                  <Route path="/strategy/library" element={<StrategyLibrary />} />
                  <Route path="/saved" element={<SavedStrategies />} />
                  <Route path="/custom" element={<CustomStrategy />} />
                  <Route path="/watchlist-scanner" element={<WatchlistScanner />} />
                </Routes>
              </ErrorBoundary>
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
                <Link to="/help" className="text-slate-400 hover:text-indigo-300 transition flex items-center space-x-1">
                  <HelpCircle size={12} />
                  <span className="hidden sm:inline">Help</span>
                </Link>
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

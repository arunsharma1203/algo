import React, { useState } from 'react';
import { X, ShieldAlert, Zap } from 'lucide-react';
import axios from 'axios';

export default function ExecutionModal({ trade, onClose, onSuccess }) {
  // Load global profile config
  const savedProfileStr = localStorage.getItem('swing_profile');
  const defaultProfile = savedProfileStr ? JSON.parse(savedProfileStr) : {
    apiKey: localStorage.getItem('indmoney_api_token') || '',
    simulationMode: true,
    defaultCapital: 100000
  };

  const [apiToken, setApiToken] = useState(defaultProfile.apiKey);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const capital = Number(defaultProfile.defaultCapital) || 100000;
  const maxRisk = Number(defaultProfile.maxRiskPerTrade) || 2.0;
  
  // Dynamic Risk-Based Position Sizing
  const riskAmount = capital * (maxRisk / 100);
  const riskPerShare = Math.abs(trade.entry - trade.sl);
  let qty = 0;
  if (riskPerShare > 0) {
    qty = Math.floor(riskAmount / riskPerShare);
  }
  // Safeguard: Ensure we don't exceed total capital
  const maxQtyByCapital = Math.floor(capital / trade.entry);
  qty = Math.min(qty, maxQtyByCapital);

  const potentialLoss = (riskPerShare * qty).toFixed(2);
  const potentialGain = (Math.abs(trade.tp1 - trade.entry) * qty).toFixed(2);

  const handleExecute = async () => {
    if (!apiToken || apiToken.length < 10) {
      setError("Please enter a valid INDstocks API Token");
      return;
    }
    
    // Save token for future use
    localStorage.setItem('indmoney_api_token', apiToken);
    
    setLoading(true);
    setError(null);
    try {
      // If simulation mode is OFF, we would theoretically hit the real INDmoney API here.
      // But the backend /api/broker/execute is designed to mock it anyway for safety.
      // We pass the simulation flag to the backend so it knows what to log.
      const response = await axios.post('http://localhost:8000/api/broker/execute', {
        api_token: apiToken,
        ticker: trade.ticker,
        action: trade.action,
        quantity: qty,
        target: trade.tp1,
        stop_loss: trade.sl,
        order_type: "LIMIT",
        simulation: defaultProfile.simulationMode
      });
      
      onSuccess(response.data);
    } catch (err) {
      setError(err.response?.data?.detail || "Execution failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-xl max-w-lg w-full shadow-2xl overflow-hidden border border-gray-200">
        
        {/* Header */}
        <div className={`p-4 flex justify-between items-center ${trade.action === 'BUY' ? 'bg-green-600' : 'bg-red-600'}`}>
          <h2 className="text-xl font-bold text-white flex items-center">
            <Zap size={20} className="mr-2" /> 
            1-Click Execution: {trade.ticker}
          </h2>
          <button onClick={onClose} className="text-white opacity-70 hover:opacity-100">
            <X size={24} />
          </button>
        </div>
        
        {/* Safeguard Warning */}
        <div className={`p-3 flex items-start space-x-3 border-b ${defaultProfile.simulationMode ? 'bg-blue-50 border-blue-100' : 'bg-orange-50 border-orange-100'}`}>
          <ShieldAlert className={`${defaultProfile.simulationMode ? 'text-blue-500' : 'text-orange-500'} mt-1 flex-shrink-0`} size={20} />
          <p className={`text-xs font-medium ${defaultProfile.simulationMode ? 'text-blue-800' : 'text-orange-800'}`}>
            {defaultProfile.simulationMode 
              ? <span><strong>Simulation Mode:</strong> This execution will be validated but NOT sent to the exchange.</span>
              : <span><strong>Safeguard Active:</strong> You are in LIVE TRADING mode. Review the parameters carefully before sending to INDstocks.</span>}
          </p>
        </div>

        <div className="p-6">
          <div className="grid grid-cols-2 gap-4 mb-6">
            <div className="bg-gray-50 p-3 rounded-lg border border-gray-200">
              <span className="text-xs text-gray-500 block mb-1 uppercase font-bold">Action</span>
              <span className={`text-lg font-bold ${trade.action === 'BUY' ? 'text-green-600' : 'text-red-600'}`}>{trade.action}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded-lg border border-gray-200">
              <span className="text-xs text-gray-500 block mb-1 uppercase font-bold">Quantity</span>
              <span className="text-lg font-bold text-gray-800">{qty} shares</span>
            </div>
            <div className="bg-gray-50 p-3 rounded-lg border border-gray-200">
              <span className="text-xs text-gray-500 block mb-1 uppercase font-bold">Limit Price</span>
              <span className="text-lg font-bold text-gray-800">₹{trade.entry.toFixed(2)}</span>
            </div>
            <div className="bg-gray-50 p-3 rounded-lg border border-gray-200">
              <span className="text-xs text-gray-500 block mb-1 uppercase font-bold">Capital Required</span>
              <span className="text-lg font-bold text-gray-800">₹{(qty * trade.entry).toLocaleString()}</span>
            </div>
          </div>

          <div className="mb-6 p-4 border border-gray-200 rounded-lg shadow-inner bg-gray-50">
            <h3 className="text-sm font-bold text-gray-700 mb-3 uppercase tracking-wider">Risk Profile</h3>
            <div className="flex justify-between items-center mb-2">
              <span className="text-red-600 font-medium">Stop Loss: ₹{trade.sl.toFixed(2)}</span>
              <span className="text-red-600 font-bold">-₹{Math.abs(potentialLoss)}</span>
            </div>
            <div className="flex justify-between items-center">
              <span className="text-green-600 font-medium">Target 1: ₹{trade.tp1.toFixed(2)}</span>
              <span className="text-green-600 font-bold">+₹{Math.abs(potentialGain)}</span>
            </div>
          </div>

          <div className="mb-6">
            <label className="block text-sm font-bold text-gray-700 mb-2">INDstocks API Token</label>
            <input 
              type="password" 
              value={apiToken}
              onChange={e => setApiToken(e.target.value)}
              className="w-full border border-gray-300 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Paste your API token here..."
            />
            {error && <p className="text-red-500 text-xs mt-2 font-medium">{error}</p>}
          </div>

          <button 
            onClick={handleExecute}
            disabled={loading}
            className={`w-full py-4 rounded-lg font-bold text-lg text-white transition shadow-lg ${
              loading ? 'bg-gray-400 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-700'
            }`}
          >
            {loading ? 'Executing...' : 'CONFIRM & EXECUTE'}
          </button>
        </div>
      </div>
    </div>
  );
}

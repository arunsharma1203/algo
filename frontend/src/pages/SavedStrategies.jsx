import React, { useState, useEffect } from 'react';
import { PlayCircle, Trash2, Edit } from 'lucide-react';
import BacktestViewer from '../components/BacktestViewer';

export default function SavedStrategies() {
  const [savedStrategies, setSavedStrategies] = useState([]);
  const [selectedStrategy, setSelectedStrategy] = useState(null);

  useEffect(() => {
    const existingStr = localStorage.getItem('saved_strategies');
    if (existingStr) {
      try {
        setSavedStrategies(JSON.parse(existingStr));
      } catch (e) {}
    }
  }, []);

  const handleDelete = (name) => {
    const newSaved = savedStrategies.filter(s => s.name !== name);
    setSavedStrategies(newSaved);
    localStorage.setItem('saved_strategies', JSON.stringify(newSaved));
  };

  if (selectedStrategy) {
    return (
      <div>
        <button onClick={() => setSelectedStrategy(null)} className="mb-4 text-blue-500 hover:underline">
          &larr; Back to Saved Strategies
        </button>
        <BacktestViewer strategy={selectedStrategy} />
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-3xl font-bold mb-2 text-gray-800">Saved Strategies</h1>
      <p className="text-gray-600 mb-8">View, backtest, and manage your custom created strategies.</p>
      
      {savedStrategies.length === 0 ? (
        <div className="bg-white p-8 rounded-lg shadow-sm border border-gray-200 text-center">
          <p className="text-gray-500 mb-4">You haven't saved any custom strategies yet.</p>
          <a href="/custom" className="text-blue-600 hover:underline font-medium">Create your first strategy</a>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {savedStrategies.map((st, i) => (
            <div key={i} className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 flex flex-col">
              <h3 className="text-xl font-bold text-gray-800 mb-2">{st.name}</h3>
              
              <div className="text-sm text-gray-500 mb-4 bg-gray-50 p-3 rounded">
                <p className="font-semibold text-green-700 mb-1">Entry:</p>
                <ul className="list-disc pl-4 mb-2">
                  {st.entry.conditions.map((c, j) => (
                    <li key={j}>{c.left.name} {c.operator} {c.right.name || c.right}</li>
                  ))}
                </ul>
                <p className="font-semibold text-red-700 mb-1">Exit:</p>
                <ul className="list-disc pl-4">
                  {st.exit.conditions.map((c, j) => (
                    <li key={j}>{c.left.name} {c.operator} {c.right.name || c.right}</li>
                  ))}
                </ul>
              </div>
              
              <div className="flex space-x-2 mt-auto">
                <button 
                  onClick={() => setSelectedStrategy(st)}
                  className="flex-1 bg-blue-600 hover:bg-blue-700 text-white py-2 rounded flex items-center justify-center font-medium transition"
                >
                  <PlayCircle size={18} className="mr-2"/> Backtest
                </button>
                <button onClick={() => handleDelete(st.name)} className="bg-red-100 hover:bg-red-200 text-red-700 px-3 py-2 rounded transition" title="Delete">
                  <Trash2 size={18} />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

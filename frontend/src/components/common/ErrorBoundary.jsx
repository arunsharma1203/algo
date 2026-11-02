import React from 'react';
import { AlertTriangle, RefreshCw, ChevronDown, ChevronUp, Home } from 'lucide-react';

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
      showDetails: false
    };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error(`[ErrorBoundary caught error in ${this.props.sectionName || 'Component'}]:`, error, errorInfo);
    this.setState({ errorInfo });
  }

  handleReset = () => {
    this.setState({
      hasError: false,
      error: null,
      errorInfo: null,
      showDetails: false
    });
    if (this.props.onReset) {
      this.props.onReset();
    }
  };

  render() {
    if (this.state.hasError) {
      const sectionName = this.props.sectionName || 'This component';
      return (
        <div className="my-4 p-6 bg-slate-900 border border-rose-800/60 rounded-2xl shadow-2xl text-slate-200">
          <div className="flex items-start space-x-4">
            <div className="p-3 bg-rose-500/20 text-rose-400 rounded-xl shrink-0">
              <AlertTriangle size={24} />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-4">
                <h3 className="text-base font-bold text-rose-300">
                  {sectionName} Encountered a Rendering Error
                </h3>
                <div className="flex items-center space-x-2 shrink-0">
                  <button
                    onClick={this.handleReset}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-rose-900/60 hover:bg-rose-800 text-rose-200 rounded-lg text-xs font-bold transition cursor-pointer border border-rose-700/60"
                  >
                    <RefreshCw size={12} />
                    <span>Retry Section</span>
                  </button>
                  <a
                    href="/"
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-bold transition cursor-pointer"
                  >
                    <Home size={12} />
                    <span>Home</span>
                  </a>
                </div>
              </div>
              <p className="text-xs text-slate-400 mt-2">
                The rest of the application remains fully functional. This section was safely isolated to prevent a complete application crash.
              </p>

              <div className="mt-4 p-3 bg-slate-950 border border-slate-800 rounded-xl font-mono text-xs text-rose-400 overflow-x-auto">
                <strong>Error:</strong> {this.state.error?.toString() || 'Unknown runtime error'}
              </div>

              <div className="mt-3">
                <button
                  onClick={() => this.setState({ showDetails: !this.state.showDetails })}
                  className="flex items-center gap-1 text-[11px] text-slate-400 hover:text-slate-200 transition font-mono cursor-pointer"
                >
                  {this.state.showDetails ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                  <span>{this.state.showDetails ? 'Hide technical trace' : 'Show technical trace'}</span>
                </button>
                {this.state.showDetails && (
                  <pre className="mt-2 p-3 bg-slate-950 border border-slate-800/80 rounded-xl text-[10px] text-slate-400 overflow-x-auto max-h-60 custom-scrollbar whitespace-pre-wrap font-mono">
                    {this.state.errorInfo?.componentStack || this.state.error?.stack || 'No stack trace available.'}
                  </pre>
                )}
              </div>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}


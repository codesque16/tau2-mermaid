import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import { Agents } from './pages/Agents'
import { Sessions } from './pages/Sessions'
import { SessionDetail } from './pages/SessionDetail'
import './App.css'

const VIEWER_BASE = '/app/viewer'

function App() {
  return (
    <BrowserRouter basename={VIEWER_BASE}>
      <div className="flex h-screen bg-slate-950 text-slate-100">
        <aside className="w-56 shrink-0 border-r border-slate-800 bg-slate-900/90 flex flex-col">
          <div className="p-4 border-b border-slate-800">
            <h1 className="text-sm font-semibold text-slate-100 tracking-tight">Agent Monitor</h1>
            <p className="text-[10px] text-slate-500 uppercase tracking-wider">Viewer</p>
          </div>
          <nav className="flex-1 py-3 px-3">
            <div className="flex rounded-lg bg-slate-800/80 p-0.5 gap-0.5">
              <NavLink
                to="/"
                end
                className={({ isActive }) =>
                  `flex-1 px-3 py-2 rounded-md text-center text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-slate-600 text-slate-100 shadow-sm'
                      : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
                  }`
                }
              >
                Agents
              </NavLink>
              <NavLink
                to="/sessions"
                className={({ isActive }) =>
                  `flex-1 px-3 py-2 rounded-md text-center text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-slate-600 text-slate-100 shadow-sm'
                      : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'
                  }`
                }
              >
                Sessions
              </NavLink>
            </div>
          </nav>
        </aside>
        <main className="flex-1 min-w-0 flex flex-col overflow-hidden bg-slate-950">
          <Routes>
            <Route path="/" element={<Agents />} />
            <Route path="/sessions" element={<Sessions />} />
            <Route path="/session/:sessionId" element={<SessionDetail />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}

export default App

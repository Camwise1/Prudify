import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, getApiKey, openEventStream, setApiKey } from './lib/api.js'
import { Banner, Icon, Progress, ToastProvider, useToast } from './components/ui.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Library from './pages/Library.jsx'
import Queue from './pages/Queue.jsx'
import Wordlists from './pages/Wordlists.jsx'
import Settings from './pages/Settings.jsx'
import Logs from './pages/Logs.jsx'

const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: 'dashboard' },
  { id: 'library', label: 'Library', icon: 'library' },
  { id: 'queue', label: 'Queue', icon: 'queue' },
  { id: 'wordlists', label: 'Wordlists', icon: 'words' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
  { id: 'logs', label: 'Logs', icon: 'logs' },
]

const TITLES = {
  dashboard: 'Dashboard',
  library: 'Library',
  queue: 'Queue',
  wordlists: 'Wordlists',
  settings: 'Settings',
  logs: 'Logs',
}

function currentRoute() {
  const hash = window.location.hash.replace(/^#\/?/, '')
  return NAV.some((item) => item.id === hash) ? hash : 'dashboard'
}

export default function App() {
  return (
    <ToastProvider>
      <Shell />
    </ToastProvider>
  )
}

function Shell() {
  const toast = useToast()
  const [route, setRoute] = useState(currentRoute)
  const [status, setStatus] = useState(null)
  const [queue, setQueue] = useState(null)
  const [settings, setSettings] = useState(null)
  const [needsKey, setNeedsKey] = useState(false)
  const [connected, setConnected] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const sourceRef = useRef(null)

  const navigate = useCallback((next) => {
    window.location.hash = `#/${next}`
    setRoute(next)
    setSidebarOpen(false)
  }, [])

  useEffect(() => {
    const handler = () => setRoute(currentRoute())
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextQueue] = await Promise.all([api.status(), api.queue()])
      setStatus(nextStatus)
      setQueue(nextQueue)
      setNeedsKey(false)
    } catch (err) {
      if (err.status === 401) setNeedsKey(true)
      else if (err.status === 0) setConnected(false)
    }
  }, [])

  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api.settings())
    } catch (err) {
      if (err.status === 401) setNeedsKey(true)
    }
  }, [])

  useEffect(() => {
    refresh()
    loadSettings()
  }, [refresh, loadSettings])

  // A slow poll is the safety net; SSE does the real-time work.
  useEffect(() => {
    const handle = setInterval(refresh, 15000)
    return () => clearInterval(handle)
  }, [refresh])

  useEffect(() => {
    if (needsKey) return undefined
    const source = openEventStream((type, data) => {
      if (type === 'job.progress') {
        setQueue((current) => {
          if (!current) return current
          const active = (current.active || []).map((job) =>
            job.job_id === data.job_id ? { ...job, ...data } : job,
          )
          return { ...current, active }
        })
        return
      }
      if (type === 'job.finished') {
        toast(`${data.title || 'Book'}: ${data.status}`, data.status === 'completed' ? 'success' : 'error')
      }
      if (type === 'library.scan_complete') {
        setRefreshKey((value) => value + 1)
      }
      refresh()
    })
    source.onopen = () => setConnected(true)
    source.onerror = () => setConnected(false)
    sourceRef.current = source
    return () => source.close()
  }, [needsKey, refresh, toast])

  if (needsKey) {
    return <ApiKeyGate onSaved={() => { refresh(); loadSettings() }} />
  }

  const queueCount = (queue?.active?.length || 0) + (queue?.pending?.length || 0)
  const activeJob = queue?.active?.[0]

  return (
    <div className="app">
      <aside className={`sidebar${sidebarOpen ? ' open' : ''}`}>
        <div className="brand">
          <div className="brand-mark">P</div>
          <div>
            <div className="brand-name">Prudify</div>
            <div className="brand-version">v{status?.version || '—'}</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.id}
              className={`nav-item${route === item.id ? ' active' : ''}`}
              onClick={() => navigate(item.id)}
            >
              <Icon name={item.icon} />
              {item.label}
              {item.id === 'queue' && queueCount ? (
                <span className="nav-badge">{queueCount}</span>
              ) : null}
              {item.id === 'library' && status?.stats?.total_books ? (
                <span className="nav-badge">{status.stats.total_books}</span>
              ) : null}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          {activeJob ? (
            <>
              <div className="truncate">{activeJob.title}</div>
              <Progress value={activeJob.progress} slim />
            </>
          ) : (
            <div>{queue?.paused ? 'Queue paused' : 'Idle'}</div>
          )}
          <div>{connected ? '● live' : '○ reconnecting…'}</div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <button
            className="ghost small"
            style={{ display: 'none' }}
            onClick={() => setSidebarOpen((open) => !open)}
          >
            ☰
          </button>
          <h1>{TITLES[route]}</h1>
          <div className="topbar-actions">
            {status?.free_space_mb != null ? (
              <span className="faint">{Math.round(status.free_space_mb / 1024)} GB free</span>
            ) : null}
            <button className="small" onClick={refresh}>
              <Icon name="refresh" /> Refresh
            </button>
          </div>
        </header>

        {route === 'dashboard' ? (
          <Dashboard status={status} queue={queue} onNavigate={navigate} onRefresh={refresh} />
        ) : null}
        {route === 'library' ? <Library refreshKey={refreshKey} /> : null}
        {route === 'queue' ? <Queue queue={queue} onRefresh={refresh} /> : null}
        {route === 'wordlists' ? (
          <Wordlists settings={settings} onSettingsSaved={loadSettings} />
        ) : null}
        {route === 'settings' ? (
          <Settings
            settings={settings}
            onSaved={() => {
              loadSettings()
              refresh()
            }}
          />
        ) : null}
        {route === 'logs' ? <Logs /> : null}
      </main>
    </div>
  )
}

function ApiKeyGate({ onSaved }) {
  const [key, setKey] = useState(getApiKey())
  const [error, setError] = useState('')

  const submit = async (event) => {
    event.preventDefault()
    setApiKey(key.trim())
    try {
      await api.status()
      onSaved()
    } catch (err) {
      setError(err.status === 401 ? 'That key was not accepted.' : err.message)
    }
  }

  return (
    <div style={{ display: 'grid', placeItems: 'center', minHeight: '100vh', padding: 24 }}>
      <form className="card" style={{ width: 'min(460px, 100%)' }} onSubmit={submit}>
        <div className="brand" style={{ padding: 0, border: 'none', marginBottom: 14 }}>
          <div className="brand-mark">P</div>
          <div className="brand-name">Prudify</div>
        </div>
        <p className="dim" style={{ marginTop: 0 }}>
          Enter the API key printed in the server log on startup. You can also find it in
          <code> config.yaml</code> under <code>server.api_key</code>.
        </p>
        {error ? <Banner tone="error">{error}</Banner> : null}
        <input
          autoFocus
          className="mono"
          placeholder="API key"
          value={key}
          onChange={(event) => setKey(event.target.value)}
        />
        <button className="primary mt" type="submit" style={{ width: '100%' }}>
          Connect
        </button>
      </form>
    </div>
  )
}

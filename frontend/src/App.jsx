import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, getApiKey, openEventStream, setApiKey } from './lib/api.js'
import { Banner, Icon, Progress, ToastProvider, useToast } from './components/ui.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Library from './pages/Library.jsx'
import Queue from './pages/Queue.jsx'
import Wordlists from './pages/Wordlists.jsx'
import Settings from './pages/Settings.jsx'
import Logs from './pages/Logs.jsx'
import Login from './pages/Login.jsx'

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
  const hash = window.location.hash.replace(/^#\/?/, '').split('?')[0]
  return NAV.some((item) => item.id === hash) ? hash : 'dashboard'
}

// A route may carry a filter -- "#/library?status=failed" -- so that the
// dashboard tiles can be links to the thing they are counting rather than
// numbers you then have to go and reproduce by hand.
function routeParams() {
  const query = window.location.hash.split('?')[1] || ''
  return Object.fromEntries(new URLSearchParams(query))
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
  const [params, setParams] = useState(routeParams)
  const [status, setStatus] = useState(null)
  const [queue, setQueue] = useState(null)
  const [settings, setSettings] = useState(null)
  const [auth, setAuth] = useState(null)   // null until /auth/status answers
  const [connected, setConnected] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const sourceRef = useRef(null)

  const navigate = useCallback((next) => {
    window.location.hash = `#/${next}`
    setRoute(next.split('?')[0])
    setParams(routeParams())
    setSidebarOpen(false)
  }, [])

  useEffect(() => {
    const handler = () => {
      setRoute(currentRoute())
      setParams(routeParams())
    }
    window.addEventListener('hashchange', handler)
    return () => window.removeEventListener('hashchange', handler)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextQueue] = await Promise.all([api.status(), api.queue()])
      setStatus(nextStatus)
      setQueue(nextQueue)
      setAuth((current) => (current?.authenticated ? current : { ...current, authenticated: true }))
    } catch (err) {
      if (err.status === 401 || err.status === 503) {
        // Session expired, or the server wants an account created. Re-ask the
        // server what it needs rather than guessing from the status code.
        try {
          setAuth(await api.authStatus())
        } catch {
          setAuth({ authenticated: false, supports_login: true, needs_setup: false })
        }
      } else if (err.status === 0) setConnected(false)
    }
  }, [])

  // Ask the server how it wants to be authenticated before anything else.
  useEffect(() => {
    let cancelled = false
    api
      .authStatus()
      .then((next) => { if (!cancelled) setAuth(next) })
      .catch(() => {
        if (!cancelled) setAuth({ authenticated: false, supports_login: true, needs_setup: false })
      })
    return () => { cancelled = true }
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
    if (!auth?.authenticated) return undefined
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
  }, [auth?.authenticated, refresh, toast])

  // Wait for the server's answer rather than flashing a login form at
  // someone who is already signed in.
  if (auth === null) {
    return <div className="login-shell"><div className="login-card muted">Loading…</div></div>
  }

  if (!auth.authenticated) {
    return (
      <Login
        status={auth}
        onAuthenticated={async () => {
          setAuth(await api.authStatus().catch(() => ({ ...auth, authenticated: true })))
          refresh()
          loadSettings()
        }}
      />
    )
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
          <div className="sidebar-foot-row">
            <span>{connected ? '● live' : '○ reconnecting…'}</span>
            {auth?.supports_login && auth?.username ? (
              <button
                className="linklike"
                title={`Signed in as ${auth.username}`}
                onClick={async () => {
                  try { await api.logout() } catch { /* already gone */ }
                  setApiKey('')
                  setAuth({ ...auth, authenticated: false, username: '' })
                }}
              >
                Sign out
              </button>
            ) : null}
          </div>
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
        {route === 'library' ? (
          <Library refreshKey={refreshKey} initialStatus={params.status || ''} />
        ) : null}
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


import React, { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { STATUS_LABELS } from '../lib/format.js'

/* ---------------- toasts ---------------- */

const ToastContext = createContext(() => {})
export const useToast = () => useContext(ToastContext)

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const push = useCallback((message, tone = 'info') => {
    const id = Math.random().toString(36).slice(2)
    setToasts((current) => [...current, { id, message, tone }])
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 5000)
  }, [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts">
        {toasts.map((toast) => (
          <div key={toast.id} className={`toast ${toast.tone}`}>
            {toast.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

/* ---------------- primitives ---------------- */

export function StatusPill({ status }) {
  const key = String(status || 'new').toLowerCase()
  return <span className={`pill ${key}`}>{STATUS_LABELS[key] || key}</span>
}

export function Progress({ value, slim }) {
  const pct = Math.max(0, Math.min(100, (value || 0) * 100))
  return (
    <div className={`progress${slim ? ' slim' : ''}`}>
      <div style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Stat({ label, value, sub, onClick, title }) {
  // A tile that counts something should be a way of seeing it. Rendered as a
  // real button when it leads somewhere, so it is keyboard reachable and
  // announces itself, rather than a div with a click handler bolted on.
  if (!onClick) {
    return (
      <div className="stat">
        <div className="stat-label">{label}</div>
        <div className="stat-value">{value}</div>
        {sub ? <div className="stat-sub">{sub}</div> : null}
      </div>
    )
  }
  return (
    <button type="button" className="stat stat-link" onClick={onClick} title={title}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="stat-sub">{sub}</div> : null}
    </button>
  )
}

export function BookCover({ bookId, title, size = 48 }) {
  // Artwork is optional and often absent, so the initial is the resting state
  // and the image is layered over it once it loads. That way nothing shifts
  // when a cover arrives late, and a book without one looks deliberate rather
  // than broken.
  const [loaded, setLoaded] = React.useState(false)
  const initial = (title || '?').trim().charAt(0).toUpperCase()

  return (
    <div className="cover" style={{ width: size, height: size }} aria-hidden="true">
      <span className="cover-initial">{initial}</span>
      <img
        src={api.coverUrl(bookId)}
        alt=""
        loading="lazy"
        className={loaded ? 'cover-img loaded' : 'cover-img'}
        onLoad={(event) => {
          // The blank pixel means "no artwork" -- do not fade it in over the
          // initial, or every coverless book shows an empty square instead.
          if (event.target.naturalWidth > 2) setLoaded(true)
        }}
        onError={() => setLoaded(false)}
      />
    </div>
  )
}

export function Empty({ title, children }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      <div>{children}</div>
    </div>
  )
}

export function Banner({ tone = 'info', children }) {
  return <div className={`banner ${tone}`}>{children}</div>
}

export function Modal({ title, onClose, children, footer, wide }) {
  useEffect(() => {
    const handler = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        style={wide ? { width: 'min(1080px, 100%)' } : undefined}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-head">
          <h2 style={{ fontSize: 16 }}>{title}</h2>
          <button className="ghost spacer" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  )
}

export function Field({ label, hint, children }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint ? <span className="hint">{hint}</span> : null}
    </label>
  )
}

export function Check({ label, hint, checked, onChange }) {
  return (
    <label className="check">
      <input type="checkbox" checked={!!checked} onChange={(e) => onChange(e.target.checked)} />
      <span>
        {label}
        {hint ? <span className="hint">{hint}</span> : null}
      </span>
    </label>
  )
}

export function Tabs({ tabs, active, onChange }) {
  return (
    <div className="tabs">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          className={`tab${active === tab.id ? ' active' : ''}`}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  )
}

/* ---------------- icons ---------------- */

const PATHS = {
  dashboard: 'M4 13h7V4H4v9zm0 7h7v-5H4v5zm9 0h7V11h-7v9zm0-16v5h7V4h-7z',
  library: 'M4 4h4v16H4V4zm6 0h4v16h-4V4zm7.2.4l3.6 15.3-3.9.9L13.3 5.3l3.9-.9z',
  queue: 'M4 6h16v2H4V6zm0 5h16v2H4v-2zm0 5h10v2H4v-2z',
  words: 'M5 4h14v2H5V4zm0 5h14v2H5V9zm0 5h9v2H5v-2zm0 5h9v2H5v-2z',
  settings:
    'M12 8a4 4 0 100 8 4 4 0 000-8zm8.9 4a7 7 0 01-.1 1.2l2 1.6-1.9 3.3-2.4-1a7.6 7.6 0 01-2 1.2l-.4 2.6h-3.8l-.4-2.6a7.6 7.6 0 01-2-1.2l-2.4 1L3.2 14l2-1.6A7 7 0 015 12a7 7 0 01.1-1.2l-2-1.6 1.9-3.3 2.4 1a7.6 7.6 0 012-1.2l.4-2.6h3.8l.4 2.6c.7.3 1.4.7 2 1.2l2.4-1 1.9 3.3-2 1.6c.1.4.1.8.1 1.2z',
  logs: 'M5 3h9l5 5v13H5V3zm8 1.5V9h4.5L13 4.5zM7 12h10v2H7v-2zm0 4h10v2H7v-2z',
  play: 'M8 5v14l11-7z',
  pause: 'M6 5h4v14H6V5zm8 0h4v14h-4V5z',
  refresh: 'M12 5V2L8 6l4 4V7a5 5 0 11-5 5H5a7 7 0 107-7z',
}

export function Icon({ name, size = 16 }) {
  const d = PATHS[name] || PATHS.dashboard
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d={d} />
    </svg>
  )
}

/* ---------------- filesystem picker ---------------- */

export function PathBrowser({ value, onPick, api }) {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState({ path: '', entries: [], roots: [], parent: null })
  const [loading, setLoading] = useState(false)

  const load = useCallback(
    async (path) => {
      setLoading(true)
      try {
        setState(await api.browse(path))
      } catch (err) {
        setState((s) => ({ ...s, error: err.message }))
      } finally {
        setLoading(false)
      }
    },
    [api],
  )

  useEffect(() => {
    if (open) load(value || '')
  }, [open, load, value])

  if (!open) {
    return (
      <button type="button" className="small" onClick={() => setOpen(true)}>
        Browse…
      </button>
    )
  }

  return (
    <Modal
      title="Choose a folder"
      onClose={() => setOpen(false)}
      footer={
        <>
          <button onClick={() => setOpen(false)}>Cancel</button>
          <button
            className="primary"
            disabled={!state.path}
            onClick={() => {
              onPick(state.path)
              setOpen(false)
            }}
          >
            Use this folder
          </button>
        </>
      }
    >
      <div className="flex wrap mb">
        {(state.roots || []).map((root) => (
          <button key={root.path} className="small" onClick={() => load(root.path)}>
            {root.name}
          </button>
        ))}
      </div>
      <div className="mono dim mb">{state.path || 'Pick a starting point above'}</div>
      {state.audio_file_count ? (
        <Banner tone="info">
          {state.audio_file_count} audio file{state.audio_file_count === 1 ? '' : 's'} directly in
          this folder.
        </Banner>
      ) : null}
      <div className="browser-list">
        {state.parent ? (
          <div className="browser-item" onClick={() => load(state.parent)}>
            ← ..
          </div>
        ) : null}
        {loading ? <div className="browser-item faint">Loading…</div> : null}
        {(state.entries || []).map((entry) => (
          <div key={entry.path} className="browser-item" onClick={() => load(entry.path)}>
            📁 {entry.name}
          </div>
        ))}
        {!loading && state.path && (state.entries || []).length === 0 ? (
          <div className="browser-item faint">No subfolders here.</div>
        ) : null}
      </div>
    </Modal>
  )
}

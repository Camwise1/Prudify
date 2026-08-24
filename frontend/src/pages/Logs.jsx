import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api.js'
import { Empty, useToast } from '../components/ui.jsx'

export default function Logs() {
  const toast = useToast()
  const [entries, setEntries] = useState([])
  const [level, setLevel] = useState('')
  const [filter, setFilter] = useState('')
  const [follow, setFollow] = useState(true)
  const bottom = useRef(null)

  const load = useCallback(async () => {
    try {
      const params = { limit: 400 }
      if (level) params.level = level
      const rows = await api.logs(params)
      setEntries(rows.slice().reverse())
    } catch (err) {
      toast(err.message, 'error')
    }
  }, [level, toast])

  useEffect(() => {
    load()
    if (!follow) return undefined
    const handle = setInterval(load, 4000)
    return () => clearInterval(handle)
  }, [load, follow])

  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView({ block: 'end' })
  }, [entries, follow])

  const visible = filter
    ? entries.filter((entry) => entry.message.toLowerCase().includes(filter.toLowerCase()))
    : entries

  return (
    <div className="page">
      <div className="toolbar">
        <input
          className="search"
          placeholder="Filter messages…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <select value={level} onChange={(event) => setLevel(event.target.value)}>
          <option value="">All levels</option>
          <option value="ERROR,CRITICAL">Errors</option>
          <option value="WARNING,ERROR,CRITICAL">Warnings and above</option>
          <option value="INFO,WARNING,ERROR,CRITICAL">Info and above</option>
          <option value="DEBUG">Debug only</option>
        </select>
        <label className="check" style={{ margin: 0 }}>
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
          <span>Follow</span>
        </label>
        <div className="spacer" />
        <button onClick={load}>Refresh</button>
        <button
          className="danger"
          onClick={async () => {
            if (!window.confirm('Clear all stored log entries?')) return
            await api.clearLogs()
            toast('Logs cleared', 'success')
            load()
          }}
        >
          Clear
        </button>
      </div>

      <div className="card">
        {visible.length === 0 ? (
          <Empty title="No log entries" />
        ) : (
          <div className="log-list">
            {visible.map((entry) => (
              <div key={entry.id} className={`log-line ${entry.level}`}>
                <span className="ts">{new Date(entry.created_at).toLocaleTimeString()}</span>
                <span className="lvl">{entry.level}</span>
                <span>{entry.message}</span>
              </div>
            ))}
            <div ref={bottom} />
          </div>
        )}
      </div>
    </div>
  )
}

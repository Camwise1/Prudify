import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { formatBytes } from '../lib/format.js'
import { Empty, useToast } from '../components/ui.jsx'

/**
 * A library is not a flat list of books to the person who owns it. It is a
 * list of authors, some of whom need cleaning and some of whom emphatically
 * do not -- and deciding that one author at a time is the actual task. Paging
 * through three hundred titles to find the twelve by one writer is not.
 */
export default function Authors({ onOpenAuthor, refreshKey }) {
  const toast = useToast()
  const [authors, setAuthors] = useState([])
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('')
  const [tick, setTick] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setAuthors(await api.authors())
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    load()
  }, [load, tick, refreshKey])

  const queueAuthor = async (event, author) => {
    event.stopPropagation()
    setBusy(author.author)
    try {
      const result = await api.queueAll(null, author.author)
      toast(`Queued ${result.queued} book(s) by ${author.author}`, 'success')
      setTick((value) => value + 1)
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setBusy('')
    }
  }

  const needle = query.trim().toLowerCase()
  const shown = needle
    ? authors.filter((entry) => entry.author.toLowerCase().includes(needle))
    : authors

  return (
    <div className="page">
      <div className="toolbar">
        <input
          className="search"
          placeholder="Search authors…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="spacer" />
        <span className="faint">{shown.length} author(s)</span>
      </div>

      {loading ? (
        <div className="card">Loading…</div>
      ) : shown.length === 0 ? (
        <Empty title="No authors found">
          {authors.length ? 'Nothing matches that search.' : 'Scan a library first.'}
        </Empty>
      ) : (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>Author</th>
                <th className="num">Books</th>
                <th className="num">Cleaned</th>
                <th className="num">Waiting</th>
                <th className="num">Size</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {shown.map((entry) => (
                <tr
                  key={entry.author}
                  className="clickable"
                  onClick={() => onOpenAuthor(entry.author)}
                >
                  <td>
                    <div className="cell-title truncate">{entry.author}</div>
                  </td>
                  <td className="num">{entry.count}</td>
                  <td className="num dim">{entry.cleaned}</td>
                  <td className="num">{entry.pending || '—'}</td>
                  <td className="num dim">{formatBytes(entry.total_bytes)}</td>
                  <td className="right">
                    <button
                      className="small primary"
                      disabled={!entry.pending || busy === entry.author}
                      title={
                        entry.pending
                          ? `Clean all ${entry.pending} waiting book(s)`
                          : 'Nothing waiting for this author'
                      }
                      onClick={(event) => queueAuthor(event, entry)}
                    >
                      {busy === entry.author ? 'Queueing…' : 'Clean all'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

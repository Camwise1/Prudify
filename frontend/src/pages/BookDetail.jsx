import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { formatBytes, formatClock, formatDuration } from '../lib/format.js'
import { Empty, Modal, StatusPill, Tabs, useToast } from '../components/ui.jsx'

export default function BookDetail({ bookId, onClose, onChanged }) {
  const toast = useToast()
  const [book, setBook] = useState(null)
  const [matches, setMatches] = useState(null)
  const [tab, setTab] = useState('overview')
  const [busy, setBusy] = useState(false)

  const load = async () => {
    try {
      const detail = await api.book(bookId)
      setBook(detail)
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  useEffect(() => {
    load()
  }, [bookId])

  useEffect(() => {
    if (tab === 'matches' && !matches) {
      api
        .bookMatches(bookId)
        .then(setMatches)
        .catch((err) => toast(err.message, 'error'))
    }
  }, [tab, bookId, matches])

  const act = async (fn, message) => {
    setBusy(true)
    try {
      await fn()
      toast(message, 'success')
      await load()
      onChanged?.()
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  if (!book) {
    return (
      <Modal title="Loading…" onClose={onClose}>
        <div className="faint">Fetching book details…</div>
      </Modal>
    )
  }

  const words = Object.entries(book.word_counts || {})
  const totalDuration = (book.parts || []).reduce((sum, part) => sum + (part.duration || 0), 0)

  return (
    <Modal
      wide
      title={book.title}
      onClose={onClose}
      footer={
        <>
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              act(
                () => api.resetBook(book.id, { deleteOutput: true }),
                'Cleaned copy deleted and book reset',
              )
            }
          >
            Delete clean copy & reset
          </button>
          <button
            disabled={busy}
            onClick={() =>
              act(() => api.setMonitored(book.id, !book.monitored), 'Monitoring updated')
            }
          >
            {book.monitored ? 'Stop monitoring' : 'Monitor'}
          </button>
          <button
            className="primary"
            disabled={busy}
            onClick={() => act(() => api.queueBook(book.id), 'Added to the queue')}
          >
            Clean now
          </button>
        </>
      }
    >
      <div className="flex wrap mb">
        <StatusPill status={book.status} />
        <span className="dim">{book.author || 'Unknown author'}</span>
        <span className="faint">·</span>
        <span className="dim">
          {book.part_count} part{book.part_count === 1 ? '' : 's'}
        </span>
        <span className="faint">·</span>
        <span className="dim">{formatBytes(book.total_bytes)}</span>
        {book.formats?.map((format) => (
          <span key={format} className="tag">
            {format}
          </span>
        ))}
      </div>

      {book.error ? (
        <div className="banner error mb">
          <div>{book.error}</div>
        </div>
      ) : null}

      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { id: 'overview', label: 'Overview' },
          { id: 'parts', label: `Files (${book.part_count})` },
          { id: 'matches', label: 'Detected words' },
        ]}
      />

      {tab === 'overview' ? (
        <>
          <div className="grid cols-4 mb">
            <div className="stat">
              <div className="stat-label">Instances</div>
              <div className="stat-value">{book.match_count}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Audio muted</div>
              <div className="stat-value">{formatDuration(book.muted_seconds)}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Running time</div>
              <div className="stat-value">{formatDuration(totalDuration)}</div>
            </div>
            <div className="stat">
              <div className="stat-label">Density</div>
              <div className="stat-value">
                {totalDuration
                  ? (book.match_count / (totalDuration / 3600)).toFixed(1)
                  : '—'}
              </div>
              <div className="stat-sub">per hour</div>
            </div>
          </div>

          {words.length ? (
            <>
              <div className="card-head">
                <h2>Breakdown</h2>
              </div>
              <div className="word-cloud mb">
                {words.map(([word, count]) => (
                  <span key={word} className="word-chip">
                    {word} <b>{count}</b>
                  </span>
                ))}
              </div>
            </>
          ) : null}

          <div className="card-head">
            <h2>Paths</h2>
          </div>
          <div className="mono faint" style={{ wordBreak: 'break-all' }}>
            <div>Source: {book.folder}</div>
            {book.parts?.[0]?.destination ? <div>Output: {book.parts[0].destination}</div> : null}
          </div>
        </>
      ) : null}

      {tab === 'parts' ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>File</th>
                <th>Status</th>
                <th className="num">Length</th>
                <th className="num">Size</th>
                <th className="num">Hits</th>
                <th className="num">Muted</th>
              </tr>
            </thead>
            <tbody>
              {(book.parts || []).map((part) => (
                <tr key={part.id}>
                  <td>
                    <div className="truncate">{part.relative_path.split('/').pop()}</div>
                    {part.error ? <div className="cell-sub">{part.error}</div> : null}
                  </td>
                  <td>
                    <StatusPill status={part.status} />
                  </td>
                  <td className="num">{formatDuration(part.duration)}</td>
                  <td className="num">{formatBytes(part.size_bytes)}</td>
                  <td className="num">{part.match_count || 0}</td>
                  <td className="num">{part.muted_seconds ? `${part.muted_seconds.toFixed(1)}s` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {tab === 'matches' ? (
        matches === null ? (
          <div className="faint">Loading matches…</div>
        ) : matches.total === 0 ? (
          <Empty title="Nothing detected yet">
            Matches appear here after the book has been processed.
          </Empty>
        ) : (
          <>
            <div className="card-head">
              <h2>Timeline</h2>
              <span className="spacer faint">{matches.total} instances</span>
            </div>
            <div className="timeline mb">
              {matches.matches.map((match, index) => (
                <div
                  key={index}
                  className="timeline-mark"
                  title={`${match.text} @ ${formatClock(match.absolute_start)}`}
                  style={{
                    left: `${matches.duration ? (match.absolute_start / matches.duration) * 100 : 0}%`,
                  }}
                />
              ))}
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="num">Time</th>
                    <th>Word</th>
                    <th>Rule</th>
                    <th className="num">Confidence</th>
                    <th>File</th>
                  </tr>
                </thead>
                <tbody>
                  {matches.matches.slice(0, 500).map((match, index) => (
                    <tr key={index}>
                      <td className="num mono">{formatClock(match.absolute_start)}</td>
                      <td>{match.text}</td>
                      <td>
                        <span className="tag">{match.rule}</span>
                      </td>
                      <td className="num">
                        {match.confidence != null ? `${Math.round(match.confidence * 100)}%` : '—'}
                      </td>
                      <td className="faint truncate">{match.part?.split('/').pop()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )
      ) : null}
    </Modal>
  )
}

import React, { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { formatBytes, formatDuration, relativeTime } from '../lib/format.js'
import { BookCover, Empty, StatusPill, useToast } from '../components/ui.jsx'
import BookDetail from './BookDetail.jsx'

const PAGE_SIZE = 50

export default function Library({
  refreshKey,
  initialStatus = '',
  initialAuthor = '',
  initialBook = '',
}) {
  const toast = useToast()
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState(initialStatus)
  const [author, setAuthor] = useState(initialAuthor)
  const [sort, setSort] = useState('author')
  const [order, setOrder] = useState('asc')
  const [page, setPage] = useState(1)
  const [data, setData] = useState({ items: [], total: 0 })
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(null)
  const [tick, setTick] = useState(0)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const params = { page, page_size: PAGE_SIZE, sort, order }
      if (query.trim()) params.q = query.trim()
      if (status) params.status = status
      if (author) params.author = author
      setData(await api.books(params))
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setLoading(false)
    }
  }, [page, sort, order, query, status, author, toast])

  // Arriving from a dashboard tile or an author changes the filter under us;
  // the page resets too, or you land on page 3 of a list that now has one.
  useEffect(() => {
    setStatus(initialStatus)
    setAuthor(initialAuthor)
    setPage(1)
  }, [initialStatus, initialAuthor])

  // A link that names a book opens it, which is what lets every list in the
  // app -- not just this table -- lead to the detail view.
  useEffect(() => {
    if (initialBook) setSelected(initialBook)
  }, [initialBook])

  useEffect(() => {
    const handle = setTimeout(load, query ? 250 : 0)
    return () => clearTimeout(handle)
  }, [load, tick, refreshKey])

  const toggleSort = (column) => {
    if (sort === column) setOrder(order === 'asc' ? 'desc' : 'asc')
    else {
      setSort(column)
      setOrder('asc')
    }
    setPage(1)
  }

  const queueBook = async (event, book) => {
    event.stopPropagation()
    try {
      await api.queueBook(book.id)
      toast(`Queued “${book.title}”`, 'success')
      setTick((t) => t + 1)
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const pages = Math.max(1, Math.ceil(data.total / PAGE_SIZE))

  return (
    <div className="page">
      <div className="toolbar">
        <input
          className="search"
          placeholder="Search title or author…"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            setPage(1)
          }}
        />
        <select
          value={status}
          onChange={(event) => {
            setStatus(event.target.value)
            setPage(1)
          }}
        >
          <option value="">All statuses</option>
          <option value="new">Not cleaned</option>
          <option value="queued,processing">In progress</option>
          <option value="cleaned">Cleaned</option>
          <option value="partial">Partial</option>
          <option value="failed">Failed</option>
          <option value="ignored">Ignored</option>
          <option value="missing">Missing</option>
        </select>
        {author ? (
          <button
            className="small"
            title="Show every author again"
            onClick={() => {
              setAuthor('')
              setPage(1)
            }}
          >
            {author} ✕
          </button>
        ) : null}
        <div className="spacer" />
        <span className="faint">{data.total} book(s)</span>
        <button
          className="small"
          onClick={async () => {
            try {
              await api.rescan()
              toast('Scan complete', 'success')
              setTick((t) => t + 1)
            } catch (err) {
              toast(err.message, 'error')
            }
          }}
        >
          Rescan
        </button>
        <button
          className="small primary"
          onClick={async () => {
            try {
              const result = await api.queueAll(null, author || undefined)
              toast(`Queued ${result.queued} book(s)`, 'success')
              setTick((t) => t + 1)
            } catch (err) {
              toast(err.message, 'error')
            }
          }}
        >
          {author ? `Queue all by ${author}` : 'Queue all pending'}
        </button>
      </div>

      {data.items.length === 0 && !loading ? (
        <Empty title="No books match">
          Add a library in Settings, then run a scan.
        </Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="sortable" onClick={() => toggleSort('title')}>
                  Title
                </th>
                <th className="sortable" onClick={() => toggleSort('author')}>
                  Author
                </th>
                <th className="sortable" onClick={() => toggleSort('status')}>
                  Status
                </th>
                <th>Format</th>
                <th className="num sortable" onClick={() => toggleSort('matches')}>
                  Hits
                </th>
                <th className="num">Muted</th>
                <th className="num sortable" onClick={() => toggleSort('size')}>
                  Size
                </th>
                <th className="num sortable" onClick={() => toggleSort('added')}>
                  Added
                </th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.items.map((book) => (
                <tr key={book.id} className="clickable" onClick={() => setSelected(book.id)}>
                  <td>
                    <div className="cover-row">
                      <BookCover bookId={book.id} title={book.title} size={44} />
                      <div>
                        <div className="cell-title truncate">{book.title}</div>
                        {book.part_count > 1 ? (
                          <div className="cell-sub">{book.part_count} files</div>
                        ) : null}
                      </div>
                    </div>
                  </td>
                  <td className="dim truncate">
                    {book.author ? (
                      <button
                        className="linkish"
                        title={`Show only ${book.author}`}
                        onClick={(event) => {
                          event.stopPropagation()
                          setAuthor(book.author)
                          setPage(1)
                        }}
                      >
                        {book.author}
                      </button>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    <StatusPill status={book.status} />
                  </td>
                  <td>
                    {(book.formats || []).map((format) => (
                      <span key={format} className="tag">
                        {format.replace('.', '')}
                      </span>
                    ))}
                  </td>
                  <td className="num">{book.match_count || '—'}</td>
                  <td className="num">
                    {book.muted_seconds ? formatDuration(book.muted_seconds) : '—'}
                  </td>
                  <td className="num">{formatBytes(book.total_bytes)}</td>
                  <td className="num faint nowrap">{relativeTime(book.first_seen)}</td>
                  <td className="right">
                    <button className="small" onClick={(event) => queueBook(event, book)}>
                      Clean
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pages > 1 ? (
        <div className="flex mt">
          <button disabled={page <= 1} onClick={() => setPage(page - 1)}>
            ← Previous
          </button>
          <span className="dim">
            Page {page} of {pages}
          </span>
          <button disabled={page >= pages} onClick={() => setPage(page + 1)}>
            Next →
          </button>
        </div>
      ) : null}

      {selected ? (
        <BookDetail
          bookId={selected}
          onClose={() => setSelected(null)}
          onChanged={() => setTick((t) => t + 1)}
        />
      ) : null}
    </div>
  )
}

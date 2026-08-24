import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { formatBytes, formatDuration, relativeTime } from '../lib/format.js'
import { Banner, Empty, Progress, Stat, StatusPill, useToast } from '../components/ui.jsx'

export default function Dashboard({ status, queue, onNavigate, onRefresh }) {
  const toast = useToast()
  const [recentBooks, setRecentBooks] = useState([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .books({ page_size: 8, sort: 'added', order: 'desc' })
      .then((page) => setRecentBooks(page.items || []))
      .catch(() => {})
  }, [status])

  const stats = status?.stats || {}
  const byStatus = stats.by_status || {}
  const active = queue?.active || []
  const pending = queue?.pending || []

  const scan = async () => {
    setBusy(true)
    try {
      await api.scanNow()
      toast('Library scan started', 'success')
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setBusy(false)
      onRefresh()
    }
  }

  const queueAll = async () => {
    setBusy(true)
    try {
      const result = await api.queueAll()
      toast(`Queued ${result.queued} book${result.queued === 1 ? '' : 's'}`, 'success')
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setBusy(false)
      onRefresh()
    }
  }

  return (
    <div className="page">
      {status && !status.transcription_available ? (
        <Banner tone="warn">
          <div>
            <b>Transcription is unavailable.</b> {status.transcription_detail}
          </div>
        </Banner>
      ) : null}
      {status && !status.ffprobe_available ? (
        <Banner tone="error">
          <div>
            <b>FFmpeg was not found.</b> Prudify cannot read or write audio without it.
          </div>
        </Banner>
      ) : null}
      {status && status.libraries === 0 ? (
        <Banner tone="info">
          <div>No libraries yet — add one in Settings to start monitoring for new books.</div>
          <button className="small spacer" onClick={() => onNavigate('settings')}>
            Open settings
          </button>
        </Banner>
      ) : null}

      <div className="grid cols-4 mb">
        <Stat
          label="Books"
          value={stats.total_books ?? 0}
          sub={`${formatBytes(stats.total_bytes)} of source audio`}
        />
        <Stat
          label="Cleaned"
          value={byStatus.cleaned ?? 0}
          sub={`${byStatus.new ?? 0} waiting, ${byStatus.failed ?? 0} failed`}
        />
        <Stat
          label="Instances silenced"
          value={(stats.total_matches ?? 0).toLocaleString()}
          sub={`${formatDuration(stats.total_muted_seconds)} of audio muted`}
        />
        <Stat
          label="Queue"
          value={active.length + pending.length}
          sub={queue?.paused ? 'Paused' : `${active.length} running, ${pending.length} waiting`}
        />
      </div>

      <div className="card mb">
        <div className="card-head">
          <h2>Now processing</h2>
          <div className="spacer" />
          <button className="small" onClick={scan} disabled={busy}>
            Scan libraries
          </button>
          <button className="small primary" onClick={queueAll} disabled={busy}>
            Queue everything
          </button>
        </div>
        {active.length === 0 ? (
          <Empty title="Nothing running">
            {pending.length
              ? `${pending.length} book(s) waiting in the queue.`
              : 'Add books to your library or queue something manually.'}
          </Empty>
        ) : (
          active.map((job) => (
            <div key={job.job_id} className="active-job mb">
              <div className="active-job-head">
                <b>{job.title}</b>
                <span className="faint">{job.author}</span>
                <span className="pct">{Math.round((job.progress || 0) * 100)}%</span>
              </div>
              <Progress value={job.progress} />
              <div className="flex faint" style={{ fontSize: 12 }}>
                <span>{job.message || job.stage}</span>
                {job.part_total > 1 ? (
                  <span className="spacer">
                    part {job.part_index} of {job.part_total}
                  </span>
                ) : null}
              </div>
            </div>
          ))
        )}
      </div>

      <div className="grid cols-2">
        <div className="card">
          <div className="card-head">
            <h2>Up next</h2>
            <button className="ghost small spacer" onClick={() => onNavigate('queue')}>
              View queue →
            </button>
          </div>
          {pending.length === 0 ? (
            <Empty title="Queue is empty" />
          ) : (
            <table>
              <tbody>
                {pending.slice(0, 8).map((job) => (
                  <tr key={job.id}>
                    <td>
                      <div className="cell-title">{job.book_title}</div>
                      <div className="cell-sub">{job.book_author}</div>
                    </td>
                    <td className="right nowrap faint">{relativeTime(job.queued_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Recently added</h2>
            <button className="ghost small spacer" onClick={() => onNavigate('library')}>
              View library →
            </button>
          </div>
          {recentBooks.length === 0 ? (
            <Empty title="No books found yet">
              Run a scan once a library is configured.
            </Empty>
          ) : (
            <table>
              <tbody>
                {recentBooks.map((book) => (
                  <tr key={book.id}>
                    <td>
                      <div className="cell-title truncate">{book.title}</div>
                      <div className="cell-sub">{book.author || 'Unknown author'}</div>
                    </td>
                    <td className="right">
                      <StatusPill status={book.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}

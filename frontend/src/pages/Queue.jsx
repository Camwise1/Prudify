import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { formatDuration, jobActivity, relativeTime } from '../lib/format.js'
import { Empty, Icon, Progress, StatusPill, Tabs, useToast } from '../components/ui.jsx'

export default function Queue({ queue, onRefresh }) {
  const toast = useToast()
  const [tab, setTab] = useState('current')
  const [history, setHistory] = useState([])

  useEffect(() => {
    if (tab === 'history') {
      api
        .queueHistory({ page_size: 100 })
        .then((page) => setHistory(page.items || []))
        .catch((err) => toast(err.message, 'error'))
    }
  }, [tab])

  const wrap = async (fn, message) => {
    try {
      await fn()
      if (message) toast(message, 'success')
      onRefresh()
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const active = queue?.active || []
  const pending = queue?.pending || []
  const recent = queue?.recent || []

  return (
    <div className="page">
      <div className="toolbar">
        {queue?.paused ? (
          <button className="primary" onClick={() => wrap(api.resume, 'Queue resumed')}>
            <Icon name="play" /> Resume queue
          </button>
        ) : (
          <button onClick={() => wrap(api.pause, 'Queue paused')}>
            <Icon name="pause" /> Pause queue
          </button>
        )}
        <button
          className="danger"
          disabled={!pending.length}
          onClick={() => wrap(api.clearQueue, 'Pending jobs cleared')}
        >
          Clear pending
        </button>
        <div className="spacer" />
        {queue?.paused ? <span className="pill partial">Paused</span> : null}
      </div>

      <Tabs
        active={tab}
        onChange={setTab}
        tabs={[
          { id: 'current', label: `Queue (${active.length + pending.length})` },
          { id: 'history', label: 'History' },
        ]}
      />

      {tab === 'current' ? (
        <>
          <div className="card mb">
            <div className="card-head">
              <h2>Active</h2>
            </div>
            {active.length === 0 ? (
              <Empty title="Nothing running" />
            ) : (
              active.map((job) => (
                <div key={job.job_id} className="active-job mb">
                  <div className="active-job-head">
                    <b>{job.title}</b>
                    <span className="faint">{job.author}</span>
                    <span className="pct">{Math.round((job.progress || 0) * 100)}%</span>
                    <button
                      className="small danger"
                      onClick={() => wrap(() => api.cancelJob(job.job_id), 'Cancelling…')}
                    >
                      Cancel
                    </button>
                  </div>
                  <Progress value={job.progress} />
                  <div className="flex faint job-timing" style={{ fontSize: 12 }}>
                    <span>{jobActivity(job)}</span>
                    {job.part_total > 1 ? (
                      <span className="spacer">
                        part {job.part_index} / {job.part_total}
                      </span>
                    ) : null}
                  </div>
                </div>
              ))
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <h2>Pending ({pending.length})</h2>
            </div>
            {pending.length === 0 ? (
              <Empty title="Queue is empty">
                Queue books from the Library page, or let the watcher pick up new arrivals.
              </Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Book</th>
                      <th>Author</th>
                      <th className="num">Queued</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map((job) => (
                      <tr key={job.id}>
                        <td className="cell-title">{job.book_title}</td>
                        <td className="dim">{job.book_author}</td>
                        <td className="num faint nowrap">{relativeTime(job.queued_at)}</td>
                        <td className="right">
                          <button
                            className="small danger"
                            onClick={() => wrap(() => api.cancelJob(job.id), 'Removed')}
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {recent.length ? (
            <div className="card mt">
              <div className="card-head">
                <h2>Just finished</h2>
              </div>
              <table>
                <tbody>
                  {recent.slice(0, 8).map((job) => (
                    <tr key={job.id}>
                      <td className="cell-title">{job.book_title}</td>
                      <td>
                        <StatusPill status={job.status} />
                      </td>
                      <td className="dim">{job.message || job.error}</td>
                      <td className="num faint nowrap">{relativeTime(job.finished_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Book</th>
                <th>Status</th>
                <th className="num">Hits</th>
                <th className="num">Muted</th>
                <th>Outcome</th>
                <th className="num">Finished</th>
              </tr>
            </thead>
            <tbody>
              {history.map((job) => (
                <tr key={job.id}>
                  <td>
                    <div className="cell-title truncate">{job.book_title}</div>
                    <div className="cell-sub">{job.book_author}</div>
                  </td>
                  <td>
                    <StatusPill status={job.status} />
                  </td>
                  <td className="num">{job.result?.matches ?? '—'}</td>
                  <td className="num">
                    {job.result?.muted_seconds ? formatDuration(job.result.muted_seconds) : '—'}
                  </td>
                  <td className="dim truncate">{job.message || job.error || '—'}</td>
                  <td className="num faint nowrap">{relativeTime(job.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {history.length === 0 ? <Empty title="No history yet" /> : null}
        </div>
      )}
    </div>
  )
}

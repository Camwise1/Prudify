import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { clockIn, formatDuration, jobActivity, relativeTime } from '../lib/format.js'
import { Empty, Icon, Progress, StatusPill, Tabs, useToast } from '../components/ui.jsx'
import JobDetail from './JobDetail.jsx'

export default function Queue({ queue, onRefresh }) {
  const toast = useToast()
  const [tab, setTab] = useState('current')
  const [history, setHistory] = useState([])
  const [openJob, setOpenJob] = useState(null)

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
  const estimate = queue?.estimate || {}

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
                    {job.mode === 'copy' ? <span className="tag">copy only</span> : null}
                    <span className="pct">{Math.round((job.progress || 0) * 100)}%</span>
                    <button className="small" onClick={() => setOpenJob(job.job_id)}>
                      Details
                    </button>
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
              <QueueEstimate estimate={estimate} count={pending.length + active.length} />
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
                      <th className="num">Length</th>
                      <th className="num">Est.</th>
                      <th className="num">Queued</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map((job) => (
                      <tr key={job.id} className="clickable" onClick={() => setOpenJob(job.id)}>
                        <td className="cell-title">
                          {job.book_title}
                          {job.result?.mode === 'copy' ? (
                            <span className="tag" style={{ marginLeft: 8 }}>copy only</span>
                          ) : null}
                        </td>
                        <td className="dim">{job.book_author}</td>
                        <td className="num dim nowrap">
                          {job.book_duration ? formatDuration(job.book_duration) : '…'}
                        </td>
                        <td className="num dim nowrap">
                          {job.estimate_seconds ? formatDuration(job.estimate_seconds) : '—'}
                        </td>
                        <td className="num faint nowrap">{relativeTime(job.queued_at)}</td>
                        <td className="right nowrap" onClick={(event) => event.stopPropagation()}>
                          {job.result?.mode !== 'copy' ? (
                            <button
                              className="small"
                              title="Skip transcription and copy this book across unchanged"
                              onClick={() =>
                                wrap(() => api.markClean(job.book_id), 'Will copy unchanged')
                              }
                            >
                              Mark clean
                            </button>
                          ) : null}{' '}
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
                    <tr key={job.id} className="clickable" onClick={() => setOpenJob(job.id)}>
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
                <tr key={job.id} className="clickable" onClick={() => setOpenJob(job.id)}>
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

      {openJob ? (
        <JobDetail
          jobId={openJob}
          live={active.find((job) => job.job_id === openJob)}
          onClose={() => setOpenJob(null)}
          onChanged={onRefresh}
        />
      ) : null}
    </div>
  )
}

function QueueEstimate({ estimate, count }) {
  // Only claim a finish time the numbers support. Books not yet measured
  // turn it into "at least", and with nothing measured it says so.
  if (!count) return null
  const known = estimate.seconds > 0
  const unknown = estimate.unknown_jobs || 0
  const audio = estimate.pending_audio_seconds
  const bits = []
  if (audio) bits.push(`${formatDuration(audio)} of audio waiting`)
  if (known) {
    bits.push(
      unknown
        ? `at least ${formatDuration(estimate.seconds)} (${unknown} not yet estimated)`
        : `all done ${clockIn(estimate.seconds)}`,
    )
  } else if (unknown) {
    bits.push('finish time appears once the first book has been timed end to end')
  }
  return <span className="faint spacer">{bits.join(' · ')}</span>
}

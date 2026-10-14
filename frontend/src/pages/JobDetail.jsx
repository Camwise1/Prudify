import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import {
  clockIn,
  formatBytes,
  formatDuration,
  formatSpeed,
  parseDate,
  relativeTime,
} from '../lib/format.js'
import { Modal, Progress, Stat, StatusPill, useToast } from '../components/ui.jsx'

const STAGES = {
  clean: [
    ['probing', 'Read media info'],
    ['transcribing', 'Transcribe'],
    ['matching', 'Match wordlists'],
    ['rendering', 'Encode cleaned audio'],
    ['validating', 'Validate output'],
    ['publishing', 'Publish'],
  ],
  copy: [['copying', 'Copy unchanged']],
}

// Stages that scale with the length of the book. The others take seconds.
const SLOW = new Set(['transcribing', 'rendering', 'copying'])

export default function JobDetail({ jobId, live, onClose, onChanged }) {
  const toast = useToast()
  const [detail, setDetail] = useState(null)

  useEffect(() => {
    let cancelled = false
    let timer = null
    const load = async () => {
      try {
        const next = await api.jobDetail(jobId)
        if (cancelled) return
        setDetail(next)
        // Live progress arrives by SSE; the poll only refreshes the chunk
        // history and the log, so it can afford to be slow.
        if (next.job.status === 'running' || next.job.status === 'pending') {
          timer = setTimeout(load, 5000)
        }
      } catch (err) {
        if (!cancelled) toast(err.message, 'error')
      }
    }
    load()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [jobId])

  if (!detail) {
    return (
      <Modal title="Loading…" onClose={onClose}>
        <div className="faint">Fetching job details…</div>
      </Modal>
    )
  }

  const { job, book, logs } = detail
  // The SSE copy is fresher than the poll; the poll has the history.
  const state = detail.live ? { ...detail.live, ...(live || {}) } : null
  const running = job.status === 'running' && state

  const cancel = async () => {
    try {
      await api.cancelJob(job.id)
      toast(running ? 'Cancelling…' : 'Removed', 'success')
      onChanged?.()
      onClose()
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  return (
    <Modal
      wide
      title={job.book_title}
      onClose={onClose}
      footer={
        job.status === 'running' || job.status === 'pending' ? (
          <button className="danger" onClick={cancel}>
            {job.status === 'running' ? 'Cancel job' : 'Remove from queue'}
          </button>
        ) : null
      }
    >
      <div className="flex wrap mb">
        <StatusPill status={job.status} />
        {detail.mode === 'copy' ? <span className="tag">marked clean · copy only</span> : null}
        <span className="dim">{job.book_author || 'Unknown author'}</span>
        {detail.mode !== 'copy' ? (
          <span className="faint">
            {detail.engine} · {detail.model}
          </span>
        ) : null}
      </div>

      {running ? <LiveSummary state={state} /> : <FinishedSummary job={job} />}

      <div className="grid cols-2 mt">
        <div className="card">
          <div className="card-head">
            <h2>Stages</h2>
            {running && state.part_total > 1 ? (
              <span className="faint spacer">
                part {state.part_index} of {state.part_total}
              </span>
            ) : null}
          </div>
          <StageList mode={detail.mode} state={running ? state : null} job={job} />
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Book</h2>
          </div>
          {book ? (
            <dl className="facts">
              <dt>Length</dt>
              <dd>{formatDuration(book.duration || state?.audio_seconds)}</dd>
              <dt>Parts</dt>
              <dd>{book.part_count}</dd>
              <dt>Size</dt>
              <dd>{formatBytes(book.total_bytes)}</dd>
              <dt>Format</dt>
              <dd>{(book.formats || []).join(', ') || '—'}</dd>
              <dt>Folder</dt>
              <dd className="mono truncate" title={book.folder}>
                {book.folder}
              </dd>
              <dt>Queued</dt>
              <dd>{relativeTime(job.queued_at)}</dd>
            </dl>
          ) : (
            <div className="faint">This book is no longer in the library.</div>
          )}
        </div>
      </div>

      {running && state.stage === 'transcribing' ? <ChunkPace state={state} /> : null}

      <div className="card mt">
        <div className="card-head">
          <h2>Log</h2>
          <span className="faint spacer">everything logged while this job ran</span>
        </div>
        {logs.length ? (
          <div className="log-list job-log">
            {logs.map((entry) => (
              <div key={entry.id} className={`log-line ${entry.level}`}>
                <span className="ts">{parseDate(entry.created_at).toLocaleTimeString()}</span>
                <span className="lvl">{entry.level}</span>
                <span>{entry.message}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="faint">{job.started_at ? 'Nothing logged yet.' : 'Not started yet.'}</div>
        )}
      </div>
    </Modal>
  )
}

function LiveSummary({ state }) {
  const finish = state.job_eta_seconds > 0
    ? { value: clockIn(state.job_eta_seconds), sub: `${formatDuration(state.job_eta_seconds)} from now` }
    : state.stage_eta_seconds > 0
      ? {
          value: '—',
          // Say what is known rather than nothing: the end of this stage, and
          // why the whole-job figure is missing.
          sub: `${stageName(state.stage)} done ${clockIn(state.stage_eta_seconds)}; later stages not measured yet`,
        }
      : { value: '—', sub: state.stalled ? 'no progress reported recently' : 'measuring…' }

  return (
    <>
      <div className="grid cols-4">
        <Stat
          label="Progress"
          value={`${Math.round((state.progress || 0) * 100)}%`}
          sub={state.message || state.stage}
        />
        <Stat label="Running for" value={formatDuration(state.elapsed)} />
        <Stat label="Finishes" value={finish.value} sub={finish.sub} />
        <Stat
          label="Speed"
          value={state.speed ? `${state.speed >= 10 ? Math.round(state.speed) : state.speed.toFixed(1)}×` : '—'}
          sub={state.speed ? 'faster than real time' : 'once a step completes'}
        />
      </div>
      <div className="mt">
        <Progress value={state.progress} />
      </div>
    </>
  )
}

function FinishedSummary({ job }) {
  const result = job.result || {}
  if (job.status === 'pending') {
    return (
      <div className="grid cols-4">
        <Stat label="Status" value="Waiting" sub={`queued ${relativeTime(job.queued_at)}`} />
        <Stat
          label="Estimate"
          value={job.estimate_seconds ? formatDuration(job.estimate_seconds) : '—'}
          sub={job.estimate_seconds ? 'once it starts' : 'not measured yet'}
        />
      </div>
    )
  }
  return (
    <div className="grid cols-4">
      <Stat label="Took" value={formatDuration(durationOf(job))} sub={relativeTime(job.finished_at)} />
      <Stat label="Matches" value={result.matches ?? '—'} />
      <Stat
        label="Muted"
        value={result.muted_seconds ? formatDuration(result.muted_seconds) : '—'}
      />
      <Stat label="Outcome" value={job.status === 'completed' ? 'Done' : job.status} sub={job.message || job.error} />
    </div>
  )
}

function StageList({ mode, state, job }) {
  const stages = STAGES[mode] || STAGES.clean
  const timings = job.result?.timings?.stages || {}
  const history = state?.stages || []
  const currentIndex = state ? stages.findIndex(([id]) => id === state.stage) : -1
  const partAudio = state?.part_audio?.[(state.part_index || 1) - 1] || 0

  // Time already spent in each stage of the current part.
  const spent = {}
  for (const entry of history) {
    if (entry.part === state?.part_index && entry.seconds != null) spent[entry.stage] = entry.seconds
  }

  return (
    <div className="stage-list">
      {stages.map(([id, label], index) => {
        let status = 'upcoming'
        if (state) {
          if (index < currentIndex) status = 'done'
          else if (index === currentIndex) status = 'current'
        } else if (job.status === 'completed') {
          status = 'done'
        }

        let note = null
        if (status === 'current') {
          const left = state.stage_eta_seconds > 0 ? ` · ~${formatDuration(state.stage_eta_seconds)} left` : ''
          note = `${formatDuration(state.stage_elapsed)} so far${left}`
        } else if (status === 'done') {
          const seconds = state ? spent[id] : timings[id]
          if (seconds == null) note = '✓'
          else note = seconds < 1 ? '<1s' : formatDuration(seconds)
        } else if (state && SLOW.has(id)) {
          const rate = state.rates?.[id]
          note = rate && partAudio ? `est. ${formatDuration(rate * partAudio)}` : 'not measured yet'
        } else if (state) {
          note = 'seconds'
        }

        return (
          <div key={id} className={`stage-row ${status}`}>
            <span className="stage-dot" aria-hidden="true" />
            <div className="stage-body">
              <div className="flex">
                <span>{label}</span>
                <span className="faint spacer nowrap">{note}</span>
              </div>
              {status === 'current' ? (
                <>
                  <Progress slim value={state.stage_fraction} />
                  {state.message && state.message !== id ? (
                    <div className="faint stage-message">{state.message}</div>
                  ) : null}
                </>
              ) : null}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function ChunkPace({ state }) {
  // Each tick is a finished chunk, so the gaps between them are chunk times.
  const ticks = state.ticks || []
  const gaps = []
  for (let i = 1; i < ticks.length; i += 1) gaps.push(ticks[i][0] - ticks[i - 1][0])
  const match = /(\d+)\/(\d+)/.exec(state.message || '')
  const total = match ? Number(match[2]) : null
  const current = match ? Number(match[1]) : null
  const average = gaps.length ? gaps.reduce((a, b) => a + b, 0) / gaps.length : null
  const longest = Math.max(1, ...gaps, state.stage_elapsed - (ticks.at(-1)?.[0] || 0))
  const inFlight = Math.max(0, state.stage_elapsed - (ticks.at(-1)?.[0] || 0))

  return (
    <div className="card mt">
      <div className="card-head">
        <h2>Transcription pace</h2>
        <span className="faint spacer">
          {total ? `chunk ${current} of ${total}` : null}
          {average ? ` · ${formatDuration(average)} per chunk on average` : ''}
          {state.speed ? ` · ${formatSpeed(state.speed)}` : ''}
        </span>
      </div>
      {gaps.length ? (
        <div className="chunk-bars" role="img" aria-label="Time taken by each finished chunk">
          {gaps.map((gap, index) => (
            <div
              key={index}
              className="chunk-bar"
              style={{ height: `${(gap / longest) * 100}%` }}
              title={`Chunk ${index + 1}: ${formatDuration(gap)}`}
            />
          ))}
          <div
            className="chunk-bar running"
            style={{ height: `${(inFlight / longest) * 100}%` }}
            title={`Chunk ${gaps.length + 1}: ${formatDuration(inFlight)} so far`}
          />
          {total
            ? Array.from({ length: Math.max(0, total - gaps.length - 1) }, (_, index) => (
                <div key={`todo-${index}`} className="chunk-bar todo" />
              ))
            : null}
        </div>
      ) : (
        <div className="faint">The first chunk is still running; its time will appear here.</div>
      )}
    </div>
  )
}

function stageName(stage) {
  for (const list of Object.values(STAGES)) {
    const found = list.find(([id]) => id === stage)
    if (found) return found[1]
  }
  return stage
}

function durationOf(job) {
  if (!job.started_at || !job.finished_at) return null
  return (parseDate(job.finished_at) - parseDate(job.started_at)) / 1000
}

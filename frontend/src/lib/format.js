export function formatDuration(seconds) {
  if (!seconds || seconds < 0) return '—'
  const total = Math.round(seconds)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`
  return `${s}s`
}

export function jobActivity(job) {
  // The percentage alone cannot distinguish slow from hung. Encoding a long
  // book is a single ffmpeg run that can sit on one number for an hour, so
  // what the reader actually needs is how long this *stage* has been going
  // and, once there is enough to extrapolate from, how much is left.
  if (!job) return ''
  const bits = []
  if (job.message || job.stage) bits.push(job.message || job.stage)
  if (job.stage_elapsed > 5) bits.push(`${formatDuration(job.stage_elapsed)} in this stage`)
  if (job.stage_eta_seconds > 0) bits.push(`~${formatDuration(job.stage_eta_seconds)} left`)
  return bits.join(' · ')
}

export function formatClock(seconds) {
  if (seconds == null) return '—'
  const total = Math.max(0, Math.floor(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`
}

export function formatBytes(bytes) {
  if (!bytes) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  return `${value.toFixed(value >= 100 || index === 0 ? 0 : 1)} ${units[index]}`
}

export function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function relativeTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  const diff = (Date.now() - date.getTime()) / 1000
  if (Number.isNaN(diff)) return '—'
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`
  return formatDate(value)
}

export const STATUS_LABELS = {
  new: 'Not cleaned',
  queued: 'Queued',
  processing: 'Processing',
  cleaned: 'Cleaned',
  partial: 'Partial',
  failed: 'Failed',
  ignored: 'Ignored',
  missing: 'Missing',
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  cancelled: 'Cancelled',
}

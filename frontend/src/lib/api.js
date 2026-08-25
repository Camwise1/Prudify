// Thin API client.
//
// Browsers authenticate with an HttpOnly session cookie the server sets at
// login, which is why there is no token in localStorage any more: script
// cannot read an HttpOnly cookie, so an XSS bug cannot walk off with the
// credential. Cookies are sent automatically, so state-changing requests
// carry a CSRF token echoed from a readable companion cookie.
//
// An API key is still supported for people driving the API from a script or
// from another host, and for the rare deployment left on `apikey` auth.

const KEY_STORAGE = 'prudify.apiKey'
const CSRF_COOKIE = 'prudify_csrf'
const CSRF_HEADER = 'X-Prudify-CSRF'

function readCookie(name) {
  const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'))
  return match ? decodeURIComponent(match[1]) : ''
}

export function getApiKey() {
  try {
    return localStorage.getItem(KEY_STORAGE) || ''
  } catch {
    return ''
  }
}

export function setApiKey(key) {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key)
    else localStorage.removeItem(KEY_STORAGE)
  } catch {
    /* private mode: the key simply will not persist */
  }
}

function apiBase() {
  // Works whether the app is served from / or from a reverse-proxy sub-path.
  const path = window.location.pathname.replace(/\/+$/, '')
  const marker = path.indexOf('/api/')
  const base = marker >= 0 ? path.slice(0, marker) : path
  return `${base.replace(/\/index\.html$/, '')}/api/v1`
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

export async function request(path, options = {}) {
  const url = `${apiBase()}${path}`
  const headers = { ...(options.headers || {}) }
  const key = getApiKey()
  if (key) headers['X-Api-Key'] = key

  const method = (options.method || 'GET').toUpperCase()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) headers[CSRF_HEADER] = csrf
  }
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }

  let response
  try {
    response = await fetch(url, {
      ...options,
      headers,
      // Send the session cookie, including when the UI is served from a
      // different origin during development.
      credentials: 'same-origin',
      body:
        options.body !== undefined && !(options.body instanceof FormData)
          ? JSON.stringify(options.body)
          : options.body,
    })
  } catch (err) {
    throw new ApiError('Could not reach the Prudify server', 0)
  }

  if (response.status === 204) return null
  const text = await response.text()
  let payload = null
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    payload = text
  }

  if (!response.ok) {
    const detail =
      (payload && (payload.detail || payload.message)) || `Request failed (${response.status})`
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), response.status)
  }
  return payload
}

export const api = {
  // Artwork is loaded by the browser as an <img src>, which cannot carry a
  // header -- so this is a plain URL and the session cookie authenticates it.
  // A book with no artwork answers with a blank pixel rather than a 404, so
  // scrolling a library does not fill the console with errors.
  coverUrl: (bookId) => `${apiBase()}/books/${encodeURIComponent(bookId)}/cover`,

  // auth
  authStatus: () => request('/auth/status'),
  login: (username, password) =>
    request('/auth/login', { method: 'POST', body: { username, password } }),
  setupAccount: (username, password) =>
    request('/auth/setup', { method: 'POST', body: { username, password } }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  changePassword: (current_password, new_password, sign_out_everywhere = false) =>
    request('/auth/password', {
      method: 'POST',
      body: { current_password, new_password, sign_out_everywhere },
    }),

  // system
  status: () => request('/system/status'),
  about: () => request('/system/about'),
  logs: (params = {}) => request(`/system/logs?${new URLSearchParams(params)}`),
  clearLogs: () => request('/system/logs', { method: 'DELETE' }),
  scanNow: () => request('/system/scan', { method: 'POST' }),
  browse: (path) => request(`/system/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`),

  // books
  books: (params = {}) => request(`/books?${new URLSearchParams(params)}`),
  book: (id) => request(`/books/${id}`),
  bookMatches: (id) => request(`/books/${id}/matches`),
  queueBook: (id) => request(`/books/${id}/queue`, { method: 'POST' }),
  queueAll: (libraryId) =>
    request(`/books/queue-all${libraryId ? `?library_id=${libraryId}` : ''}`, { method: 'POST' }),
  setMonitored: (id, monitored) =>
    request(`/books/${id}/monitor?monitored=${monitored}`, { method: 'POST' }),
  resetBook: (id, opts = {}) =>
    request(
      `/books/${id}/reset?delete_output=${!!opts.deleteOutput}&delete_transcript=${!!opts.deleteTranscript}`,
      { method: 'POST' },
    ),
  rescan: () => request('/books/scan', { method: 'POST' }),
  stats: () => request('/books/stats/summary'),

  // queue
  queue: () => request('/queue'),
  queueHistory: (params = {}) => request(`/queue/history?${new URLSearchParams(params)}`),
  pause: () => request('/queue/pause', { method: 'POST' }),
  resume: () => request('/queue/resume', { method: 'POST' }),
  cancelJob: (id) => request(`/queue/${id}`, { method: 'DELETE' }),
  clearQueue: () => request('/queue/clear', { method: 'POST' }),

  // settings
  settings: () => request('/settings'),
  saveSettings: (body) => request('/settings', { method: 'PUT', body }),
  regenerateKey: () => request('/settings/regenerate-api-key', { method: 'POST' }),
  libraries: () => request('/libraries'),
  createLibrary: (body) => request('/libraries', { method: 'POST', body }),
  updateLibrary: (id, body) => request(`/libraries/${id}`, { method: 'PUT', body }),
  deleteLibrary: (id) => request(`/libraries/${id}`, { method: 'DELETE' }),
  scanLibrary: (id) => request(`/libraries/${id}/scan`, { method: 'POST' }),

  // wordlists
  wordlists: () => request('/wordlists'),
  wordlist: (name) => request(`/wordlists/${name}`),
  saveWordlist: (name, content) => request(`/wordlists/${name}`, { method: 'PUT', body: { content } }),
  deleteWordlist: (name) => request(`/wordlists/${name}`, { method: 'DELETE' }),
  testWords: (body) => request('/wordlists/test', { method: 'POST', body }),
}

// Server-Sent Events. EventSource cannot set headers, so the key rides along
// as a query parameter -- the server accepts either.
export function openEventStream(onEvent) {
  const key = getApiKey()
  const url = `${apiBase()}/queue/events${key ? `?apikey=${encodeURIComponent(key)}` : ''}`
  const source = new EventSource(url)

  const types = [
    'job.queued',
    'job.started',
    'job.progress',
    'job.part_finished',
    'job.finished',
    'job.cancelled',
    'queue.paused',
    'queue.cleared',
    'queue.cooldown',
    'queue.auto_enqueued',
    'library.book_added',
    'library.scan_complete',
    'settings.updated',
    'wordlist.updated',
    'log',
  ]
  types.forEach((type) => {
    source.addEventListener(type, (event) => {
      let data = {}
      try {
        data = JSON.parse(event.data)
      } catch {
        /* ignore malformed frames */
      }
      onEvent(type, data)
    })
  })

  return source
}

import React, { useEffect, useState } from 'react'
import { api, setApiKey } from '../lib/api.js'
import { Banner, Check, Field, Modal, PathBrowser, Tabs, useToast } from '../components/ui.jsx'

const WHISPER_MODELS = [
  'tiny.en',
  'tiny',
  'base.en',
  'base',
  'small.en',
  'small',
  'medium.en',
  'medium',
  'large-v3',
  'large-v3-turbo',
  'distil-large-v3',
]

export default function Settings({ settings, onSaved }) {
  const toast = useToast()
  const [tab, setTab] = useState('libraries')
  const [draft, setDraft] = useState(settings)
  const [dirty, setDirty] = useState(false)
  const [libraries, setLibraries] = useState([])
  const [editing, setEditing] = useState(null)

  useEffect(() => {
    setDraft(settings)
    setDirty(false)
  }, [settings])

  const loadLibraries = async () => {
    try {
      setLibraries(await api.libraries())
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  useEffect(() => {
    loadLibraries()
  }, [settings])

  if (!draft) return <div className="page faint">Loading settings…</div>

  const update = (section, key, value) => {
    setDraft({ ...draft, [section]: { ...draft[section], [key]: value } })
    setDirty(true)
  }

  const save = async () => {
    try {
      const { libraries: _ignored, ...payload } = draft
      await api.saveSettings(payload)
      toast('Settings saved', 'success')
      setDirty(false)
      onSaved()
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  return (
    <div className="page">
      <div className="toolbar">
        <Tabs
          active={tab}
          onChange={setTab}
          tabs={[
            { id: 'libraries', label: 'Libraries' },
            { id: 'transcription', label: 'Transcription' },
            { id: 'filtering', label: 'Filtering' },
            { id: 'output', label: 'Output' },
            { id: 'processing', label: 'Processing' },
            { id: 'ebooks', label: 'Ebook check' },
            { id: 'schedule', label: 'Schedule' },
            { id: 'integrations', label: 'Integrations' },
            { id: 'security', label: 'Security' },
          ]}
        />
        <div className="spacer" />
        <button className="primary" disabled={!dirty} onClick={save}>
          Save changes
        </button>
      </div>

      {tab === 'libraries' ? (
        <LibrariesTab
          libraries={libraries}
          onReload={loadLibraries}
          onEdit={setEditing}
          onSaved={onSaved}
        />
      ) : null}

      {tab === 'transcription' ? (
        <div className="card">
          <div className="row">
            <Field
              label="Engine"
              hint="faster-whisper runs in bounded memory — the safe choice on machines without a GPU. On an Apple Silicon Mac, mlx-whisper uses the GPU."
            >
              <select
                value={draft.transcription.engine}
                onChange={(e) => update('transcription', 'engine', e.target.value)}
              >
                <option value="faster-whisper">faster-whisper (recommended)</option>
                <option value="mlx-whisper">mlx-whisper (Apple Silicon)</option>
                <option value="whisper-cpp">whisper.cpp</option>
                <option value="openai-whisper">openai-whisper</option>
              </select>
            </Field>
            <Field label="Model" hint="base.en is a good default; small.en is more accurate and ~3x slower.">
              <select
                value={draft.transcription.model}
                onChange={(e) => update('transcription', 'model', e.target.value)}
              >
                {WHISPER_MODELS.map((model) => (
                  <option key={model} value={model}>
                    {model}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="row">
            <Field label="Device">
              <select
                value={draft.transcription.device}
                onChange={(e) => update('transcription', 'device', e.target.value)}
              >
                <option value="auto">Auto-detect</option>
                <option value="cpu">CPU</option>
                <option value="cuda">CUDA GPU</option>
              </select>
            </Field>
            <Field label="Compute type" hint="int8 halves memory on CPU at a small accuracy cost.">
              <select
                value={draft.transcription.compute_type}
                onChange={(e) => update('transcription', 'compute_type', e.target.value)}
              >
                <option value="auto">Auto</option>
                <option value="int8">int8</option>
                <option value="int8_float16">int8_float16</option>
                <option value="float16">float16</option>
                <option value="float32">float32</option>
              </select>
            </Field>
            <Field label="CPU threads">
              <input
                type="number"
                min="1"
                max="64"
                value={draft.transcription.cpu_threads}
                onChange={(e) => update('transcription', 'cpu_threads', Number(e.target.value))}
              />
            </Field>
          </div>
          <div className="row">
            <Field label="Language" hint="Leave as en for English narration; blank auto-detects.">
              <input
                value={draft.transcription.language}
                onChange={(e) => update('transcription', 'language', e.target.value)}
              />
            </Field>
            <Field
              label="Chunk length (minutes)"
              hint="0 auto-chunks long books. Set a number to force a specific segment length."
            >
              <input
                type="number"
                min="0"
                max="240"
                value={draft.transcription.chunk_minutes}
                onChange={(e) => update('transcription', 'chunk_minutes', Number(e.target.value))}
              />
            </Field>
            <Field label="Model cache directory" hint="Leave blank to use the default cache.">
              <input
                value={draft.transcription.model_dir}
                onChange={(e) => update('transcription', 'model_dir', e.target.value)}
              />
            </Field>
          </div>
          <Check
            label="Voice activity detection"
            hint="Skips silence. Faster, and avoids Whisper inventing words in quiet passages."
            checked={draft.transcription.vad_filter}
            onChange={(value) => update('transcription', 'vad_filter', value)}
          />
        </div>
      ) : null}

      {tab === 'filtering' ? (
        <div className="card">
          <div className="row">
            <Field label="Wordlist" hint="Edit lists on the Wordlists page.">
              <input
                value={draft.filtering.wordlist}
                onChange={(e) => update('filtering', 'wordlist', e.target.value)}
              />
            </Field>
            <Field
              label="Match mode"
              hint="Exact matches whole words only — the safest setting. Fuzzy also catches mishearings."
            >
              <select
                value={draft.filtering.match_mode}
                onChange={(e) => update('filtering', 'match_mode', e.target.value)}
              >
                <option value="exact">Exact</option>
                <option value="prefix">Prefix (honours word*)</option>
                <option value="fuzzy">Fuzzy</option>
              </select>
            </Field>
            <Field
              label="Minimum confidence"
              hint="0 keeps every hit. 0.5 ignores words Whisper was unsure about."
            >
              <input
                type="number"
                step="0.05"
                min="0"
                max="1"
                value={draft.filtering.min_confidence}
                onChange={(e) => update('filtering', 'min_confidence', Number(e.target.value))}
              />
            </Field>
          </div>
          <div className="row">
            <Field label="Padding before (ms)">
              <input
                type="number"
                min="0"
                value={draft.filtering.pad_before_ms}
                onChange={(e) => update('filtering', 'pad_before_ms', Number(e.target.value))}
              />
            </Field>
            <Field label="Padding after (ms)" hint="Covers Whisper cutting the end of a word short.">
              <input
                type="number"
                min="0"
                value={draft.filtering.pad_after_ms}
                onChange={(e) => update('filtering', 'pad_after_ms', Number(e.target.value))}
              />
            </Field>
            <Field label="Merge gap (ms)" hint="Two hits closer than this become one silence.">
              <input
                type="number"
                min="0"
                value={draft.filtering.merge_gap_ms}
                onChange={(e) => update('filtering', 'merge_gap_ms', Number(e.target.value))}
              />
            </Field>
          </div>
          <Field
            label="Extra words"
            hint="One per line, added on top of the selected wordlist."
          >
            <textarea
              style={{ minHeight: 120 }}
              value={(draft.filtering.custom_words || []).join('\n')}
              onChange={(e) =>
                update(
                  'filtering',
                  'custom_words',
                  e.target.value.split('\n').filter((line) => line.trim()),
                )
              }
            />
          </Field>
        </div>
      ) : null}

      {tab === 'output' ? (
        <div className="card">
          <div className="row">
            <Field label="What to do with a hit">
              <select
                value={draft.output.mode}
                onChange={(e) => update('output', 'mode', e.target.value)}
              >
                <option value="mute">Silence it (keeps timing)</option>
                <option value="beep">Beep over it</option>
                <option value="sound">Play a sound clip over it (a goat, say)</option>
                <option value="cut">Cut it out (shortens the book)</option>
              </select>
            </Field>
            <Field label="Container">
              <select
                value={draft.output.container}
                onChange={(e) => update('output', 'container', e.target.value)}
              >
                <option value="same">Same as source</option>
                <option value="m4b">M4B</option>
                <option value="m4a">M4A</option>
                <option value="mp3">MP3</option>
                <option value="opus">Opus</option>
              </select>
            </Field>
            <Field label="Bitrate" hint="auto matches the source.">
              <input
                value={draft.output.bitrate}
                onChange={(e) => update('output', 'bitrate', e.target.value)}
              />
            </Field>
          </div>
          {draft.output.mode === 'sound' ? <SoundClip /> : null}
          {draft.output.mode === 'beep' || draft.output.mode === 'sound' ? (
            <div className="row">
              {draft.output.mode === 'beep' ? (
                <Field label="Beep frequency (Hz)">
                  <input
                    type="number"
                    value={draft.output.beep_frequency}
                    onChange={(e) => update('output', 'beep_frequency', Number(e.target.value))}
                  />
                </Field>
              ) : null}
              <Field
                label="Volume (0–1)"
                hint="A fraction of full scale. Narration usually peaks around 0.3–0.5."
              >
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  max="1"
                  value={draft.output.beep_volume}
                  onChange={(e) => update('output', 'beep_volume', Number(e.target.value))}
                />
              </Field>
              <Field label="Hear it">
                <button
                  type="button"
                  onClick={() => {
                    const sample = new Audio(
                      api.soundSampleUrl({
                        mode: draft.output.mode,
                        volume: draft.output.beep_volume,
                        frequency: draft.output.beep_frequency,
                      }),
                    )
                    sample.play().catch(() => toast('Could not play the sample', 'error'))
                  }}
                >
                  Play sample
                </button>
              </Field>
            </div>
          ) : null}
          {draft.output.mode === 'cut' ? (
            <Banner tone="warn">
              <div>
                Cutting changes the running time. Prudify rebuilds chapter markers to match, but
                Audiobookshelf progress saved against the original will no longer line up.
              </div>
            </Banner>
          ) : null}
          <Check
            label="Preserve chapters"
            checked={draft.output.preserve_chapters}
            onChange={(value) => update('output', 'preserve_chapters', value)}
          />
          <Check
            label="Preserve embedded cover art"
            checked={draft.output.preserve_cover}
            onChange={(value) => update('output', 'preserve_cover', value)}
          />
          <Check
            label="Preserve tags and metadata"
            checked={draft.output.preserve_metadata}
            onChange={(value) => update('output', 'preserve_metadata', value)}
          />
          <Check
            label="Copy clean books across unchanged"
            hint="Keeps the clean library a complete mirror even when a book had nothing to silence."
            checked={draft.output.copy_when_clean}
            onChange={(value) => update('output', 'copy_when_clean', value)}
          />
          <Check
            label="Tag output as cleaned by Prudify"
            checked={draft.output.tag_cleaned}
            onChange={(value) => update('output', 'tag_cleaned', value)}
          />
          <Check
            label="Overwrite existing cleaned files"
            checked={draft.output.overwrite_existing}
            onChange={(value) => update('output', 'overwrite_existing', value)}
          />
        </div>
      ) : null}

      {tab === 'processing' ? (
        <div className="card">
          <div className="row">
            <Field
              label="Books at a time"
              hint="Whisper is CPU-hungry. Leave this at 1 unless you have cores to spare."
            >
              <input
                type="number"
                min="1"
                max="8"
                value={draft.processing.max_concurrent_jobs}
                onChange={(e) =>
                  update('processing', 'max_concurrent_jobs', Number(e.target.value))
                }
              />
            </Field>
            <Field label="Cool-down between books (seconds)">
              <input
                type="number"
                min="0"
                value={draft.processing.cooldown_seconds}
                onChange={(e) => update('processing', 'cooldown_seconds', Number(e.target.value))}
              />
            </Field>
            <Field
              label="Rescan interval (minutes)"
              hint="0 disables scheduled scans. Keep this on for network shares."
            >
              <input
                type="number"
                min="0"
                value={draft.processing.scan_interval_minutes}
                onChange={(e) =>
                  update('processing', 'scan_interval_minutes', Number(e.target.value))
                }
              />
            </Field>
          </div>
          <div className="row">
            <Field
              label="File settle time (seconds)"
              hint="How long a new file must stop growing before it is queued."
            >
              <input
                type="number"
                min="0"
                value={draft.processing.stability_seconds}
                onChange={(e) => update('processing', 'stability_seconds', Number(e.target.value))}
              />
            </Field>
            <Field label="Minimum free space (MB)">
              <input
                type="number"
                min="0"
                value={draft.processing.min_free_space_mb}
                onChange={(e) => update('processing', 'min_free_space_mb', Number(e.target.value))}
              />
            </Field>
            <Field label="Duration tolerance (seconds)" hint="Output is rejected if it drifts more.">
              <input
                type="number"
                step="0.1"
                min="0"
                value={draft.processing.duration_tolerance_seconds}
                onChange={(e) =>
                  update('processing', 'duration_tolerance_seconds', Number(e.target.value))
                }
              />
            </Field>
          </div>
          <Check
            label="Keep transcripts"
            hint="Lets you re-run with different wordlists without paying for Whisper again."
            checked={draft.processing.keep_transcripts}
            onChange={(value) => update('processing', 'keep_transcripts', value)}
          />
          <Check
            label="Skip books that already have a cleaned copy"
            checked={draft.processing.skip_if_output_exists}
            onChange={(value) => update('processing', 'skip_if_output_exists', value)}
          />
          <Check
            label="Dry run"
            hint="Transcribe and report matches, but never write an output file."
            checked={draft.processing.dry_run}
            onChange={(value) => update('processing', 'dry_run', value)}
          />
          <Check
            label="Keep working files for debugging"
            checked={draft.processing.keep_work_files}
            onChange={(value) => update('processing', 'keep_work_files', value)}
          />
        </div>
      ) : null}

      {tab === 'ebooks' ? (
        <div className="card">
          <p className="dim" style={{ marginTop: 0 }}>
            Before transcribing a queued book, Prudify looks for the same book as an EPUB and runs
            your wordlists over its text. That takes seconds instead of hours. Books are paired by
            title and author; only plain, DRM-free <code>.epub</code> files can be read.
          </p>
          <Field
            label="Ebook folders"
            hint="One per line, searched including subfolders. Leave empty to turn the check off."
          >
            <textarea
              style={{ minHeight: 90 }}
              placeholder="/private/nfs/Media/Plex/Books/Ebooks"
              value={(draft.prescreen?.ebook_paths || []).join('\n')}
              onChange={(e) => update('prescreen', 'ebook_paths', e.target.value.split('\n'))}
            />
          </Field>
          <Check
            label="Copy books across unchanged when their ebook has no matches"
            hint="Only when the ebook is DRM-free and long enough to be the whole book. Anything else is transcribed as usual."
            checked={draft.prescreen?.auto_mark_clean ?? true}
            onChange={(value) => update('prescreen', 'auto_mark_clean', value)}
          />
        </div>
      ) : null}

      {tab === 'schedule' ? (
        <div className="card">
          <p className="dim" style={{ marginTop: 0 }}>
            Transcription works this machine hard. Outside these hours, or while Plex is playing, no
            new book starts, and one already running stops at the end of its current 30-minute chunk
            and carries on from there later. Copies of books marked clean are not held.
          </p>
          <div className="row">
            <Field label="Transcribe from" hint="Leave both empty for any time.">
              <input
                type="time"
                value={draft.schedule?.start || ''}
                onChange={(e) => update('schedule', 'start', e.target.value)}
              />
            </Field>
            <Field label="Until" hint="May be earlier than the start, for overnight.">
              <input
                type="time"
                value={draft.schedule?.end || ''}
                onChange={(e) => update('schedule', 'end', e.target.value)}
              />
            </Field>
          </div>
          <Check
            label="Wait while Plex is playing something"
            hint="Needs the Plex token on the Integrations tab. Paused streams do not count."
            checked={!!draft.schedule?.pause_while_plex_playing}
            onChange={(value) => update('schedule', 'pause_while_plex_playing', value)}
          />
        </div>
      ) : null}

      {tab === 'integrations' ? (
        <IntegrationsTab
          draft={draft}
          onChange={(service, key, value) => {
            setDraft({
              ...draft,
              integrations: {
                ...draft.integrations,
                [service]: { ...draft.integrations[service], [key]: value },
              },
            })
            setDirty(true)
          }}
        />
      ) : null}

      {tab === 'security' ? (
        <div className="card">
          <Field
            label="How browsers sign in"
            hint="The API key below keeps working for scripts whichever you choose."
          >
            <select
              value={draft.auth?.method || 'forms'}
              onChange={(e) => update('auth', 'method', e.target.value)}
            >
              <option value="forms">Login page (recommended)</option>
              <option value="basic">Browser password prompt</option>
              <option value="apikey">API key only</option>
              <option value="external">Reverse proxy header</option>
              <option value="none">No authentication</option>
            </select>
          </Field>

          {draft.auth?.method === 'none' ? (
            <Banner tone="error">
              Anyone who can reach this port has full access, including the
              ability to read your API key and change settings. Only sensible
              if something in front of Prudify is doing the authenticating.
            </Banner>
          ) : null}

          {draft.auth?.method === 'external' ? (
            <>
              <Field
                label="Trusted proxy networks"
                hint="Comma-separated CIDRs. The identity header is ignored from anywhere else — without this, anyone could simply send the header themselves."
              >
                <input
                  className="mono"
                  placeholder="172.16.0.0/12, 192.168.1.0/24"
                  value={(draft.auth?.trusted_proxies || []).join(', ')}
                  onChange={(e) =>
                    update(
                      'auth',
                      'trusted_proxies',
                      e.target.value.split(',').map((s) => s.trim()).filter(Boolean)
                    )
                  }
                />
              </Field>
              <Field label="Username header">
                <input
                  className="mono"
                  value={draft.auth?.proxy_user_header || 'X-Forwarded-User'}
                  onChange={(e) => update('auth', 'proxy_user_header', e.target.value)}
                />
              </Field>
              {!(draft.auth?.trusted_proxies || []).length ? (
                <Banner tone="error">
                  No trusted networks set, so the header is refused from
                  everywhere and nobody can sign in. Add your proxy's address.
                </Banner>
              ) : null}
            </>
          ) : null}

          <Check
            label="Skip authentication on the local network"
            hint="Convenient on a trusted LAN. Wrong behind a reverse proxy, where every request appears to come from the proxy itself."
            checked={draft.auth?.required === 'disabled_for_local'}
            onChange={(value) =>
              update('auth', 'required', value ? 'disabled_for_local' : 'always')
            }
          />

          <Field
            label="Stay signed in for"
            hint="How long a session lasts before you have to sign in again."
          >
            <select
              value={String(draft.auth?.session_lifetime_hours ?? 720)}
              onChange={(e) =>
                update('auth', 'session_lifetime_hours', Number(e.target.value))
              }
            >
              <option value="24">1 day</option>
              <option value="168">1 week</option>
              <option value="720">30 days</option>
              <option value="8760">1 year</option>
            </select>
          </Field>

          <hr className="rule" />

          <Field label="API key" hint="For scripts, the CLI and Home Assistant.">
            <input readOnly value={draft.server.api_key} className="mono" />
          </Field>
          <div className="flex mb">
            <button
              onClick={async () => {
                const result = await api.regenerateKey()
                setApiKey(result.api_key)
                toast('API key regenerated', 'success')
                onSaved()
              }}
            >
              Regenerate
            </button>
            <button
              onClick={() => {
                navigator.clipboard?.writeText(draft.server.api_key)
                toast('Copied', 'success')
              }}
            >
              Copy
            </button>
          </div>
          <div className="row">
            <Field label="Bind address" hint="Takes effect after a restart.">
              <input
                value={draft.server.host}
                onChange={(e) => update('server', 'host', e.target.value)}
              />
            </Field>
            <Field label="Port" hint="Takes effect after a restart.">
              <input
                type="number"
                value={draft.server.port}
                onChange={(e) => update('server', 'port', Number(e.target.value))}
              />
            </Field>
            <Field label="URL base" hint="For reverse proxies, e.g. /prudify. Restart required.">
              <input
                value={draft.server.url_base}
                onChange={(e) => update('server', 'url_base', e.target.value)}
              />
            </Field>
          </div>
          <Field label="Log level">
            <select
              value={draft.log_level}
              onChange={(e) => {
                setDraft({ ...draft, log_level: e.target.value })
                setDirty(true)
              }}
            >
              <option>DEBUG</option>
              <option>INFO</option>
              <option>WARNING</option>
              <option>ERROR</option>
            </select>
          </Field>
        </div>
      ) : null}

      {editing !== null ? (
        <LibraryModal
          library={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            loadLibraries()
            onSaved()
          }}
        />
      ) : null}
    </div>
  )
}

function LibrariesTab({ libraries, onReload, onEdit, onSaved }) {
  const toast = useToast()

  const remove = async (library) => {
    if (!window.confirm(`Remove “${library.name}”? Files on disk are not touched.`)) return
    try {
      await api.deleteLibrary(library.id)
      toast('Library removed', 'success')
      onReload()
      onSaved()
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  return (
    <>
      <div className="toolbar">
        <button
          className="primary"
          onClick={() =>
            onEdit({ name: 'Audiobooks', source_path: '', output_path: '', enabled: true, auto_process: true, layout: 'books' })
          }
        >
          Add library
        </button>
      </div>

      {libraries.length === 0 ? (
        <div className="card">
          <p className="dim">
            A library is a pair of folders: where your audiobooks live, and where cleaned copies
            should be written. Prudify never modifies anything in the source folder.
          </p>
        </div>
      ) : (
        <div className="grid cols-2">
          {libraries.map((library) => (
            <div className="card" key={library.id}>
              <div className="card-head">
                <h2>{library.name}</h2>
                <div className="spacer" />
                {library.enabled ? (
                  <span className="pill cleaned">Enabled</span>
                ) : (
                  <span className="pill ignored">Disabled</span>
                )}
              </div>
              <div className="mono faint mb" style={{ wordBreak: 'break-all' }}>
                <div>
                  {library.source_exists ? '✓' : '✗'} source: {library.source_path}
                </div>
                <div>
                  {library.output_exists ? '✓' : '✗'} output: {library.output_path}
                </div>
              </div>
              <div className="flex wrap">
                <span className="tag">
                  {library.layout === 'episodes' ? 'episodes · ' : ''}
                  {library.auto_process ? 'auto-processes new books' : 'manual only'}
                </span>
                <div className="spacer" />
                <button
                  className="small"
                  onClick={async () => {
                    try {
                      const result = await api.scanLibrary(library.id)
                      toast(`Scanned: ${result.total} book(s)`, 'success')
                      onSaved()
                    } catch (err) {
                      toast(err.message, 'error')
                    }
                  }}
                >
                  Scan
                </button>
                <button className="small" onClick={() => onEdit(library)}>
                  Edit
                </button>
                <button className="small danger" onClick={() => remove(library)}>
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

function LibraryModal({ library, onClose, onSaved }) {
  const toast = useToast()
  const [form, setForm] = useState({
    name: library.name || 'Audiobooks',
    source_path: library.source_path || '',
    output_path: library.output_path || '',
    enabled: library.enabled !== false,
    auto_process: library.auto_process !== false,
    layout: library.layout || 'books',
    extensions: library.extensions || [],
    exclude_patterns: library.exclude_patterns || [],
  })
  const [saving, setSaving] = useState(false)

  const save = async () => {
    setSaving(true)
    try {
      if (library.id) await api.updateLibrary(library.id, form)
      else await api.createLibrary(form)
      toast('Library saved', 'success')
      onSaved()
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={library.id ? 'Edit library' : 'Add library'}
      onClose={onClose}
      footer={
        <>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={saving} onClick={save}>
            Save
          </button>
        </>
      }
    >
      <Field label="Name">
        <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </Field>
      <Field label="Source folder" hint="Your existing audiobook library. Never modified.">
        <div className="flex">
          <input
            value={form.source_path}
            onChange={(e) => setForm({ ...form, source_path: e.target.value })}
          />
          <PathBrowser
            api={api}
            value={form.source_path}
            onPick={(path) => setForm({ ...form, source_path: path })}
          />
        </div>
      </Field>
      <Field label="Output folder" hint="Where cleaned copies go. Point a second Audiobookshelf library here.">
        <div className="flex">
          <input
            value={form.output_path}
            onChange={(e) => setForm({ ...form, output_path: e.target.value })}
          />
          <PathBrowser
            api={api}
            value={form.output_path}
            onPick={(path) => setForm({ ...form, output_path: path })}
          />
        </div>
      </Field>
      <Check
        label="Enabled"
        checked={form.enabled}
        onChange={(value) => setForm({ ...form, enabled: value })}
      />
      <Check
        label="Automatically clean new books"
        hint="Off means new books are catalogued but wait for you to queue them."
        checked={form.auto_process}
        onChange={(value) => setForm({ ...form, auto_process: value })}
      />
      <Field
        label="Layout"
        hint="Books: a folder is one work, split into parts. Episodes: a folder is a show and every file is cleaned on its own -- use this for podcasts."
      >
        <select
          value={form.layout}
          onChange={(e) => setForm({ ...form, layout: e.target.value })}
        >
          <option value="books">Books (audiobooks)</option>
          <option value="episodes">Episodes (podcasts)</option>
        </select>
      </Field>
      <Field
        label="Only these extensions"
        hint="Comma separated, e.g. .m4b,.mp3. Leave blank for all supported formats."
      >
        <input
          value={(form.extensions || []).join(',')}
          onChange={(e) =>
            setForm({
              ...form,
              extensions: e.target.value
                .split(',')
                .map((value) => value.trim())
                .filter(Boolean),
            })
          }
        />
      </Field>
      <Field label="Exclude patterns" hint="Comma separated globs, e.g. Samples/*, */Podcasts/*">
        <input
          value={(form.exclude_patterns || []).join(',')}
          onChange={(e) =>
            setForm({
              ...form,
              exclude_patterns: e.target.value
                .split(',')
                .map((value) => value.trim())
                .filter(Boolean),
            })
          }
        />
      </Field>
    </Modal>
  )
}

function IntegrationsTab({ draft, onChange }) {
  const toast = useToast()
  const abs = draft.integrations.audiobookshelf
  const plex = draft.integrations.plex
  const [libraries, setLibraries] = useState(null)
  const [plexResult, setPlexResult] = useState('')

  const loadLibraries = async () => {
    try {
      const body = await api.absLibraries({ url: abs.url, api_key: abs.api_key })
      setLibraries(body.libraries)
    } catch (err) {
      setLibraries(null)
      toast(err.message, 'error')
    }
  }

  const toggleLibrary = (id, on) => {
    const ids = new Set(abs.library_ids || [])
    if (on) ids.add(id)
    else ids.delete(id)
    onChange('audiobookshelf', 'library_ids', [...ids])
  }

  return (
    <>
      <div className="card mb">
        <div className="card-head">
          <h2>Audiobookshelf</h2>
        </div>
        <p className="dim" style={{ marginTop: 0 }}>
          After Prudify publishes a book, it asks Audiobookshelf to rescan, so the clean copy shows
          up straight away. A burst of books causes one scan, a minute after the last.
        </p>
        <div className="row">
          <Field label="Address" hint="As this machine reaches it, e.g. http://localhost:9090">
            <input
              value={abs.url}
              placeholder="http://localhost:9090"
              onChange={(e) => onChange('audiobookshelf', 'url', e.target.value)}
            />
          </Field>
          <Field label="API key" hint="Audiobookshelf → Settings → API Keys, from an admin user.">
            <input
              type="password"
              autoComplete="off"
              value={abs.api_key}
              onChange={(e) => onChange('audiobookshelf', 'api_key', e.target.value)}
            />
          </Field>
        </div>
        <div className="flex mb">
          <button className="small" disabled={!abs.url || !abs.api_key} onClick={loadLibraries}>
            Load libraries
          </button>
          <button
            className="small"
            disabled={!(abs.library_ids || []).length}
            title="Uses the saved settings"
            onClick={async () => {
              try {
                const result = await api.absScan()
                toast(
                  result.errors?.length ? result.errors[0] : 'Audiobookshelf is rescanning',
                  result.errors?.length ? 'error' : 'success',
                )
              } catch (err) {
                toast(err.message, 'error')
              }
            }}
          >
            Scan now
          </button>
        </div>
        {libraries ? (
          libraries.map((library) => (
            <Check
              key={library.id}
              label={library.name}
              hint={library.folders.join(', ')}
              checked={(abs.library_ids || []).includes(library.id)}
              onChange={(on) => toggleLibrary(library.id, on)}
            />
          ))
        ) : (abs.library_ids || []).length ? (
          <div className="faint">
            Rescanning {abs.library_ids.length} librar{abs.library_ids.length === 1 ? 'y' : 'ies'}.
            Load libraries to change which.
          </div>
        ) : null}
      </div>

      <div className="card mb">
        <div className="card-head">
          <h2>Plex</h2>
        </div>
        <p className="dim" style={{ marginTop: 0 }}>
          Used by the Schedule tab to wait while something is playing.
        </p>
        <div className="row">
          <Field label="Address">
            <input
              value={plex.url}
              onChange={(e) => onChange('plex', 'url', e.target.value)}
            />
          </Field>
          <Field
            label="Token"
            hint="Your X-Plex-Token; Plex's support article “Finding an authentication token” shows where."
          >
            <input
              type="password"
              autoComplete="off"
              value={plex.token}
              onChange={(e) => onChange('plex', 'token', e.target.value)}
            />
          </Field>
        </div>
        <div className="flex">
          <button
            className="small"
            disabled={!plex.url || !plex.token}
            onClick={async () => {
              try {
                const body = await api.plexTest({ url: plex.url, token: plex.token })
                setPlexResult(
                  `Connected: ${body.playing} stream${body.playing === 1 ? '' : 's'} playing now`,
                )
              } catch (err) {
                setPlexResult('')
                toast(err.message, 'error')
              }
            }}
          >
            Test
          </button>
          {plexResult ? <span className="faint">{plexResult}</span> : null}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2>Home Assistant</h2>
        </div>
        <p className="dim" style={{ margin: 0 }}>
          Install the Prudify integration through HACS: HACS → ⋮ → Custom repositories, add{' '}
          <code>https://github.com/Camwise1/prudify</code> as an <b>Integration</b>, install it,
          restart Home Assistant, then add “Prudify” under Settings → Devices &amp; services. It
          asks for this server&apos;s address and the API key on the Security tab.
        </p>
      </div>
    </>
  )
}

function SoundClip() {
  // The clip is stored on its own, outside the settings draft: it is a file,
  // and choosing one takes effect at once rather than on "Save changes".
  const toast = useToast()
  const [sound, setSound] = useState(undefined)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.sound().then((body) => setSound(body.sound)).catch(() => setSound(null))
  }, [])

  const upload = async (file) => {
    if (!file) return
    setBusy(true)
    try {
      const body = await api.uploadSound(file)
      setSound(body.sound)
      toast('Sound saved', 'success')
    } catch (err) {
      toast(err.message, 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mb">
      <Field
        label="Sound clip"
        hint="Any audio file. It plays from its start at each silenced word, cut off with a quick fade when the word ends, so the first half-second matters most. Leading silence is trimmed for you."
      >
        <div className="flex wrap">
          {sound ? (
            <span>
              <b>{sound.name}</b>{' '}
              <span className="faint">({sound.seconds.toFixed(1)} s)</span>
            </span>
          ) : sound === null ? (
            <span className="faint">None yet. Until there is one, hits are silenced.</span>
          ) : null}
          <label className={`btn small${busy ? ' disabled' : ''}`}>
            {busy ? 'Saving…' : sound ? 'Replace…' : 'Upload…'}
            <input
              type="file"
              accept="audio/*,.mp3,.wav,.m4a,.ogg,.flac"
              hidden
              disabled={busy}
              onChange={(e) => {
                upload(e.target.files?.[0])
                e.target.value = ''
              }}
            />
          </label>
          {sound ? (
            <button
              type="button"
              className="small danger"
              onClick={async () => {
                await api.deleteSound()
                setSound(null)
              }}
            >
              Remove
            </button>
          ) : null}
        </div>
      </Field>
    </div>
  )
}

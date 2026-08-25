import { useEffect, useRef, useState } from 'react'
import { api, ApiError, setApiKey } from '../lib/api.js'

/**
 * The gate in front of everything else.
 *
 * It handles three situations with one component, because to the person in
 * front of it they are the same moment -- "I opened Prudify and it wants
 * something from me":
 *
 *   setup   no account exists yet; create one
 *   login   an account exists; sign in
 *   apikey  the deployment authenticates with a key rather than an account
 */
export default function Login({ status, onAuthenticated }) {
  const mode = status?.needs_setup ? 'setup' : status?.supports_login ? 'login' : 'apikey'

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const firstField = useRef(null)

  useEffect(() => {
    firstField.current?.focus()
  }, [mode])

  const problem = () => {
    if (mode === 'setup') {
      if (username.trim().length < 1) return 'Choose a username.'
      if (password.length < 8) return 'Use at least 8 characters.'
      if (password !== confirm) return 'The passwords do not match.'
    }
    return ''
  }

  async function submit(event) {
    event.preventDefault()
    const local = problem()
    if (local) {
      setError(local)
      return
    }
    setError('')
    setBusy(true)
    try {
      if (mode === 'setup') {
        await api.setupAccount(username.trim(), password)
      } else if (mode === 'login') {
        await api.login(username.trim(), password)
      } else {
        setApiKey(key.trim())
        // Prove the key works before letting the app render, so a typo is
        // reported here instead of as a wall of failures behind it.
        await api.status()
      }
      onAuthenticated()
    } catch (err) {
      if (mode === 'apikey') setApiKey('')
      setError(
        err instanceof ApiError
          ? err.message
          : 'Something went wrong. Check the server logs.'
      )
      setPassword('')
      setConfirm('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="login-brand">
          <div className="brand-mark">P</div>
          <div>
            <h1>Prudify</h1>
            <p className="muted">
              {mode === 'setup'
                ? 'Create your account to get started'
                : mode === 'login'
                  ? 'Sign in to continue'
                  : 'Enter your API key to continue'}
            </p>
          </div>
        </div>

        {mode === 'setup' && (
          <p className="login-note">
            This is the only account. Prudify runs on your own machine, so
            there is no password reset by email — if you lose it, run{' '}
            <code>prudify auth set-password</code> on the server.
          </p>
        )}

        {mode !== 'apikey' && (
          <>
            <label className="field">
              <span>Username</span>
              <input
                ref={firstField}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoCapitalize="none"
                spellCheck="false"
                disabled={busy}
              />
            </label>
            <label className="field">
              <span>Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === 'setup' ? 'new-password' : 'current-password'}
                disabled={busy}
              />
            </label>
          </>
        )}

        {mode === 'setup' && (
          <label className="field">
            <span>Confirm password</span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              disabled={busy}
            />
          </label>
        )}

        {mode === 'apikey' && (
          <label className="field">
            <span>API key</span>
            <input
              ref={firstField}
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="Run: prudify config --reveal-key"
              autoComplete="off"
              spellCheck="false"
              disabled={busy}
            />
          </label>
        )}

        {error && (
          <div className="login-error" role="alert">
            {error}
          </div>
        )}

        <button className="primary login-submit" type="submit" disabled={busy}>
          {busy
            ? 'Working…'
            : mode === 'setup'
              ? 'Create account'
              : mode === 'login'
                ? 'Sign in'
                : 'Continue'}
        </button>
      </form>
    </div>
  )
}

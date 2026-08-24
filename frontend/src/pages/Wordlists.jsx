import React, { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { Banner, useToast } from '../components/ui.jsx'

const SAMPLE =
  'He said the classic line, then muttered something about a motherfucker in Scunthorpe.'

export default function Wordlists({ settings, onSettingsSaved }) {
  const toast = useToast()
  const [lists, setLists] = useState([])
  const [name, setName] = useState('strict')
  const [content, setContent] = useState('')
  const [builtin, setBuiltin] = useState(true)
  const [ruleCount, setRuleCount] = useState(0)
  const [dirty, setDirty] = useState(false)
  const [sample, setSample] = useState(SAMPLE)
  const [tested, setTested] = useState(null)

  const loadLists = async () => {
    try {
      setLists(await api.wordlists())
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const loadOne = async (target) => {
    try {
      const list = await api.wordlist(target)
      setName(list.name)
      setContent(list.content)
      setBuiltin(list.builtin)
      setRuleCount(list.rule_count)
      setDirty(false)
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  useEffect(() => {
    loadLists()
  }, [])

  useEffect(() => {
    loadOne(name)
  }, [])

  const save = async () => {
    try {
      const saved = await api.saveWordlist(name, content)
      setRuleCount(saved.rule_count)
      setBuiltin(false)
      setDirty(false)
      toast(`Saved ${name} (${saved.rule_count} rules)`, 'success')
      loadLists()
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const runTest = async () => {
    try {
      setTested(await api.testWords({ text: sample, wordlist: name }))
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const useThisList = async () => {
    try {
      await api.saveSettings({ filtering: { ...settings.filtering, wordlist: name } })
      toast(`Prudify will now filter with “${name}”`, 'success')
      onSettingsSaved?.()
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const activeList = settings?.filtering?.wordlist

  return (
    <div className="page">
      <div className="toolbar">
        <select
          value={name}
          onChange={(event) => loadOne(event.target.value)}
          style={{ maxWidth: 240 }}
        >
          {[...new Set(lists.map((list) => list.name))].map((listName) => (
            <option key={listName} value={listName}>
              {listName}
              {listName === activeList ? '  (in use)' : ''}
            </option>
          ))}
        </select>
        <button
          onClick={() => {
            const next = window.prompt('Name for the new list (letters, numbers, - and _):')
            if (!next) return
            setName(next.replace(/[^A-Za-z0-9_-]/g, ''))
            setContent('# One entry per line.\n# word   word*   two words   /regex/\n\n')
            setBuiltin(false)
            setDirty(true)
          }}
        >
          New list
        </button>
        <div className="spacer" />
        {activeList !== name ? (
          <button onClick={useThisList}>Use this list for filtering</button>
        ) : (
          <span className="pill cleaned">Currently in use</span>
        )}
        <button className="primary" disabled={!dirty} onClick={save}>
          Save
        </button>
      </div>

      {builtin ? (
        <Banner tone="info">
          <div>
            <b>{name}</b> is a bundled list. Saving creates your own copy that shadows it, so
            upgrades never overwrite your edits.
          </div>
        </Banner>
      ) : null}

      <div className="grid cols-2">
        <div className="card">
          <div className="card-head">
            <h2>{name}.txt</h2>
            <span className="spacer faint">{ruleCount} rules</span>
          </div>
          <textarea
            value={content}
            spellCheck={false}
            onChange={(event) => {
              setContent(event.target.value)
              setDirty(true)
            }}
            style={{ minHeight: 420 }}
          />
          <div className="faint mt" style={{ fontSize: 12 }}>
            <code>word</code> exact token · <code>word*</code> prefix (needs prefix or fuzzy mode) ·{' '}
            <code>two words</code> phrase · <code>/regex/</code> · <code>#</code> comment
          </div>
        </div>

        <div className="card">
          <div className="card-head">
            <h2>Try it</h2>
          </div>
          <p className="dim" style={{ marginTop: 0 }}>
            Paste a line of narration to see exactly what would be silenced. This uses the saved
            list, so save your edits first.
          </p>
          <textarea
            value={sample}
            onChange={(event) => setSample(event.target.value)}
            style={{ minHeight: 90 }}
          />
          <div className="flex mt mb">
            <button className="primary" onClick={runTest}>
              Test
            </button>
            {tested ? (
              <span className="dim">
                {tested.match_count} match{tested.match_count === 1 ? '' : 'es'}
              </span>
            ) : null}
          </div>
          {tested ? (
            <div className="tester">
              {tested.tokens.map((token, index) => (
                <span key={index}>
                  <span className={token.matched ? 'hit' : ''} title={token.rule}>
                    {token.text}
                  </span>{' '}
                </span>
              ))}
            </div>
          ) : null}

          <div className="card-head mt">
            <h2>Allowlist</h2>
          </div>
          <p className="dim" style={{ marginTop: 0 }}>
            Words in <code>allowlist</code> are never silenced, whatever the wordlist says. That is
            what keeps “Scunthorpe” and “classic” intact when prefix matching is on.
          </p>
          <button onClick={() => loadOne('allowlist')}>Edit the allowlist</button>
        </div>
      </div>
    </div>
  )
}

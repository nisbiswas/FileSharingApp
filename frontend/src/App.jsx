import { useEffect, useRef, useState } from 'react'
import './App.css'

const uuidPattern = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
const sizeLabel = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2
  ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`
const expiresLabel = seconds => new Date(seconds * 1000).toLocaleString([], {
  month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
})
function newToken() {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('')
}
async function api(url, options) {
  const response = await fetch(url, options)
  if (!response.ok) {
    const result = await response.json().catch(() => ({}))
    const retry = response.headers.get('Retry-After')
    throw new Error((typeof result.detail === 'string' ? result.detail : 'Transfer unavailable.') +
      (retry ? ` Try again in ${retry} seconds.` : ''))
  }
  return response.status === 204 ? null : response.json()
}

function Send() {
  const [file, setFile] = useState(null)
  const [ttl, setTtl] = useState('24')
  const [busy, setBusy] = useState(false)
  const [drag, setDrag] = useState(false)
  const [receipt, setReceipt] = useState(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem('sender-receipt'))
      return saved?.expires_at > Date.now() / 1000 ? saved : null
    } catch { return null }
  })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const token = useRef(null)
  const picker = useRef(null)
  const linkField = useRef(null)
  const link = receipt ? `${location.origin}${location.pathname}#share=${receipt.file_uuid}` : ''
  useEffect(() => {
    try {
      if (receipt) sessionStorage.setItem('sender-receipt', JSON.stringify(receipt))
      else sessionStorage.removeItem('sender-receipt')
    } catch { /* The current page still holds the sender controls. */ }
  }, [receipt])

  function select(next) {
    if (busy || !next) return
    token.current = null
    setNotice('')
    if (next.size > 10 * 1024 ** 2) {
      setFile(null)
      setError('Choose a file of 10 MiB or less.')
      return
    }
    setFile(next)
    setError('')
  }
  async function upload(event) {
    event.preventDefault()
    if (!file || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    token.current ??= newToken()
    const body = new FormData()
    body.append('file', file)
    try {
      setReceipt(await api(`/api/v1/upload?ttl_hours=${ttl}`, {
        method: 'POST', body, headers: { Authorization: `Bearer ${token.current}` },
      }))
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }
  async function revoke() {
    setBusy(true)
    setError('')
    try {
      await api(`/api/v1/shares/${receipt.file_uuid}`, {
        method: 'DELETE', headers: { Authorization: `Bearer ${receipt.owner_token}` },
      })
      restart()
      setNotice('Link revoked. Recipients can no longer open it.')
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }
  function restart() {
    setReceipt(null)
    setFile(null)
    setError('')
    setNotice('')
    token.current = null
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(link)
      setNotice('Link copied.')
    } catch {
      linkField.current.select()
      setNotice('Link selected. Use your copy shortcut.')
    }
  }
  return <>
    <h2>{receipt ? 'Your link is ready' : 'Create a transfer'}</h2>
    {receipt ? <>
      <div className="details"><strong>{receipt.filename}</strong><small>{sizeLabel(receipt.size_bytes)} · Expires {expiresLabel(receipt.expires_at)}</small></div>
      <label htmlFor="share-url">Recipient link</label>
      <input id="share-url" ref={linkField} value={link} readOnly />
      <button className="primary" onClick={copy}>Copy link <span aria-hidden="true">↗</span></button>
      <p className="hint">This tab keeps the revoke control until you start a new transfer.</p>
      <div className="actions"><button disabled={busy} onClick={restart}>New transfer</button><button disabled={busy} onClick={revoke}>Revoke link</button></div>
    </> : <form onSubmit={upload}>
      <button type="button" className={`drop ${drag ? 'over' : ''}`} disabled={busy}
        onClick={() => picker.current.click()} onDragOver={e => { e.preventDefault(); setDrag(true) }}
        onDragLeave={() => setDrag(false)} onDrop={e => {
          e.preventDefault(); setDrag(false); select(e.dataTransfer.files[0])
        }}>
        <span className="file-icon" aria-hidden="true">↑</span>
        <strong>{file ? file.name : 'Choose a file'}</strong>
        <small>{file ? `${sizeLabel(file.size)} · Click to replace` : 'or drop it here · Up to 10 MiB'}</small>
      </button>
      <input hidden type="file" ref={picker} onChange={e => { select(e.target.files[0]); e.target.value = '' }} />
      <label htmlFor="ttl">Link expires after</label>
      <select id="ttl" disabled={busy} value={ttl} onChange={e => { setTtl(e.target.value); token.current = null }}>
        <option value="1">1 hour</option><option value="6">6 hours</option><option value="24">24 hours</option>
      </select>
      <button className="primary" disabled={!file || busy}>{busy ? 'Creating link…' : 'Create share link'} <span aria-hidden="true">→</span></button>
    </form>}
    {notice && <p role="status">{notice}</p>}{error && <p className="error" role="alert">{error}</p>}
  </>
}

function Receive({ initial }) {
  const [input, setInput] = useState(initial)
  const [id, setId] = useState(initial)
  const [info, setInfo] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!id) return
    if (!uuidPattern.test(id)) { setError('Enter a full transfer ID or recipient link.'); return }
    const controller = new AbortController()
    setBusy(true)
    api(`/api/v1/shares/${id}`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setInfo(data) })
      .catch(err => { if (!controller.signal.aborted) setError(err.message) })
      .finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [id, attempt])
  return <>
    <h2>Receive a file</h2>
    <form onSubmit={e => {
      e.preventDefault()
      setId(input.trim().split('#share=').pop())
      setAttempt(n => n + 1)
      setInfo(null)
      setError('')
    }}>
      <label htmlFor="transfer">Recipient link or transfer ID</label>
      <input id="transfer" value={input} required onChange={e => {
        setInput(e.target.value); setInfo(null); setError(''); setId(''); setBusy(false)
      }} placeholder="Paste a link or UUID" />
      <button className="primary" disabled={!input.trim() || busy}>{busy ? 'Looking up file…' : 'Find file'} <span aria-hidden="true">→</span></button>
    </form>
    {info && <div className="details" role="status"><strong>{info.filename}</strong>
      <small>{sizeLabel(info.size_bytes)} · Expires {expiresLabel(info.expires_at)}</small>
      <a className="download" href={`/api/v1/download/${info.file_uuid}`} download>Download file ↓</a>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </>
}

function fragment() {
  return location.hash.startsWith('#share=') ? location.hash.slice(7) : ''
}
export default function App() {
  const [initial, setInitial] = useState(fragment)
  const [mode, setMode] = useState(initial ? 'receive' : 'send')
  useEffect(() => {
    function navigate() {
      const value = fragment()
      setInitial(value)
      setMode(value ? 'receive' : 'send')
    }
    window.addEventListener('hashchange', navigate)
    return () => window.removeEventListener('hashchange', navigate)
  }, [])
  return <main>
    <header><span className="brandmark" aria-hidden="true">↗</span> FileSharingApp</header>
    <div className="layout"><section className="intro">
      <h1>Share a file.<br /><em>Set its lifetime.</em></h1>
      <p>Send a download link that expires on your schedule. Anyone with the link can retrieve the file.</p>
      <div className="paper-art" aria-hidden="true"><i /><i /><i><b>↗</b></i></div>
    </section>
    <section className="panel" aria-label="File transfer">
      <nav aria-label="Transfer action">{['send', 'receive'].map(item =>
        <button key={item} aria-pressed={mode === item} onClick={() => setMode(item)}>{item === 'send' ? 'Send' : 'Receive'}</button>)}</nav>
      <div hidden={mode !== 'send'}><Send /></div>
      <div hidden={mode !== 'receive'}><Receive key={initial} initial={initial} /></div>
    </section></div>
  </main>
}

import { useCallback, useRef, useState } from 'react'
import { uploadDocument } from '../../api/documents.api.js'
import { formatBytes } from '../../lib/format.js'
import Button from '../ui/Button.jsx'
import Spinner from '../ui/Spinner.jsx'

/**
 * Drag-and-drop (or click-to-browse) uploader for the document list.
 *
 * Files are uploaded one at a time rather than in one batch request, because
 * the endpoint takes a single `file` per call. Each result is reported
 * separately so one rejected file does not hide the ones that succeeded.
 *
 * Size and type are checked here *and* server-side. The client check exists
 * only to fail fast on an obvious mistake — the server does not trust the
 * declared Content-Type at all, it sniffs the magic bytes, so a renamed `.exe`
 * is rejected there no matter what this component thinks.
 */
export default function UploadDropzone({ limits, onUploaded }) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState([])

  const maxBytes = limits?.maxUploadMb ? limits.maxUploadMb * 1024 * 1024 : null
  const acceptedTypes = limits?.acceptedTypes ?? []

  const rejectReason = useCallback(
    (file) => {
      if (maxBytes && file.size > maxBytes) {
        return `is ${formatBytes(file.size)} — the limit is ${limits.maxUploadMb} MB`
      }
      // An empty `accept` list means the server did not tell us, so anything
      // goes and the server decides.
      if (acceptedTypes.length > 0 && file.type && !acceptedTypes.includes(file.type)) {
        return `is a ${file.type} file, which is not accepted`
      }
      return null
    },
    [maxBytes, acceptedTypes, limits],
  )

  const send = useCallback(
    async (files) => {
      const list = Array.from(files)
      if (list.length === 0) return

      setBusy(true)
      const outcome = []

      for (const file of list) {
        const reason = rejectReason(file)
        if (reason) {
          outcome.push({ name: file.name, tone: 'error', message: `${file.name} ${reason}.` })
          continue
        }
        try {
          const { document, deduplicated } = await uploadDocument(file)
          outcome.push({
            name: file.name,
            tone: deduplicated ? 'info' : 'success',
            message: deduplicated
              ? `${file.name} was already uploaded — showing the original (#${document.id}).`
              : `${file.name} accepted. Extraction is running; the row updates as it finishes.`,
            documentId: document.id,
          })
        } catch (err) {
          outcome.push({ name: file.name, tone: 'error', message: `${file.name}: ${err.message}` })
        }
      }

      setResults(outcome)
      setBusy(false)
      // Refresh the list even on a partial failure: anything that did upload
      // should appear immediately rather than after a manual refresh.
      if (outcome.some((entry) => entry.tone !== 'error')) onUploaded?.()
    },
    [rejectReason, onUploaded],
  )

  function handleDrop(event) {
    event.preventDefault()
    setDragging(false)
    if (!busy) send(event.dataTransfer.files)
  }

  return (
    <div className="upload">
      <div
        className={`dropzone ${dragging ? 'dropzone--active' : ''} ${busy ? 'dropzone--busy' : ''}`}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <input
          ref={inputRef}
          className="dropzone__input"
          type="file"
          multiple
          accept={acceptedTypes.join(',') || undefined}
          disabled={busy}
          onChange={(event) => {
            send(event.target.files)
            // Reset so picking the same file again still fires onChange — the
            // common "it did nothing the second time" bug with file inputs.
            event.target.value = ''
          }}
        />

        {busy ? (
          <div className="dropzone__body">
            <Spinner size={22} />
            <p className="dropzone__title">Uploading…</p>
          </div>
        ) : (
          <div className="dropzone__body">
            <span className="dropzone__icon" aria-hidden>⬆</span>
            <p className="dropzone__title">Drop files here, or</p>
            <Button variant="ghost" className="btn--sm" onClick={() => inputRef.current?.click()}>
              Choose files
            </Button>
            <p className="dropzone__hint">
              {acceptedTypes.length > 0
                ? `${acceptedTypes.map(shortType).join(', ')} · up to ${limits.maxUploadMb} MB each`
                : 'PDF, PNG, JPEG, TIFF or plain text'}
            </p>
          </div>
        )}
      </div>

      {results.length > 0 && (
        <ul className="upload__results">
          {results.map((entry, index) => (
            <li className={`upload__result upload__result--${entry.tone}`} key={`${entry.name}-${index}`}>
              {entry.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** "application/pdf" -> "PDF", "image/jpeg" -> "JPEG". */
function shortType(mime) {
  const subtype = mime.split('/')[1] ?? mime
  return subtype === 'plain' ? 'TXT' : subtype.toUpperCase()
}

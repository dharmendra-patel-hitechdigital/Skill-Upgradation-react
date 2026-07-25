import { useId } from 'react'

/**
 * Labeled text input with optional error + trailing slot (e.g. show/hide toggle).
 */
export default function Input({ label, error, trailing, className = '', ...rest }) {
  const id = useId()
  return (
    <div className={`field ${error ? 'field--error' : ''} ${className}`}>
      {label && (
        <label htmlFor={id} className="field__label">
          {label}
        </label>
      )}
      <div className="field__control">
        <input id={id} className="field__input" {...rest} />
        {trailing && <div className="field__trailing">{trailing}</div>}
      </div>
      {error && <p className="field__error">{error}</p>}
    </div>
  )
}

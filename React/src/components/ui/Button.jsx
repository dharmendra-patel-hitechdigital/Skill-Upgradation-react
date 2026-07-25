import Spinner from './Spinner.jsx'

/**
 * @param {{ variant?: 'primary'|'ghost'|'danger', loading?: boolean, fullWidth?: boolean }} props
 */
export default function Button({
  variant = 'primary',
  loading = false,
  fullWidth = false,
  children,
  className = '',
  disabled,
  ...rest
}) {
  return (
    <button
      className={`btn btn--${variant} ${fullWidth ? 'btn--block' : ''} ${className}`}
      disabled={disabled || loading}
      {...rest}
    >
      {loading && <Spinner size={16} />}
      <span>{children}</span>
    </button>
  )
}

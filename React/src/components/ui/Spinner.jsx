export default function Spinner({ size = 20 }) {
  return (
    <span
      className="spinner"
      style={{ width: size, height: size, borderWidth: Math.max(2, size / 8) }}
      role="status"
      aria-label="Loading"
    />
  )
}

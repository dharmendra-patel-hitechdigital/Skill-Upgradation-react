import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Run an async API function on mount (and on demand), tracking
 * loading / error / data state. Cancels in-flight work on unmount.
 *
 * @param {(opts: { signal: AbortSignal }) => Promise<any>} apiFn
 * @param {{ immediate?: boolean, deps?: any[] }} [options]
 */
export function useApi(apiFn, { immediate = true, deps = [] } = {}) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(immediate)
  const controllerRef = useRef(null)

  const execute = useCallback(async () => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller

    setLoading(true)
    setError(null)
    try {
      const result = await apiFn({ signal: controller.signal })
      if (!controller.signal.aborted) setData(result)
      return result
    } catch (err) {
      if (err.name !== 'AbortError') setError(err)
      throw err
    } finally {
      if (!controller.signal.aborted) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (immediate) execute().catch(() => {})
    return () => controllerRef.current?.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [execute, immediate])

  return { data, error, loading, refetch: execute }
}

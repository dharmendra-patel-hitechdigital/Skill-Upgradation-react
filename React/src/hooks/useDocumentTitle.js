import { useEffect } from 'react'

/** Set document.title and restore the previous title on unmount. */
export function useDocumentTitle(title) {
  useEffect(() => {
    const prev = document.title
    document.title = title
    return () => {
      document.title = prev
    }
  }, [title])
}

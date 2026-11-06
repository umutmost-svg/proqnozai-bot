import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

/** History back when there is in-app history, otherwise a sensible parent route. */
export function useGoBack(fallback = '/') {
  const navigate = useNavigate();
  return useCallback(() => {
    const idx = (window.history.state as { idx?: number } | null)?.idx ?? 0;
    if (idx > 0) navigate(-1);
    else navigate(fallback, { replace: true });
  }, [navigate, fallback]);
}

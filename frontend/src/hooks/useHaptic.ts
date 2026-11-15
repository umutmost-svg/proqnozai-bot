import { useCallback } from 'react';
import { useServices, useSettings } from '../state/contexts';

/** Selection haptic in Telegram when enabled in settings; no-op elsewhere. */
export function useHaptic() {
  const { telegram } = useServices();
  const { settings } = useSettings();
  return useCallback(() => {
    if (settings.haptics) telegram.haptic('selection');
  }, [settings.haptics, telegram]);
}

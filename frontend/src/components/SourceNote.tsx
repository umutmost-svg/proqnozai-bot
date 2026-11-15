import type { EventSource, Sport } from '../data/models';
import { formatDateTime, formatLongDate, zoneLabel } from '../i18n/format';
import { useServices, useSettings } from '../state/contexts';
import type { DayKey } from '../time/zoned';
import ui from './ui.module.css';

/**
 * Page footer: the demo mark in demo mode; with real data the date, zone and the
 * provider attribution its terms require.
 */
export function DataFootnote({ sport, today }: { sport: Sport; today: DayKey }) {
  const { data } = useServices();
  const { t, timeZone } = useSettings();
  const params = { date: formatLongDate(t, today), zone: zoneLabel(t, timeZone) };
  if (data.isDemo) {
    return (
      <p className={ui.footnote} data-testid="demo-footer">
        {t('footer.demo', params)}
      </p>
    );
  }
  const { attribution } = data.coverage(sport);
  return (
    <p className={ui.footnote} data-testid="source-footer">
      {t('footer.real', params)}
      {attribution && (
        <>
          <br />
          {attribution}
        </>
      )}
    </p>
  );
}

/** Under a real event: who supplied it, when, and whether it is delayed or stale. */
export function SourceNote({ source }: { source: EventSource }) {
  const { t, timeZone } = useSettings();
  const time = formatDateTime(new Date(source.fetchedAt), timeZone);
  // "Updated" is the provider's own update time when it gives one; otherwise when we received it.
  const updated = formatDateTime(new Date(source.sourceUpdatedAt ?? source.fetchedAt), timeZone);
  return (
    <div data-testid="source-note">
      {source.stale && (
        <p className={ui.notice} data-testid="stale-note">
          {t('event.staleNote', { time })}
        </p>
      )}
      {source.delayed && <p className={ui.notice}>{t('event.delayedNote')}</p>}
      <p className={ui.footnote}>
        {t('event.sourceNote', { provider: source.provider, time: updated })}
        {source.attribution && (
          <>
            <br />
            {source.attribution}
          </>
        )}
      </p>
    </div>
  );
}

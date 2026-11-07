import type { ReactNode } from 'react';
import type { Sport } from '../data/models';
import { useServices, useT } from '../state/contexts';
import { EmptyState } from './States';

/**
 * Empty result. When the sport has no real data source at all, that is said
 * instead of "no matches this day" — and nothing is substituted.
 */
export function NoEvents({ sport, title, text, actions }: { sport: Sport; title: string; text: string; actions?: ReactNode }) {
  const { data } = useServices();
  const t = useT();
  if (data.coverage(sport).status === 'not_connected') {
    return (
      <div data-testid="not-connected">
        <EmptyState title={t('catalog.notConnectedTitle')} text={t('catalog.notConnectedText')} />
      </div>
    );
  }
  return <EmptyState title={title} text={text} actions={actions} />;
}

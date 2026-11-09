import { useNavigate } from 'react-router-dom';
import { Emblem } from '../components/Emblem';
import { SourceErrorState, useBusyRetry } from '../components/BusyRetry';
import { EmptyState, LoadingState } from '../components/States';
import type { StandingBlock, TournamentStandings } from '../data/models';
import type { Translate } from '../i18n';
import { formatDateTime } from '../i18n/format';
import { useAsync } from '../hooks/useAsync';
import { useServices, useSettings } from '../state/contexts';
import s from './Standings.module.css';

/** "2026/27" for a season over two years, "2026" for a calendar-year one. */
export function seasonLabel(season: TournamentStandings['season']): string | null {
  const start = season?.startDate?.slice(0, 4);
  const end = season?.endDate?.slice(0, 4);
  if (!start) return null;
  return end && end !== start ? `${start}/${end.slice(2)}` : start;
}

const GROUP = /^GROUP_([A-Z0-9]{1,2})$/;

/** The block's heading: a known stage or group in the interface language, else the source's label as given. */
function blockTitle(t: Translate, block: StandingBlock): string | null {
  const group = block.group && GROUP.exec(block.group);
  if (group) return t('standings.group', { name: group[1]! });
  if (block.stage === 'LEAGUE_STAGE') return t('standings.stage.LEAGUE_STAGE');
  if (block.stage === 'GROUP_STAGE') return t('standings.stage.GROUP_STAGE');
  // An unfamiliar stage still gets a heading: the provider's own label, as given.
  return block.group ?? block.stage;
}

function Table({ block, title }: { block: StandingBlock; title: string | null }) {
  const { t } = useSettings();
  const navigate = useNavigate();
  const cols = ['played', 'diff', 'points', 'won', 'draw', 'lost', 'goals'] as const;
  return (
    <section className={s.block} data-testid="standings-block" data-stage={block.stage ?? undefined}>
      {title && <h3 className={s.blockTitle}>{title}</h3>}
      <div className={s.scroll} role="region" aria-label={title ?? t('tournament.tab.table')} tabIndex={0}>
        <table className={s.table}>
          <thead>
            <tr>
              <th scope="col" className={`${s.pos} ${s.sticky}`} title={t('standings.full.position')}>
                {t('standings.col.position')}
              </th>
              <th scope="col" className={`${s.team} ${s.stickyTeam}`}>
                {t('standings.col.team')}
              </th>
              {cols.map((c) => (
                <th key={c} scope="col" className={s.num} title={t(`standings.full.${c}`)}>
                  <abbr title={t(`standings.full.${c}`)}>{t(`standings.col.${c}`)}</abbr>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row) => (
              <tr key={row.team.id} data-testid="standings-row" data-team={row.team.id}>
                <td className={`${s.pos} ${s.sticky}`}>{row.position}</td>
                <td className={`${s.team} ${s.stickyTeam}`}>
                  <button
                    type="button"
                    className={s.teamButton}
                    aria-label={t('standings.rowLabel', {
                      position: row.position,
                      team: row.team.fullName ?? row.team.name,
                      played: row.played,
                      diff: row.goalDifference,
                      points: row.points,
                    })}
                    onClick={() => navigate(`/team/${encodeURIComponent(row.team.id)}`)}
                  >
                    <Emblem competitor={row.team} size={24} crestBox={18} variant="plain" />
                    <span className={s.teamName}>{row.team.name}</span>
                  </button>
                </td>
                <td className={s.num}>{row.played}</td>
                <td className={s.num}>{row.goalDifference > 0 ? `+${row.goalDifference}` : row.goalDifference}</td>
                <td className={`${s.num} ${s.points}`}>{row.points}</td>
                <td className={s.num}>{row.won}</td>
                <td className={s.num}>{row.draw}</td>
                <td className={s.num}>{row.lost}</td>
                <td className={s.num}>{`${row.goalsFor}:${row.goalsAgainst}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** The table tab: the provider's own order and numbers, nothing recomputed or re-sorted. */
export function StandingsTab({ id, provider }: { id: string; provider: string | null }) {
  const { data } = useServices();
  const { t, timeZone } = useSettings();
  const standings = useAsync(() => data.getStandings(id), `standings:${id}`);
  const { busy, retry } = useBusyRetry(standings, `standings:${id}`);
  if (standings.status === 'loading') {
    return <LoadingState title={t('standings.loadingTitle')} text={t('standings.loadingText')} />;
  }
  if (standings.status === 'error') {
    return <SourceErrorState busy={busy} title={t('standings.errorTitle')} text={t('standings.errorText')} onRetry={retry} />;
  }
  const table = standings.data;
  if (!table || table.blocks.length === 0 || table.blocks.every((b) => b.rows.length === 0)) {
    return <EmptyState title={t('standings.emptyTitle')} text={t('standings.emptyText')} />;
  }
  const season = seasonLabel(table.season);
  const several = table.blocks.length > 1;
  return (
    <div className={s.root} data-testid="standings">
      {season && <p className={s.meta}>{t('standings.season', { season })}</p>}
      {table.stale && <p className={s.stale}>{t('standings.stale')}</p>}
      {table.blocks.map((block, i) => (
        <Table key={`${block.stage}:${block.group}:${i}`} block={block} title={several || (block.stage !== null && block.stage !== 'REGULAR_SEASON') ? blockTitle(t, block) : null} />
      ))}
      <p className={s.hint}>{t('standings.scrollHint')}</p>
      <p className={s.hint}>{t('standings.order')}</p>
      {provider && table.fetchedAt && (
        <p className={s.source} data-testid="standings-source">
          {t('standings.source', { provider, date: formatDateTime(new Date(table.fetchedAt), timeZone) })}
        </p>
      )}
    </div>
  );
}

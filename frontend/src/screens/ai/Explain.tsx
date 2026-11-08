import type { AiFactor, TeamBasis, Tournament } from '../../data/models';
import { listOf, plural, type Lang, type Translate } from '../../i18n';
import { useSettings } from '../../state/contexts';
import s from './Ai.module.css';

type Basis = { home: TeamBasis; away: TeamBasis };

/** "in the last 5 matches" with the language's own number agreement. */
function lastMatches(t: Translate, lang: Lang, n: number): string {
  return plural(t, lang, 'ai.n.lastMatches', n);
}

/** One factor as a natural sentence, built only from that team's verified numbers. */
export function factorSentence(t: Translate, lang: Lang, factor: AiFactor, basis: Basis): string {
  const team = factor.startsWith('home') ? basis.home : basis.away;
  if (factor.endsWith('Form')) {
    return t('ai.sentence.form', {
      team: team.name,
      last: lastMatches(t, lang, team.matches),
      wins: plural(t, lang, 'ai.n.wins', team.wins),
      draws: plural(t, lang, 'ai.n.draws', team.draws),
      losses: plural(t, lang, 'ai.n.losses', team.losses),
      points: plural(t, lang, 'ai.n.points', team.points),
    });
  }
  if (factor.endsWith('Goals')) {
    return t('ai.sentence.goals', {
      team: team.name,
      last: lastMatches(t, lang, team.matches),
      scored: plural(t, lang, 'ai.n.goals', team.goalsFor),
      conceded: team.goalsAgainst,
    });
  }
  return t('ai.sentence.table', {
    team: team.name,
    position: team.tablePosition ?? 0,
    points: plural(t, lang, 'ai.n.pointsWith', team.tablePoints ?? 0),
  });
}

export function Factors({ factors, basis }: { factors: readonly AiFactor[]; basis: Basis }) {
  const { t, settings } = useSettings();
  return (
    <section className={s.card} data-testid="ai-factors">
      <h3 className={s.cardTitle}>{t('ai.factorsTitle')}</h3>
      <ul className={s.list}>
        {factors.map((f) => (
          <li key={f}>{factorSentence(t, settings.lang, f, basis)}</li>
        ))}
      </ul>
    </section>
  );
}

const LETTER_INDEX: Record<string, number> = { W: 0, D: 1, L: 2 };

/** The form as chips, newest on the left, letters in the interface language. */
function FormChips({ form }: { form: string }) {
  const { t } = useSettings();
  const letters = listOf(t, 'ai.formLetters');
  const names = listOf(t, 'ai.formNames');
  return (
    <span className={s.form} aria-label={form.split('').map((r) => names[LETTER_INDEX[r]!]).join(', ')}>
      {form.split('').map((r, i) => (
        <span key={i} className={`${s.formChip} ${r === 'W' ? s.formW : r === 'D' ? s.formD : s.formL}`} aria-hidden="true">
          {letters[LETTER_INDEX[r]!]}
        </span>
      ))}
    </span>
  );
}

function TeamFacts({ team }: { team: TeamBasis }) {
  const { t, settings } = useSettings();
  const lang = settings.lang;
  return (
    <li className={s.teamFacts}>
      <strong>{team.name}</strong>
      <FormChips form={team.form} />
      <span>
        {t('ai.facts.record', {
          last: lastMatches(t, lang, team.matches),
          wins: plural(t, lang, 'ai.n.wins', team.wins),
          draws: plural(t, lang, 'ai.n.draws', team.draws),
          losses: plural(t, lang, 'ai.n.losses', team.losses),
        })}
      </span>
      <span>
        {t('ai.facts.goals', {
          scored: team.goalsFor,
          conceded: team.goalsAgainst,
          points: plural(t, lang, 'ai.n.points', team.points),
        })}
      </span>
      {team.tablePosition !== null && team.tablePoints !== null && (
        <span>{t('ai.facts.table', { position: team.tablePosition, points: plural(t, lang, 'ai.n.points', team.tablePoints) })}</span>
      )}
    </li>
  );
}

/** Verified numbers, what they cover, and whose data they are. */
export function Facts({ basis, tournament, provider, attribution }: { basis: Basis; tournament?: Tournament; provider: string | null; attribution: string | null }) {
  const { t } = useSettings();
  const hasTable = basis.home.tablePosition !== null || basis.away.tablePosition !== null;
  return (
    <section className={s.card} data-testid="ai-basis">
      <h3 className={s.cardTitle}>{t('ai.factsTitle')}</h3>
      <ul className={s.teams}>
        <TeamFacts team={basis.home} />
        <TeamFacts team={basis.away} />
      </ul>
      <ul className={s.notes}>
        <li>{t('ai.facts.formNote')}</li>
        {hasTable && <li>{t('ai.facts.tableNote', { tournament: tournament?.name ?? '' })}</li>}
      </ul>
      {provider && (
        <p className={s.source} data-testid="ai-source">
          {t('ai.basisSource', { provider })}
          {attribution && (
            <>
              <br />
              {attribution}
            </>
          )}
        </p>
      )}
    </section>
  );
}

export function Limitations({ hasTable }: { hasTable: boolean }) {
  const { t } = useSettings();
  return (
    <section className={s.card} data-testid="ai-limitations">
      <h3 className={s.cardTitle}>{t('ai.limitsTitle')}</h3>
      <ul className={s.list}>
        <li>{t('ai.limits.odds')}</li>
        <li>{t('ai.limits.lineups')}</li>
        <li>{t(hasTable ? 'ai.limits.data' : 'ai.limits.dataNoTable')}</li>
        <li>{t('ai.limits.guarantee')}</li>
      </ul>
    </section>
  );
}

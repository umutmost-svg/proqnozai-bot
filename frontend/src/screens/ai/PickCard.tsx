import type { AiPick, PickReason, SportEvent, TeamBasis } from '../../data/models';
import type { Translate } from '../../i18n';
import { plural } from '../../i18n';
import { useSettings } from '../../state/contexts';
import s from './Ai.module.css';

type Basis = { home: TeamBasis; away: TeamBasis };

/** "Liverpool won't lose" etc. — built from the market and the teams, nothing else. */
export function pickOutcomeText(t: Translate, market: AiPick['market'], home: string, away: string): string {
  return t(`ai.pick.outcome.${market}`, { home, away });
}

/** A reason sentence from the verified numbers of the side it names. */
export function pickReasonText(t: Translate, lang: Parameters<typeof plural>[1], reason: PickReason, b: Basis): string {
  const side = reason.startsWith('home') ? 'home' : 'away';
  const team = b[side];
  const other = b[side === 'home' ? 'away' : 'home'];
  const points = (n: number) => plural(t, lang, 'ai.n.points', n);
  switch (reason) {
    case 'homeBetterForm':
    case 'awayBetterForm':
      return t('ai.pick.reason.betterForm', { team: team.name, points: points(team.points), other: other.name, otherPoints: points(other.points) });
    case 'homeHigherTable':
    case 'awayHigherTable':
      return t('ai.pick.reason.higherTable', { team: team.name, position: team.tablePosition ?? 0, other: other.name, otherPosition: other.tablePosition ?? 0 });
    case 'homeMoreGoals':
    case 'awayMoreGoals':
      return t('ai.pick.reason.moreGoals', { team: team.name, goals: plural(t, lang, 'ai.n.goals', team.goalsFor), other: other.name, otherGoals: other.goalsFor });
    case 'homeFewerConceded':
    case 'awayFewerConceded':
      return t('ai.pick.reason.fewerConceded', { team: team.name, goals: plural(t, lang, 'ai.n.goals', team.goalsAgainst), other: other.name, otherGoals: other.goalsAgainst });
    case 'evenlyMatched':
      return t('ai.pick.reason.evenlyMatched', { home: b.home.name, away: b.away.name, homePoints: points(b.home.points), awayPoints: points(b.away.points) });
  }
}

/** The arithmetic behind the probability: which outcomes it adds up. */
function arithmetic(t: Translate, pick: AiPick, values: readonly number[], home: string, away: string): string {
  const [h, d, a] = values as [number, number, number];
  switch (pick.market) {
    case '1X':
      return t('ai.pick.sum.double', { first: home, firstP: h, second: t('ai.draw'), secondP: d, total: h + d });
    case 'X2':
      return t('ai.pick.sum.double', { first: away, firstP: a, second: t('ai.draw'), secondP: d, total: a + d });
    case '12':
      return t('ai.pick.sum.double', { first: home, firstP: h, second: away, secondP: a, total: h + a });
    default:
      return t('ai.pick.sum.single', { outcome: pickOutcomeText(t, pick.market, home, away), total: pick.probability ?? 0 });
  }
}

/**
 * "Recommended bet": the model's preferred outcome. Without bookmaker odds its value
 * cannot be judged, so it is labelled as the model's preference, never as profitable.
 */
export function PickCard({ pick, event, values, basis }: { pick: AiPick; event: SportEvent; values: readonly number[]; basis: Basis }) {
  const { t, settings } = useSettings();
  const home = event.home.name;
  const away = event.away.name;
  if (pick.market === 'NO_BET') {
    return (
      <section className={`${s.card} ${s.pick}`} data-testid="ai-pick" data-market="NO_BET">
        <h3 className={s.cardTitle}>{t('ai.pick.title')}</h3>
        <p className={s.pickKicker}>{t('ai.pick.noBetTitle')}</p>
        <p className={s.body}>{t('ai.pick.noBetText')}</p>
        <p className={s.hint}>{t('ai.pick.caveat')}</p>
      </section>
    );
  }
  return (
    <section className={`${s.card} ${s.pick}`} data-testid="ai-pick" data-market={pick.market}>
      <h3 className={s.cardTitle}>{t('ai.pick.title')}</h3>
      <p className={s.pickKicker}>
        {t('ai.pick.preferred')} · {t(`ai.pick.market.${pick.market}`)} · {pick.market}
      </p>
      <p className={s.pickOutcome}>{pickOutcomeText(t, pick.market, home, away)}</p>
      <p className={s.pickProbability}>{t('ai.pick.probability', { value: pick.probability ?? 0 })}</p>
      <div className={s.why}>
        <p className={s.whyTitle}>{t('ai.pick.why')}</p>
        <ul className={s.list}>
          <li>{arithmetic(t, pick, values, home, away)}</li>
          {pick.reasons.map((r) => (
            <li key={r}>{pickReasonText(t, settings.lang, r, basis)}</li>
          ))}
        </ul>
      </div>
      <p className={s.hint}>{t('ai.pick.caveat')}</p>
    </section>
  );
}

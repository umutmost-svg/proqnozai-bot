import type { AiPick, ModelForecast, PickReason, SportEvent, TeamBasis } from '../../data/models';
import { FAMILY } from '../../data/markets';
import type { Translate } from '../../i18n';
import { plural } from '../../i18n';
import { useSettings } from '../../state/contexts';
import s from './Ai.module.css';
import { decimal, marketText } from './Model';

type Basis = { home: TeamBasis; away: TeamBasis };
type Lang = Parameters<typeof plural>[1];

/** "Liverpool won't lose" etc. — built from the market and the teams, nothing else. */
export function pickOutcomeText(t: Translate, market: AiPick['market'], home: string, away: string): string {
  return marketText(t, market, home, away);
}

/** A reason sentence from the verified numbers of the side it names (and the model's figures). */
export function pickReasonText(t: Translate, lang: Lang, reason: PickReason, b: Basis, forecast?: ModelForecast): string {
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
    case 'homeStrongerRating':
    case 'awayStrongerRating': {
      const mine = forecast!.ratings[side];
      const theirs = forecast!.ratings[side === 'home' ? 'away' : 'home'];
      return t('ai.pick.reason.strongerRating', {
        team: team.name,
        attack: decimal(lang, mine.attack),
        defence: decimal(lang, mine.defence),
        other: other.name,
        otherAttack: decimal(lang, theirs.attack),
        otherDefence: decimal(lang, theirs.defence),
      });
    }
    case 'homeAdvantage':
      return t('ai.pick.reason.homeAdvantage', { value: decimal(lang, forecast!.homeAdvantage) });
  }
}

/** The arithmetic behind the probability: which outcomes it adds up, or what the line means. */
function arithmetic(t: Translate, pick: AiPick, values: readonly number[], home: string, away: string): string {
  const [h, d, a] = values as [number, number, number];
  switch (pick.market) {
    case '1X':
      return t('ai.pick.sum.double', { first: home, firstP: h, second: t('ai.draw'), secondP: d, total: h + d });
    case 'X2':
      return t('ai.pick.sum.double', { first: away, firstP: a, second: t('ai.draw'), secondP: d, total: a + d });
    case '12':
      return t('ai.pick.sum.double', { first: home, firstP: h, second: away, secondP: a, total: h + a });
    case 'H1-0.5':
    case 'H1-1.5':
    case 'H2+0.5':
    case 'H2+1.5':
      return t('ai.pick.sum.single', { outcome: t(`ai.pick.meaning.${pick.market}`, { home, away }), total: pick.probability ?? 0 });
    default:
      return t('ai.pick.sum.single', { outcome: pickOutcomeText(t, pick.market, home, away), total: pick.probability ?? 0 });
  }
}

/**
 * "Recommended bet": the market the goal model's frozen rules selected. Without bookmaker
 * odds its value cannot be judged, so it is always labelled as a model estimate, never as
 * profitable; when nothing passes the rules it says so (NO_BET is a full result).
 */
export function PickCard({ pick, event, values, basis, forecast }: { pick: AiPick; event: SportEvent; values: readonly number[]; basis: Basis; forecast?: ModelForecast }) {
  const { t, settings } = useSettings();
  const home = event.home.name;
  const away = event.away.name;
  if (pick.market === 'NO_BET') {
    const why = forecast?.noBetReason ? t(`ai.pick.noBet.${forecast.noBetReason}`) : t('ai.pick.noBetText');
    return (
      <section className={`${s.card} ${s.pick}`} data-testid="ai-pick" data-market="NO_BET" data-reason={forecast?.noBetReason ?? undefined}>
        <h3 className={s.cardTitle}>{t('ai.pick.title')}</h3>
        <p className={s.pickKicker}>{t('ai.pick.noBetTitle')}</p>
        <p className={s.body}>{why}</p>
        <p className={s.hint} data-testid="ai-pick-caveat">{t('ai.pick.caveat')}</p>
      </section>
    );
  }
  return (
    <section className={`${s.card} ${s.pick}`} data-testid="ai-pick" data-market={pick.market}>
      <h3 className={s.cardTitle}>{t('ai.pick.title')}</h3>
      {forecast?.experimental && (
        <span className={s.limited} data-testid="ai-pick-experimental">
          {t('ai.pick.experimentalBadge')}
        </span>
      )}
      <p className={s.pickKicker}>
        {t('ai.pick.preferred')} · {t(`ai.family.${FAMILY[pick.market]}`)}
      </p>
      <p className={s.pickOutcome}>{pickOutcomeText(t, pick.market, home, away)}</p>
      <p className={s.pickProbability}>{t('ai.pick.probability', { value: pick.probability ?? 0 })}</p>
      <div className={s.why}>
        <p className={s.whyTitle}>{t('ai.pick.why')}</p>
        <ul className={s.list}>
          <li>{arithmetic(t, pick, values, home, away)}</li>
          {pick.reasons.map((r) => (
            <li key={r}>{pickReasonText(t, settings.lang, r, basis, forecast)}</li>
          ))}
        </ul>
      </div>
      {forecast && <p className={s.hint}>{t('ai.pick.selectionNote')}</p>}
      {forecast?.experimental && <p className={s.hint}>{t('ai.pick.experimentalNote')}</p>}
      <p className={s.hint} data-testid="ai-pick-caveat">{t('ai.pick.caveat')}</p>
    </section>
  );
}

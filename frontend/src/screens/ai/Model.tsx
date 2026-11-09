import type { MarketChance, MarketKey, ModelForecast, SportEvent } from '../../data/models';
import { DISPLAY_ROWS, FAMILY } from '../../data/markets';
import { LOCALES, type Lang, type Translate } from '../../i18n';
import { formatDateTime } from '../../i18n/format';
import { useSettings } from '../../state/contexts';
import s from './Ai.module.css';

/** A model figure such as 1.25 in the interface language's number format. */
export function decimal(lang: Lang, value: number): string {
  return new Intl.NumberFormat(LOCALES[lang], { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

/** The selection as words: "Arsenal handicap (−1.5)", "Over 2.5 goals"… — teams and line only. */
export function marketText(t: Translate, market: MarketKey | 'NO_BET', home: string, away: string): string {
  return t(`ai.pick.outcome.${market}`, { home, away });
}

/** A cell label: team totals drop the team name (shown above them), everything else as in marketText. */
function cellText(t: Translate, key: MarketKey, home: string, away: string): string {
  const team = /^[HA]([OU])(\d\.5)$/.exec(key);
  if (team) return t(team[1] === 'O' ? 'ai.markets.over' : 'ai.markets.under', { line: team[2]! });
  return marketText(t, key, home, away);
}

/** Up to three other markets that passed the same frozen selection rules. */
export function Alternatives({ items, event, experimental }: { items: readonly MarketChance[]; event: SportEvent; experimental: boolean }) {
  const { t } = useSettings();
  if (!items.length) return null;
  return (
    <section className={s.card} data-testid="ai-alternatives">
      <h3 className={s.cardTitle}>{t('ai.alt.title')}</h3>
      <ul className={s.marketRows}>
        {items.map((a) => (
          <li key={a.market} className={s.marketRow} data-market={a.market}>
            <span className={s.marketName}>
              {marketText(t, a.market, event.home.name, event.away.name)}
              <span className={s.marketFamily}>{t(`ai.family.${FAMILY[a.market]}`)}</span>
            </span>
            <span className={s.marketValue}>{a.probability}%</span>
          </li>
        ))}
      </ul>
      <p className={s.hint}>{t('ai.alt.note')}</p>
      {experimental && <p className={s.hint}>{t('ai.pick.experimentalBadge')}</p>}
    </section>
  );
}

/** Every market of the score distribution, grouped by family; unproven families are marked as reference only. */
export function MarketsBlock({ forecast, event }: { forecast: ModelForecast; event: SportEvent }) {
  const { t } = useSettings();
  const m = forecast.markets;
  const families = Object.keys(DISPLAY_ROWS) as (keyof typeof DISPLAY_ROWS)[];
  return (
    <section className={s.card} data-testid="ai-markets">
      <h3 className={s.cardTitle}>{t('ai.markets.title')}</h3>
      {families.map((family) => {
        const unproven = forecast.unprovenFamilies.includes(family);
        return (
          <div key={family} className={s.marketGroup} data-family={family} data-unproven={unproven || undefined}>
            <h4 className={s.marketGroupTitle}>{t(`ai.family.${family}`)}</h4>
            {DISPLAY_ROWS[family].map((row, i) => (
              <div key={row.join()}>
                {/* Team totals: the team once above its two lines, short labels in the cells. */}
                {family === 'team_total' && i % 2 === 0 && (
                  <p className={s.marketTeam}>{row[0]!.startsWith('H') ? event.home.name : event.away.name}</p>
                )}
                <div className={s.marketPair}>
                  {row.map((key) => (
                    <div key={key} className={s.marketCell} data-market={key}>
                      <span className={s.marketCellName}>{cellText(t, key, event.home.name, event.away.name)}</span>
                      <span className={s.marketCellValue}>{m[key]}%</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {unproven && <p className={s.unproven}>{t('ai.markets.unproven')}</p>}
          </div>
        );
      })}
      {forecast.topScores.length > 0 && (
        <div className={s.marketGroup} data-testid="ai-scores">
          <h4 className={s.marketGroupTitle}>{t('ai.markets.scores')}</h4>
          <div className={s.scores}>
            {forecast.topScores.map((x) => (
              <span key={`${x.home}:${x.away}`} className={s.score}>
                <strong>{`${x.home}:${x.away}`}</strong> {x.probability}%
              </span>
            ))}
          </div>
          <p className={s.hint}>{t('ai.markets.scoresNote')}</p>
        </div>
      )}
      <p className={s.hint}>{t('ai.markets.note')}</p>
    </section>
  );
}

/** The model's own figures for both teams (expected goals, ratings, the competition's level). */
export function ModelStats({ forecast, event }: { forecast: ModelForecast; event: SportEvent }) {
  const { t, settings } = useSettings();
  const lang = settings.lang;
  const sides = [
    { name: event.home.name, xg: forecast.expectedGoals.home, rating: forecast.ratings.home },
    { name: event.away.name, xg: forecast.expectedGoals.away, rating: forecast.ratings.away },
  ];
  return (
    <div className={s.modelStats} data-testid="ai-model-stats">
      <p className={s.whyTitle}>{t('ai.stats.expected')}</p>
      <div className={s.xg}>
        {sides.map((side) => (
          <div key={side.name} className={s.xgSide}>
            <span className={s.xgValue}>{decimal(lang, side.xg)}</span>
            <span className={s.xgName}>{side.name}</span>
            <span className={s.hint}>
              {t('ai.stats.rating', { attack: decimal(lang, side.rating.attack), defence: decimal(lang, side.rating.defence) })}
            </span>
          </div>
        ))}
      </div>
      <p className={s.hint}>{t('ai.stats.ratingNote')}</p>
      <p className={s.hint}>
        {t('ai.stats.league', { home: decimal(lang, forecast.leagueGoals.home), away: decimal(lang, forecast.leagueGoals.away) })}
      </p>
    </div>
  );
}

/** Which model, how much data and how fresh: shown with the provider's attribution. */
export function ModelSource({ forecast }: { forecast: ModelForecast }) {
  const { t, timeZone } = useSettings();
  return (
    <>
      <p className={s.source} data-testid="ai-model-source">
        {t('ai.sources.model', {
          version: forecast.version,
          kind: t(`ai.sources.kind.${forecast.kind}`),
          sample: forecast.sample,
          date: formatDateTime(new Date(forecast.dataUntil), timeZone),
        })}
      </p>
      <p className={s.hint}>{t('ai.sources.validated')}</p>
    </>
  );
}

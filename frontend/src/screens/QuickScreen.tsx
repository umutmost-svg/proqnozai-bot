import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AppShell } from '../components/AppShell';
import { Emblem } from '../components/Emblem';
import { Header } from '../components/Header';
import { DataFootnote } from '../components/SourceNote';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { ALL_TOURNAMENTS } from '../data/filter';
import { quickPicks } from '../data/tournaments';
import { DataError } from '../data/provider';
import type { PriceDeadline, QuickAnswer, QuickCriteria, QuickLeg, QuickPreview, QuickReason } from '../data/models';
import { QUICK_REASONS } from '../data/models';
import {
  COUPON_SIZES,
  QUICK_MARKETS,
  QUICK_MODES,
  QUICK_PERIODS,
  QUICK_RISKS,
  loadCriteria,
  saveCriteria,
} from '../data/quick';
import type { Translate } from '../i18n';
import { formatDateTime, formatDayMonth } from '../i18n/format';
import { useAsync } from '../hooks/useAsync';
import { useFilters, useServices, useSettings } from '../state/contexts';
import { dayKeyOf, timeOf } from '../time/zoned';
import { PriceLine, pricesExpired, usePricesLive } from './ai/Market';
import { decimal, marketText } from './ai/Model';
import s from './QuickScreen.module.css';

type Run = {
  criteria: QuickCriteria;
  state:
    | { status: 'loading' }
    /**
     * skewMs: server "computed at" minus device time when the request was sent (≥ 0).
     * deadline: the first kickoff on the monotonic clock (immune to device-clock changes).
     * keepUntil: how long a result without legs may be shown again on return (monotonic).
     */
    | { status: 'success'; answer: QuickAnswer; skewMs: number; deadline: number; keepUntil: number }
    | { status: 'error'; reason: string | undefined };
};

/** Monotonic milliseconds (performance.now): never moves back when the device clock is changed. */
export const monotonic = { now: () => performance.now() };

function nextUtcMidnight(iso: string): number {
  const d = new Date(iso);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
}

function successState(answer: QuickAnswer, sentAt: number, receivedAt: number) {
  const skewMs = Math.max(0, Date.parse(answer.computedAt) - sentAt);
  const first = answer.legs.length ? Math.min(...answer.legs.map((l) => Date.parse(l.event.startsAt))) : Infinity;
  // Time to kickoff as estimated now (server-adjusted), fixed on the monotonic clock.
  const deadline = monotonic.now() + (first - (receivedAt + skewMs));
  // A preview lists matches of later UTC days: at the next UTC midnight (server time) the first of them may
  // become a match-day one, so it goes by then at the latest — counted from the server time at receipt
  // (delivery included); already past it, the preview is not shown at all.
  const left = nextUtcMidnight(answer.computedAt) - (receivedAt + skewMs);
  if (answer.preview.length && left <= 0) answer = { ...answer, preview: [] };
  const keep = answer.preview.length ? Math.min(EMPTY_RESULT_KEEP_MS, left) : EMPTY_RESULT_KEEP_MS;
  return { status: 'success' as const, answer, skewMs, deadline, keepUntil: monotonic.now() + keep };
}

/** "No matches" / "no pick" answers are about the moment they were computed: kept briefly only. */
const EMPTY_RESULT_KEEP_MS = 10 * 60_000;

/** Why a shown answer must go: "kickoff" (a leg started), "old" (an empty answer aged out), or null. */
function staleness(state: Extract<Run['state'], { status: 'success' }>, deviceNow: number): 'kickoff' | 'old' | null {
  const { answer, skewMs, deadline, keepUntil } = state;
  if (answer.legs.length === 0) {
    if (monotonic.now() >= keepUntil) return 'old';
    // A preview is about later UTC days: past the next UTC midnight (server-adjusted wall clock — the
    // monotonic one may stop while the device sleeps) it is yesterday's and goes.
    return answer.preview.length && deviceNow + skewMs >= nextUtcMidnight(answer.computedAt) ? 'old' : null;
  }
  const first = Math.min(...answer.legs.map((l) => Date.parse(l.event.startsAt)));
  // Either clock saying "kicked off" is enough: the monotonic deadline survives a device clock
  // set back; the server-adjusted device time covers a monotonic clock that slept.
  return monotonic.now() >= deadline || deviceNow + skewMs >= first ? 'kickoff' : null;
}

/** When the answer must go at the latest, on the monotonic clock. */
function monotonicLimit(state: Extract<Run['state'], { status: 'success' }>): number {
  return state.answer.legs.length === 0 ? state.keepUntil : state.deadline;
}

/**
 * What a returning screen may show of the last run, decided BEFORE the first render: never a
 * spinner nothing will finish, never a pick past kickoff, never an old "nothing found".
 */
function restore(run: Run | null, deviceNow: number): { run: Run | null; expired: boolean } {
  if (!run || run.state.status === 'loading') return { run: null, expired: false };
  if (run.state.status !== 'success') return { run, expired: false };
  const why = staleness(run.state, deviceNow);
  if (why !== null) return { run: null, expired: why === 'kickoff' };
  const answer = withoutExpiredPrices(run.state.answer);
  return { run: answer === run.state.answer ? run : { ...run, state: { ...run.state, answer } }, expired: false };
}

/** The last pick survives a trip to a match and back (until the app is reloaded). */
let lastRun: Run | null = null;
export function resetQuickMemory(): void {
  lastRun = null;
}

function Choice<T extends string | number>({
  label,
  options,
  value,
  text,
  onChange,
  testId,
}: {
  label: string;
  options: readonly T[];
  value: T;
  text: (option: T) => string;
  onChange: (option: T) => void;
  testId: string;
}) {
  return (
    <fieldset className={s.group} data-testid={testId}>
      <legend className={s.legend}>{label}</legend>
      <div className={s.chips}>
        {options.map((o) => (
          <button
            key={String(o)}
            type="button"
            className={`${s.chip} ${o === value ? s.chipOn : ''}`}
            aria-pressed={o === value}
            data-value={o}
            onClick={() => onChange(o)}
          >
            {text(o)}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

function Criteria({ criteria, onChange }: { criteria: QuickCriteria; onChange: (c: QuickCriteria) => void }) {
  const { t } = useSettings();
  const { data } = useServices();
  // Asked on its own: the tab may be the first screen opened, before any listing.
  const loaded = useAsync(() => data.loadTournaments('football'), 'quick:tournaments');
  const tournaments = loaded.status === 'success' && loaded.data.length ? loaded.data : data.listTournaments('football');
  const set = (patch: Partial<QuickCriteria>) => onChange({ ...criteria, ...patch });
  const [showAll, setShowAll] = useState(false);
  const picks = quickPicks(tournaments, criteria.tournaments);
  const chips = showAll ? tournaments : picks;
  // A remembered competition the catalogue no longer has is dropped (and the cleaned choice
  // saved) once the catalogue is known: an invisible chip must never keep filtering picks.
  const catalogue = loaded.status === 'success' ? loaded.data : null;
  useEffect(() => {
    if (!catalogue?.length) return;
    const known = new Set(catalogue.map((x) => x.id));
    const kept = criteria.tournaments.filter((id) => known.has(id));
    if (kept.length !== criteria.tournaments.length) onChange({ ...criteria, tournaments: kept });
  }, [catalogue, criteria, onChange]);
  const toggle = (id: string) => {
    const on = criteria.tournaments.includes(id);
    const next = on ? criteria.tournaments.filter((x) => x !== id) : [...criteria.tournaments, id];
    // Every tournament ticked is the same as "all".
    set({ tournaments: next.length === tournaments.length ? [] : next });
  };
  return (
    <div className={s.criteria} data-testid="quick-criteria">
      <Choice
        label={t('quick.mode.label')}
        options={QUICK_MODES}
        value={criteria.mode}
        text={(m) => t(`quick.mode.${m}`)}
        onChange={(mode) => set({ mode })}
        testId="quick-mode"
      />
      <p className={s.hint}>{t(criteria.mode === 'single' ? 'quick.mode.singleHint' : 'quick.mode.couponHint')}</p>
      {criteria.mode === 'coupon' && (
        <Choice label={t('quick.size')} options={COUPON_SIZES} value={criteria.size as (typeof COUPON_SIZES)[number]} text={String} onChange={(size) => set({ size })} testId="quick-size" />
      )}
      <Choice
        label={t('quick.period')}
        options={QUICK_PERIODS}
        value={criteria.period}
        text={(p) => t(`quick.period.${p}`)}
        onChange={(period) => set({ period })}
        testId="quick-period"
      />
      <p className={s.hint} data-testid="quick-match-day-note">
        {t('quick.matchDayNote')}
      </p>
      <fieldset className={s.group} data-testid="quick-tournaments">
        <legend className={s.legend}>{t('quick.tournaments')}</legend>
        <div className={s.chips}>
          <button
            type="button"
            className={`${s.chip} ${criteria.tournaments.length === 0 ? s.chipOn : ''}`}
            aria-pressed={criteria.tournaments.length === 0}
            data-value={ALL_TOURNAMENTS}
            onClick={() => set({ tournaments: [] })}
          >
            {t('quick.tournaments.all')}
          </button>
          {loaded.status === 'error' && tournaments.length === 0 && (
            <span className={s.inlineError} data-testid="quick-tournaments-error">
              {t('quick.tournamentsError')}{' '}
              <button type="button" className={s.link} onClick={loaded.retry}>
                {t('common.retry')}
              </button>
            </span>
          )}
          {chips.map((x) => (
            <button
              key={x.id}
              type="button"
              className={`${s.chip} ${criteria.tournaments.includes(x.id) ? s.chipOn : ''}`}
              aria-pressed={criteria.tournaments.includes(x.id)}
              data-value={x.id}
              onClick={() => toggle(x.id)}
            >
              {x.name}
            </button>
          ))}
          {picks.length < tournaments.length && (
            <button type="button" className={s.link} aria-expanded={showAll} onClick={() => setShowAll((v) => !v)} data-testid="quick-tournaments-more">
              {showAll ? t('quick.tournaments.less') : t('quick.tournaments.more', { count: tournaments.length })}
            </button>
          )}
        </div>
      </fieldset>
      <Choice
        label={t('quick.market')}
        options={QUICK_MARKETS}
        value={criteria.market}
        text={(m) => (m === 'any' ? t('quick.market.any') : t(`ai.family.${m}`))}
        onChange={(market) => set({ market })}
        testId="quick-market"
      />
      {(criteria.market === 'total' || criteria.market === 'btts') && <p className={s.hint}>{t('quick.marketNote')}</p>}
      <Choice
        label={t('quick.risk')}
        options={QUICK_RISKS}
        value={criteria.risk}
        text={(r) => t(`quick.risk.${r}`)}
        onChange={(risk) => set({ risk })}
        testId="quick-risk"
      />
      <p className={s.hint}>{t(`quick.risk.${criteria.risk}Hint`)}</p>
    </div>
  );
}

function Leg({ leg, index, priced }: { leg: QuickLeg; index: number | null; priced: boolean }) {
  const { t, timeZone, settings } = useSettings();
  const { data } = useServices();
  const navigate = useNavigate();
  const { event } = leg;
  const start = new Date(event.startsAt);
  return (
    <article className={s.leg} data-testid="quick-leg" data-event={event.id} data-market={leg.market}>
      <button type="button" className={s.legMatch} onClick={() => navigate(`/event/${event.id}`)}>
        {index !== null && <span className={s.legIndex}>{index}</span>}
        <span className={s.legTeams}>
          <span className={s.legTeam}>
            <Emblem competitor={event.home} size={28} crestBox={22} variant="plain" />
            {event.home.name}
          </span>
          <span className={s.legTeam}>
            <Emblem competitor={event.away} size={28} crestBox={22} variant="plain" />
            {event.away.name}
          </span>
        </span>
        <span className={s.legMeta}>
          {data.getTournament(event.tournamentId)?.name}
          <br />
          {`${formatDayMonth(t, dayKeyOf(start, timeZone))} · ${timeOf(start, timeZone)}`}
        </span>
      </button>
      <p className={s.legKicker}>{t(`ai.family.${leg.family}`)}</p>
      <p className={s.legOutcome}>{marketText(t, leg.market, event.home.name, event.away.name)}</p>
      <p className={s.legProbability}>{t('quick.leg.probability', { value: leg.probability })}</p>
      {priced && leg.price && <PriceLine quote={leg.price} />}
      <ul className={s.why}>
        <li>
          {t('quick.leg.why', {
            rate: leg.leagueRate,
            home: decimal(settings.lang, leg.expectedGoals.home),
            away: decimal(settings.lang, leg.expectedGoals.away),
          })}
        </li>
      </ul>
    </article>
  );
}

function reasonLines(t: Translate, answer: QuickAnswer): string[] {
  return QUICK_REASONS.filter((r: QuickReason) => (answer.reasons[r] ?? 0) > 0).map((r) =>
    t(`quick.reason.${r}`, { n: answer.reasons[r] ?? 0 }),
  );
}

function Counts({ answer }: { answer: QuickAnswer }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const lines = reasonLines(t, answer);
  const names = answer.unavailableTournaments.map((id) => data.getTournament(id)?.name ?? id);
  return (
    <section className={s.card} data-testid="quick-counts">
      <p className={s.body}>{t('quick.counts', { matches: answer.counts.matches, suitable: answer.suitable })}</p>
      {lines.length > 0 && (
        <>
          <p className={s.legend}>{t('quick.reasonsTitle')}</p>
          <ul className={s.why}>
            {lines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </>
      )}
      {names.length > 0 && (
        <p className={s.warn} data-testid="quick-unavailable">
          {t('quick.unavailable', { names: names.join(', ') })}
        </p>
      )}
      <p className={s.meta}>{t('quick.computedAt', { date: formatDateTime(new Date(answer.computedAt), timeZone) })}</p>
      {answer.uncheckedDays > 0 && (
        <p className={s.warn} data-testid="quick-unchecked">
          {t('quick.unchecked', { n: answer.uncheckedDays })}
        </p>
      )}
    </section>
  );
}

/**
 * Where to go from an empty answer: when the next match of the period can be picked, and the
 * user's own ways to look elsewhere (a longer period, the match calendar). Nothing is loosened.
 */
function NextSteps({ answer, period, onWeek }: { answer: QuickAnswer; period: QuickCriteria['period']; onWeek: () => void }) {
  const { t, timeZone } = useSettings();
  const navigate = useNavigate();
  const next = answer.nextMatchAt ? new Date(answer.nextMatchAt) : null;
  return (
    <div className={s.nextSteps} data-testid="quick-next-steps">
      {next && (
        <p className={s.body} data-testid="quick-next-match">
          {t('quick.nextMatch', {
            date: `${formatDayMonth(t, dayKeyOf(next, timeZone))} · ${timeOf(next, timeZone)}`,
            // The rule's day is the UTC one: said explicitly, it may differ from the local date.
            utcDay: formatDayMonth(t, dayKeyOf(next, 'UTC')),
          })}
        </p>
      )}
      <div className={s.actions}>
        {period !== '7d' && (
          <button type="button" className={s.secondary} onClick={onWeek} data-testid="quick-try-week">
            {t('quick.tryWeek')}
          </button>
        )}
        <button type="button" className={s.secondary} onClick={() => navigate('/matches')} data-testid="quick-open-calendar">
          {t('quick.openCalendar')}
        </button>
      </div>
    </div>
  );
}

/**
 * Without a pick: the nearest later matches of the validated leagues with the model's 1/X/2. Not a
 * recommendation — it says on which (UTC) day one may appear, if the outcome passes the rules then.
 */
function Preview({ items }: { items: readonly QuickPreview[] }) {
  const { t, timeZone } = useSettings();
  const { data } = useServices();
  const navigate = useNavigate();
  return (
    <section className={s.card} data-testid="quick-preview">
      <h2 className={s.resultTitle}>{t('quick.preview.title')}</h2>
      <p className={s.hint}>{t('quick.preview.note')}</p>
      {items.map(({ event, probabilities: p, recommendationDay }) => {
        const start = new Date(event.startsAt);
        return (
          <article key={event.id} className={s.leg} data-testid="quick-preview-item" data-event={event.id}>
            <button type="button" className={s.legMatch} onClick={() => navigate(`/event/${event.id}`)}>
              <span className={s.legTeams}>
                <span className={s.legTeam}>
                  <Emblem competitor={event.home} size={28} crestBox={22} variant="plain" />
                  {event.home.name}
                </span>
                <span className={s.legTeam}>
                  <Emblem competitor={event.away} size={28} crestBox={22} variant="plain" />
                  {event.away.name}
                </span>
              </span>
              <span className={s.legMeta}>
                {data.getTournament(event.tournamentId)?.name}
                <br />
                {`${formatDayMonth(t, dayKeyOf(start, timeZone))} · ${timeOf(start, timeZone)}`}
              </span>
            </button>
            <p className={s.legProbability}>{t('quick.preview.odds', { home: p.home, draw: p.draw, away: p.away })}</p>
            <p className={s.hint}>{t('quick.preview.day', { date: formatDayMonth(t, recommendationDay) })}</p>
          </article>
        );
      })}
    </section>
  );
}

/** The earliest of the answer's price deadlines: every price goes at once (a coupon is one view). */
function firstDeadline(answer: QuickAnswer): PriceDeadline | undefined {
  const all = [...answer.legs.map((l) => l.price?.expires), answer.couponOdds?.expires].filter((d): d is PriceDeadline => !!d);
  return all.sort((a, b) => a.mono - b.mono)[0];
}

/** An answer kept from an earlier visit loses prices that expired meanwhile (before it is shown again). */
function withoutExpiredPrices(answer: QuickAnswer): QuickAnswer {
  const deadline = firstDeadline(answer);
  if (!deadline || !pricesExpired(deadline)) return answer;
  return { ...answer, couponOdds: null, legs: answer.legs.map((l) => ({ ...l, price: null })) };
}

function Result({ answer, period, onWeek }: { answer: QuickAnswer; period: QuickCriteria['period']; onWeek: () => void }) {
  const { t, timeZone, settings } = useSettings();
  const priced = usePricesLive(firstDeadline(answer));
  if (answer.status !== 'ok') {
    const empty = answer.counts.matches === 0;
    return (
      <>
        {answer.status === 'unavailable' ? (
          <EmptyState title={t('quick.empty.unavailableTitle')} text={t('quick.empty.unavailableText')} icon="error" testId="quick-unavailable-state" />
        ) : empty ? (
          <EmptyState title={t('quick.empty.noMatchesTitle')} text={t('quick.empty.noMatchesText')} testId="quick-empty" />
        ) : answer.status === 'not_enough' ? (
          <EmptyState
            title={t('quick.empty.notEnoughTitle')}
            text={t('quick.empty.notEnoughText', { size: answer.size, found: answer.suitable })}
            testId="quick-not-enough"
          />
        ) : (
          <EmptyState title={t('quick.empty.noBetTitle')} text={t('quick.empty.noBetText')} testId="quick-no-bet" />
        )}
        {answer.status !== 'unavailable' && <NextSteps answer={answer} period={period} onWeek={onWeek} />}
        {answer.preview.length > 0 && <Preview items={answer.preview} />}
        {!empty || answer.status === 'unavailable' || answer.uncheckedDays > 0 ? (
          <Counts answer={answer} />
        ) : (
          <p className={s.meta}>{t('quick.computedAt', { date: formatDateTime(new Date(answer.computedAt), timeZone) })}</p>
        )}
      </>
    );
  }
  const coupon = answer.mode === 'coupon';
  // When the shown prices were taken: the oldest of them.
  const pricedAt = answer.legs
    .map((l) => l.price?.fetchedAt)
    .filter((x): x is string => !!x)
    .sort((a, b) => Date.parse(a) - Date.parse(b))[0];
  return (
    <>
      <section className={`${s.card} ${s.result}`} data-testid="quick-result" data-mode={answer.mode}>
        <div className={s.resultHead}>
          <h2 className={s.resultTitle}>{coupon ? t('quick.result.coupon', { n: answer.legs.length }) : t('quick.result.single')}</h2>
        </div>
        {answer.legs.map((leg, i) => (
          <Leg key={leg.event.id} leg={leg} index={coupon ? i + 1 : null} priced={priced} />
        ))}
        {coupon && answer.combined !== null && (
          <div className={s.combined} data-testid="quick-combined">
            <p className={s.combinedValue}>
              {answer.combined[0] === 0
                ? t('quick.combinedUpTo', { n: answer.legs.length, high: answer.combined[1] })
                : t('quick.combined', { n: answer.legs.length, low: answer.combined[0], high: answer.combined[1] })}
            </p>
            <p className={s.hint}>{t('quick.combinedNote')}</p>
          </div>
        )}
        {coupon && priced && answer.couponOdds && (
          <div className={s.combined} data-testid="quick-coupon-odds">
            <p className={s.combinedValue}>{t('quick.couponOdds', { odds: decimal(settings.lang, answer.couponOdds.odds) })}</p>
            <p className={s.hint}>{t('quick.couponOddsNote')}</p>
          </div>
        )}
        <p className={s.meta}>
          {[
            t('quick.computedAt', { date: formatDateTime(new Date(answer.computedAt), timeZone) }),
            priced && pricedAt ? t('ai.value.asOf', { time: formatDateTime(new Date(pricedAt), timeZone) }) : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        </p>
      </section>
      <Counts answer={answer} />
    </>
  );
}

/** Too many picks or the source being down are "try later", not a connection problem. */
function PickError({ reason, onRetry }: { reason: string | undefined; onRetry: () => void }) {
  const { t } = useSettings();
  if (reason === 'unauthenticated') return <ErrorState title={t('quick.errorTitle')} text={t('auth.sessionExpired')} onRetry={onRetry} />;
  if (reason === 'bad_range') return <ErrorState title={t('quick.clockTitle')} text={t('quick.clockText')} onRetry={onRetry} />;
  if (reason === 'limit_reached') return <ErrorState title={t('quick.busyTitle')} text={t('quick.busyText')} onRetry={onRetry} />;
  if (reason && reason !== 'network' && reason !== 'bad_response' && reason !== 'bad_range') {
    return <ErrorState title={t('quick.sourceTitle')} text={t('quick.sourceText')} onRetry={onRetry} />;
  }
  return <ErrorState title={t('quick.errorTitle')} text={t('quick.errorText')} onRetry={onRetry} />;
}

export function QuickScreen() {
  const { data, clock } = useServices();
  const { t, timeZone } = useSettings();
  const f = useFilters();
  const known = new Set(data.listTournaments('football').map((x) => x.id));
  const [criteria, setCriteria] = useState<QuickCriteria>(() => lastRun?.criteria ?? loadCriteria(known));
  // Decided once, before the first render (see restore): nothing stale is ever painted.
  const [initial] = useState(() => restore(lastRun, clock.now().getTime()));
  const [run, setRun] = useState<Run | null>(initial.run);
  const sequence = useRef(0);

  const [expired, setExpired] = useState(initial.expired);

  useEffect(() => {
    lastRun = run;
  }, [run]);

  // A shown answer goes when it turns stale (a leg kicked off, or an empty answer aged out): at
  // that moment on a monotonic timer, and whenever the app returns to the foreground.
  useEffect(() => {
    if (run?.state.status !== 'success') return;
    const state = run.state;
    const drop = (why: 'kickoff' | 'old') => {
      sequence.current++;
      setRun(null);
      setExpired(why === 'kickoff');
    };
    const check = () => {
      const why = staleness(state, clock.now().getTime());
      if (why) drop(why);
    };
    // When the monotonic timer fires the limit has passed, whatever the device clock says.
    const wait = Math.min(monotonicLimit(state) - monotonic.now() + 500, 2_147_000_000);
    const timer = setTimeout(() => drop(state.answer.legs.length ? 'kickoff' : 'old'), Math.max(0, wait));
    const onVisible = () => document.visibilityState === 'visible' && check();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [run, clock]);

  const change = useCallback((next: QuickCriteria) => {
    setCriteria(next);
    saveCriteria(next);
  }, []);

  const pick = (chosen: QuickCriteria) => {
    const id = ++sequence.current;
    const sentAt = clock.now().getTime();
    setExpired(false);
    setRun({ criteria: chosen, state: { status: 'loading' } });
    data.quickPick(chosen, clock.now(), timeZone).then(
      (answer) => {
        if (id !== sequence.current) return;
        // A slow device clock must not keep a pick on screen past kickoff: the server's
        // "computed at" moves our notion of now forward (never backward). Measured against
        // the moment the request was SENT, so time spent delivering the answer counts as
        // already elapsed: the estimate errs towards expiring early, never late.
        const state = successState(answer, sentAt, clock.now().getTime());
        // Checked before it is ever rendered: an answer already stale is never painted.
        if (staleness(state, clock.now().getTime()) === 'kickoff') {
          setRun(null);
          setExpired(true);
          return;
        }
        setRun({ criteria: chosen, state });
      },
      (error: unknown) =>
        id === sequence.current &&
        setRun({ criteria: chosen, state: { status: 'error', reason: error instanceof DataError ? error.reason : undefined } }),
    );
  };

  const edit = () => {
    sequence.current++;
    setRun(null);
  };

  return (
    <AppShell scrollKey="/quick">
      <Header variant="screen" title={t('quick.title')} backTo="/" />
      {!data.quickAvailable() ? (
        <EmptyState title={t('quick.demoTitle')} text={t('quick.demoText')} testId="quick-demo" />
      ) : run === null ? (
        <>
          <p className={s.lead}>{t('quick.subtitle')}</p>
          {expired && (
            <p className={s.warn} data-testid="quick-expired">
              {t('quick.expired')}
            </p>
          )}
          <Criteria criteria={criteria} onChange={change} />
          <button type="button" className={s.primary} onClick={() => pick(criteria)} data-testid="quick-submit">
            {t('quick.submit')}
          </button>
        </>
      ) : (
        <>
          {run.state.status === 'loading' && <LoadingState cards={run.criteria.mode === 'coupon' ? 2 : 1} text={t('quick.loading')} />}
          {run.state.status === 'error' && <PickError reason={run.state.reason} onRetry={() => pick(run.criteria)} />}
          {run.state.status === 'success' && (
            <Result
              answer={run.state.answer}
              period={run.criteria.period}
              onWeek={() => {
                // The user's explicit choice of a longer period (saved like any other), never automatic.
                const week = { ...run.criteria, period: '7d' as const };
                change(week);
                pick(week);
              }}
            />
          )}
          {run.state.status !== 'loading' && (
            <div className={s.actions}>
              <button type="button" className={s.secondary} onClick={edit} data-testid="quick-change">
                {t('quick.change')}
              </button>
              <button type="button" className={s.primary} onClick={() => pick(run.criteria)} data-testid="quick-repeat">
                {t('quick.repeat')}
              </button>
            </div>
          )}
        </>
      )}
      <DataFootnote sport="football" today={f.today} />
    </AppShell>
  );
}

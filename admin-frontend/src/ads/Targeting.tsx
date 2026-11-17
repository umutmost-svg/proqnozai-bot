import { useState } from 'react';
import type { Condition, Group } from './adsApi';
import { LANGS, SEGMENTS, parseIds, parseList } from './adsApi';

/**
 * Audience builder (ads-architecture.md §4): a flat list of conditions joined by "all" or "any", each one
 * optionally negated. Covers GEO, language, activity segments, groups, specific users and MB holders. A tree
 * the builder cannot show (nesting) is edited as JSON; the server validates both the same way.
 */
export type RuleKind = 'geo_in' | 'geo_unknown' | 'lang_in' | 'user_in' | 'group_in' | 'mb_verified' | 'segment';
export interface Rule {
  kind: RuleKind;
  not: boolean;
  text: string; // geo_in / lang_in / user_in / group_in: the list as typed
  segment: string;
  params: Record<string, number>;
}

export const RULE_LABELS: Record<RuleKind, string> = {
  geo_in: 'Страна (GEO)', geo_unknown: 'Страна неизвестна', lang_in: 'Язык', user_in: 'Конкретные пользователи (Telegram ID)',
  group_in: 'Группы', mb_verified: 'Есть ID MB', segment: 'Активность',
};

export function newRule(kind: RuleKind): Rule {
  const segment = 'active';
  return { kind, not: false, text: '', segment, params: defaults(segment) };
}

function defaults(segment: string): Record<string, number> {
  return Object.fromEntries(Object.entries(SEGMENTS[segment]!.params).map(([k, v]) => [k, v[1]]));
}

/** Rules -> condition tree (null = everyone). Throws a message for an incomplete rule. */
export function toCondition(rules: Rule[], join: 'all' | 'any'): Condition {
  const leaves = rules.map((r) => {
    const leaf = leafOf(r);
    return r.not ? { not: leaf } : leaf;
  });
  if (leaves.length === 0) return null;
  return leaves.length === 1 ? leaves[0]! : { [join]: leaves };
}

function leafOf(r: Rule): Record<string, unknown> {
  switch (r.kind) {
    case 'geo_in': {
      const list = parseList(r.text);
      if (!list.length) throw new Error('Укажите страны');
      return { geo_in: list };
    }
    case 'lang_in': {
      const list = parseList(r.text, false);
      if (!list.length) throw new Error('Укажите языки');
      return { lang_in: list };
    }
    case 'user_in':
    case 'group_in': {
      const ids = parseIds(r.text);
      if (!ids || !ids.length) throw new Error(r.kind === 'user_in' ? 'Telegram ID — числа' : 'Номера групп — числа');
      return { [r.kind]: ids };
    }
    case 'geo_unknown':
    case 'mb_verified':
      return { [r.kind]: true };
    case 'segment':
      return { segment: { name: r.segment, params: r.params } };
  }
}

/** Condition tree -> rules, or null when it is not a flat list the builder can show. */
export function fromCondition(c: Condition): { rules: Rule[]; join: 'all' | 'any' } | null {
  if (c === null) return { rules: [], join: 'all' };
  const [key, value] = Object.entries(c)[0] ?? [];
  if ((key === 'all' || key === 'any') && Array.isArray(value)) {
    const rules = value.map((n) => ruleOf(n as Record<string, unknown>));
    return rules.every((r): r is Rule => r !== null) ? { rules, join: key } : null;
  }
  const one = ruleOf(c);
  return one ? { rules: [one], join: 'all' } : null;
}

function ruleOf(node: Record<string, unknown>, negated = false): Rule | null {
  const entries = Object.entries(node);
  if (entries.length !== 1) return null;
  const [key, value] = entries[0]!;
  if (key === 'not' && !negated && value && typeof value === 'object') return ruleOf(value as Record<string, unknown>, true);
  const base = { ...newRule(key as RuleKind), not: negated };
  switch (key) {
    case 'geo_in':
    case 'lang_in':
    case 'user_in':
    case 'group_in':
      return Array.isArray(value) ? { ...base, text: value.join(', ') } : null; // redacted lists ({count}) are not editable
    case 'geo_unknown':
    case 'mb_verified':
      return base;
    case 'segment': {
      const seg = value as { name?: string; params?: Record<string, number> };
      return seg.name && SEGMENTS[seg.name] ? { ...base, segment: seg.name, params: { ...defaults(seg.name), ...seg.params } } : null;
    }
    default:
      return null; // mb_id_in and anything else: JSON mode
  }
}

export function TargetingBuilder({ value, groups, onChange }: {
  value: Condition; groups: Group[]; onChange: (c: Condition, error: string | null) => void;
}) {
  const initial = fromCondition(value);
  const [mode, setMode] = useState<'builder' | 'json'>(initial ? 'builder' : 'json');
  const [rules, setRules] = useState<Rule[]>(initial?.rules ?? []);
  const [join, setJoin] = useState<'all' | 'any'>(initial?.join ?? 'all');
  const [json, setJson] = useState(JSON.stringify(value, null, 2));

  const emit = (next: Rule[], nextJoin = join) => {
    setRules(next);
    setJoin(nextJoin);
    try {
      onChange(toCondition(next, nextJoin), null);
    } catch (e) {
      onChange(value, (e as Error).message);
    }
  };
  const patch = (i: number, p: Partial<Rule>) => emit(rules.map((r, j) => (j === i ? { ...r, ...p } : r)));

  if (mode === 'json') {
    return (
      <div className="field" data-testid="targeting-json">
        <label htmlFor="ads-json">Условие аудитории (JSON)</label>
        <textarea id="ads-json" className="control" rows={8} value={json} onChange={(e) => {
          setJson(e.target.value);
          try {
            onChange(e.target.value.trim() ? JSON.parse(e.target.value) as Condition : null, null);
          } catch {
            onChange(value, 'JSON с ошибкой');
          }
        }} />
        <div className="small muted">Вложенные условия: {'{"all":[…]}'}, {'{"any":[…]}'}, {'{"not":…}'}; до 4 уровней и 30 условий.</div>
      </div>
    );
  }
  return (
    <div data-testid="targeting-builder">
      <div className="row small" style={{ gap: 8, marginBottom: 8 }}>
        <span>{rules.length === 0 ? 'Все пользователи (без условий).' : 'Показывать, если выполнены'}</span>
        {rules.length > 1 && (
          <select className="control" aria-label="Связка условий" value={join} onChange={(e) => emit(rules, e.target.value as 'all' | 'any')}>
            <option value="all">все условия</option>
            <option value="any">любое условие</option>
          </select>
        )}
        <button type="button" className="btn link" onClick={() => { setJson(JSON.stringify(value, null, 2)); setMode('json'); }}>JSON</button>
      </div>
      {rules.map((r, i) => (
        <div key={i} className="field-row" data-testid="targeting-rule" style={{ alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <select className="control" aria-label="Не" value={r.not ? 'not' : 'is'} onChange={(e) => patch(i, { not: e.target.value === 'not' })}>
            <option value="is">—</option>
            <option value="not">НЕ</option>
          </select>
          <strong className="small">{RULE_LABELS[r.kind]}</strong>
          {(r.kind === 'geo_in' || r.kind === 'user_in') && (
            <input className="control" aria-label={RULE_LABELS[r.kind]} value={r.text}
              placeholder={r.kind === 'geo_in' ? 'AZ, TR, UZ' : '123456789, 987654321'} onChange={(e) => patch(i, { text: e.target.value })} />
          )}
          {r.kind === 'lang_in' && (
            <input className="control" aria-label="Языки" value={r.text} placeholder={LANGS.join(', ')} onChange={(e) => patch(i, { text: e.target.value })} />
          )}
          {r.kind === 'group_in' && (
            <select className="control" aria-label="Группа" multiple value={(parseIds(r.text) ?? []).map(String)}
              onChange={(e) => patch(i, { text: [...e.target.selectedOptions].map((o) => o.value).join(', ') })}>
              {groups.map((g) => <option key={g.id} value={g.id}>{g.name} ({g.members})</option>)}
            </select>
          )}
          {r.kind === 'segment' && (
            <>
              <select className="control" aria-label="Сегмент" value={r.segment} onChange={(e) => patch(i, { segment: e.target.value, params: defaults(e.target.value) })}>
                {Object.entries(SEGMENTS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </select>
              {Object.entries(SEGMENTS[r.segment]!.params).map(([p, [label, , min, max]]) => (
                <label key={p} className="small">
                  {label}{' '}
                  <input className="control" type="number" min={min} max={max} style={{ width: 80 }} aria-label={label}
                    value={r.params[p] ?? min} onChange={(e) => patch(i, { params: { ...r.params, [p]: Number(e.target.value) } })} />
                </label>
              ))}
            </>
          )}
          <button type="button" className="btn link" onClick={() => emit(rules.filter((_, j) => j !== i))}>Удалить</button>
        </div>
      ))}
      <select className="control" aria-label="Добавить условие" value="" onChange={(e) => { if (e.target.value) emit([...rules, newRule(e.target.value as RuleKind)]); }}>
        <option value="">+ Добавить условие…</option>
        {(Object.keys(RULE_LABELS) as RuleKind[]).map((k) => <option key={k} value={k}>{RULE_LABELS[k]}</option>)}
      </select>
    </div>
  );
}

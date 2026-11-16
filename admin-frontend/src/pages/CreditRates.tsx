import { useState } from 'react';
import { api, type CreditRate } from '../api';
import { Card, DangerDialog } from '../ui';

const COUNTRY = /^([A-Z]{2}|\*)$/;
const CURRENCY = /^[A-Z]{3}$/;

/** Partner deposits that earn one forecast, per country: the Mini App's calculator (and the report import later). */
export function CreditRates({ rates, cost, canEdit, onSaved }: { rates: CreditRate[]; cost: number; canEdit: boolean; onSaved: () => void }) {
  const [rows, setRows] = useState<{ country: string; currency: string; perForecast: string }[]>(
    () => rates.map((r) => ({ ...r, perForecast: String(r.perForecast) })),
  );
  const [confirm, setConfirm] = useState(false);
  const clean = rows.map((r) => ({ country: r.country.trim().toUpperCase(), currency: r.currency.trim().toUpperCase(), perForecast: Number(r.perForecast) }));
  const valid = clean.every((r) => COUNTRY.test(r.country) && CURRENCY.test(r.currency) && Number.isInteger(r.perForecast) && r.perForecast > 0)
    && new Set(clean.map((r) => r.country)).size === clean.length;
  const set = (i: number, k: 'country' | 'currency' | 'perForecast', v: string) => setRows((prev) => prev.map((r, j) => (j === i ? { ...r, [k]: v } : r)));
  return (
    <Card title="Курс депозитов по гео" sub={`Сколько депозита у партнёра = 1 прогноз (${cost} кредитов). «*» — все остальные страны; страна пользователя — по IP`}>
      <table className="table">
        <thead><tr><th>Страна</th><th>Валюта</th><th className="num">Депозит на 1 прогноз</th>{canEdit && <th />}</tr></thead>
        <tbody>{rows.map((r, i) => (
          <tr key={i}>
            <td><input className="control mono" style={{ width: 56 }} maxLength={2} aria-label="Страна" value={r.country} disabled={!canEdit} onChange={(e) => set(i, 'country', e.target.value)} /></td>
            <td><input className="control mono" style={{ width: 70 }} maxLength={3} aria-label="Валюта" value={r.currency} disabled={!canEdit} onChange={(e) => set(i, 'currency', e.target.value)} /></td>
            <td className="num"><input className="control" style={{ width: 110 }} inputMode="numeric" aria-label="Депозит на 1 прогноз" value={r.perForecast} disabled={!canEdit} onChange={(e) => set(i, 'perForecast', e.target.value.replace(/\D/g, ''))} /></td>
            {canEdit && <td><button type="button" className="btn link" onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}>Удалить</button></td>}
          </tr>
        ))}</tbody>
      </table>
      {rows.length === 0 && <p className="small muted">Курсов нет — калькулятор в Mini App скрыт.</p>}
      {canEdit && (
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          <button type="button" className="btn" onClick={() => setRows((prev) => [...prev, { country: prev.some((r) => r.country === '*') ? '' : '*', currency: 'AZN', perForecast: '' }])}>Добавить строку</button>
          <button type="button" className="btn primary" disabled={!valid} onClick={() => setConfirm(true)}>Сохранить</button>
          {!valid && <span className="small muted">Страна — 2 буквы или «*», валюта — 3 буквы, сумма — целое число, страны не повторяются</span>}
        </div>
      )}
      {confirm && (
        <DangerDialog title="Сохранить курсы?" tone="primary" confirmLabel="Сохранить" lead="Калькулятор в Mini App обновится за несколько секунд."
          onClose={() => setConfirm(false)}
          onConfirm={async (input) => {
            await api('/api/admin/settings/credits.rates', { method: 'POST', body: { ...input, value: clean } });
            setConfirm(false);
            onSaved();
          }} />
      )}
    </Card>
  );
}

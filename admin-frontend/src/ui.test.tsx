import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from './api';
import { fromUtcText, localTime, parseLocalInput, toLocalInput } from './format';
import { DangerDialog } from './ui';

describe('DangerDialog', () => {
  it('requires a reason and a 6-digit code, then sends one idempotency key', async () => {
    const calls: { reason: string; code: string; idempotencyKey: string }[] = [];
    const onConfirm = vi.fn(async (input: { reason: string; code: string; idempotencyKey: string }) => {
      calls.push(input);
      if (calls.length === 1) throw new ApiError(403, 'invalid_code', 'rid1');
    });
    render(<DangerDialog title="Отозвать?" lead="lead" confirmLabel="Отозвать" onConfirm={onConfirm} onClose={() => {}} />);
    const confirm = screen.getByRole('button', { name: 'Отозвать' });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Причина'), 'поддержка');
    await userEvent.type(screen.getByLabelText('Код двухфакторной аутентификации'), '12ab34');
    expect(confirm).toBeDisabled(); // letters are stripped: only 4 digits
    await userEvent.type(screen.getByLabelText('Код двухфакторной аутентификации'), '56');
    await userEvent.click(confirm);
    expect(await screen.findByRole('alert')).toHaveTextContent('Неверный или уже использованный код');
    expect(screen.getByLabelText('Код двухфакторной аутентификации')).toHaveValue('');
    await userEvent.type(screen.getByLabelText('Код двухфакторной аутентификации'), '654321');
    await userEvent.click(confirm);
    expect(calls).toHaveLength(2);
    expect(calls[0]!.idempotencyKey).toBe(calls[1]!.idempotencyKey);
    expect(calls[1]).toMatchObject({ reason: 'поддержка', code: '654321' });
  });
});

describe('format', () => {
  it('shows times in the viewer zone and a dash for missing values', () => {
    const d = new Date('2026-11-21T12:30:00Z');
    const hh = String(d.getHours()).padStart(2, '0');
    expect(localTime('2026-11-21T12:30:00Z')).toBe(`${d.getDate()} ноя · ${hh}:30`);
    expect(localTime(null)).toBe('—');
    expect(toLocalInput(new Date(2026, 10, 21, 9, 5))).toBe('2026-11-21T09:05');
  });
  it('parses the scheduler input in the viewer zone and rejects empty or invalid values', () => {
    expect(parseLocalInput('2026-11-21T09:05')).toBe(Math.floor(new Date(2026, 10, 21, 9, 5).getTime() / 1000));
    expect(parseLocalInput('')).toBeNull();
    expect(parseLocalInput('2026-02-30T10:00')).toBeNull();
    // a wall time skipped by a DST change in the viewer's zone (none in zones without DST)
    for (let h = 0; h < 24 * 366; h += 1) {
      const t = new Date(2026, 0, 1, h);
      const next = new Date(t.getTime() + 3600_000);
      if (next.getHours() === (t.getHours() + 2) % 24) {
        const skipped = `${toLocalInput(t).slice(0, 11)}${String((t.getHours() + 1) % 24).padStart(2, '0')}:30`;
        expect(parseLocalInput(skipped)).toBeNull();
        break;
      }
    }
    expect(fromUtcText('2026-11-15 12:30:00')).toBe('2026-11-15T12:30:00Z');
    expect(fromUtcText(null)).toBeNull();
  });
});

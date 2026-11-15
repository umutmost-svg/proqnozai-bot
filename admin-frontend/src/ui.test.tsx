import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from './api';
import { utc } from './format';
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
  it('shows UTC times and a dash for missing values', () => {
    expect(utc('2026-11-21T12:30:00Z')).toBe('21 ноя · 12:30');
    expect(utc(null)).toBe('—');
  });
});

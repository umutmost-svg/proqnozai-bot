/**
 * Support contact and the bug-report text. There is no agreed delivery channel for
 * reports yet, so nothing is "sent" by the app: the text is prepared, copied, and the
 * user sends it in the support chat themselves — the app never claims a delivery.
 */

const USERNAME = /^[A-Za-z][A-Za-z0-9_]{4,31}$/;

/** The configured support account (build-time VITE_SUPPORT_TELEGRAM), or null when not set up. */
export function supportUsername(raw: string | undefined = import.meta.env.VITE_SUPPORT_TELEGRAM): string | null {
  const value = (raw ?? '').trim().replace(/^@/, '');
  return USERNAME.test(value) ? value : null;
}

export function supportLink(username: string): string {
  return `https://t.me/${username}`;
}

export type ReportTopic = 'matches' | 'ai' | 'app' | 'other';
export const REPORT_TOPICS: readonly ReportTopic[] = ['matches', 'ai', 'app', 'other'];
export const MAX_REPORT = 1000;
const EVENT_ID = /^[a-z]{2,4}:\d{1,12}$/;

export interface ReportInput {
  topicLabel: string;
  description: string;
  eventId: string;
  lang: string;
  timeZone: string;
  mode: string;
}

/**
 * Plain text for the support chat. Only what the user typed plus harmless technical
 * context (app version, mode, language, time zone) — never initData, tokens, cookies or
 * any session data.
 */
export function reportText(input: ReportInput, version: string = __APP_VERSION__): string {
  const lines = [
    `ProqnozAI — ${input.topicLabel}`,
    '',
    input.description.trim().slice(0, MAX_REPORT),
    '',
  ];
  const eventId = input.eventId.trim();
  if (EVENT_ID.test(eventId)) lines.push(`Match: ${eventId}`);
  lines.push(`App: ${version} · ${input.mode} · ${input.lang} · ${input.timeZone}`);
  return lines.join('\n');
}

export function validEventId(value: string): boolean {
  return value.trim() === '' || EVENT_ID.test(value.trim());
}

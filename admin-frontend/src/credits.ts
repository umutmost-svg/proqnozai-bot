/** Kinds of the Mini App's credit journal (docs/miniapp/credits.md): label and tag tone. */
export const CREDIT_KIND: Record<string, [string, 'success' | 'danger' | 'violet' | 'neutral' | 'accent']> = {
  welcome: ['Стартовые', 'accent'], monthly: ['Ежемесячные', 'accent'], spend: ['Прогноз', 'neutral'],
  grant: ['Начисление', 'success'], debit: ['Списание', 'danger'], unlimited: ['Безлимит', 'violet'],
};

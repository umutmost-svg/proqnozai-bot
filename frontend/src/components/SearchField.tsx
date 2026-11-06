import { forwardRef } from 'react';
import searchIcon from '../assets/figma/icon-search.svg';
import { useT } from '../state/contexts';
import s from './Filters.module.css';

const Icon = () => (
  <span className={s.searchIcon} aria-hidden="true">
    <img src={searchIcon} alt="" width={18} height={18} />
  </span>
);

/** Home: a button that opens the catalog search. */
export function SearchLauncher({ onOpen, value }: { onOpen(): void; value: string }) {
  const t = useT();
  return (
    <button type="button" className={s.search} onClick={onOpen}>
      <Icon />
      <span className={s.searchLabel}>{value || t('search.placeholder')}</span>
    </button>
  );
}

/** Catalog: live search input. */
export const SearchField = forwardRef<HTMLInputElement, { value: string; onChange(value: string): void }>(
  function SearchField({ value, onChange }, ref) {
    const t = useT();
    return (
      <label className={s.search}>
        <Icon />
        <span className="visually-hidden">{t('search.placeholder')}</span>
        <input
          ref={ref}
          className={s.searchInput}
          type="search"
          inputMode="search"
          enterKeyHint="search"
          autoComplete="off"
          placeholder={t('search.placeholder')}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        {value && (
          <button type="button" className={s.clear} onClick={() => onChange('')} aria-label={t('search.clear')}>
            ×
          </button>
        )}
      </label>
    );
  },
);

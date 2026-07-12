# ProqnozAI — Brand Language Guide

**Status:** Official localization standard · v1.0
**Primary market:** 🇦🇿 Azerbaijan · **Secondary:** 🇷🇺 Russia · 🇹🇷 Türkiye
**Scope:** This document is the single source of truth for all user-facing wording.
Every future pull request that adds or changes a user-visible string MUST follow it.

> This is a **product language** guide, not a dictionary translation. Azerbaijani is
> the lead language: we write AZ first, then adapt RU and TR. When AZ and a literal
> translation disagree, **AZ wins**.

---

## 0. How to read the confidence markers

Each recommended **Azerbaijani** term carries a status. AZ is our primary market, so
the marker reflects confidence in the *Azerbaijani* wording (RU and TR are given for
reference and are lower-risk).

| Marker | Meaning |
|---|---|
| ✅ **VERIFIED** | High confidence this is what Azerbaijani football users actually say. Ship it. |
| ⚠ **NEEDS REVIEW** | Plausible and used, but a native AZ football editor must confirm the exact form before it becomes canonical. |
| ❌ **DO NOT USE** | Wrong register, gambling framing, or textbook/government language. |

**Rule:** never upgrade a term to ✅ by guessing. If unsure, it stays ⚠ until a native
reviewer signs off. See §9 for the open-questions list.

---

## 1. Product positioning

**ProqnozAI is an AI football analyst.** It analyses matches, explains the data, and
shows statistics.

| ProqnozAI **is** | ProqnozAI **is NOT** |
|---|---|
| AI football analyst | A betting bot |
| Match-analysis assistant | A tipster |
| Football-statistics assistant | A gambling assistant |

We **analyse. explain. show data.** We **never promise wins.**

Odds/coefficients may appear inside an analysis, but only as **reference data** — a
number the market is showing — never as an instruction to place a bet. The voice around
odds is always analytical ("the market rates X as favourite"), never promotional
("bet on X now").

The word **proqnoz / прогноз / tahmin** is on-brand: it means an *analysis-based
projection*, the same way a weather forecast is. What we forbid is the language of
**guarantees and gambling calls-to-action** (see §3).

---

## 2. Product voice (Task 4)

### 2.1 Personality
Calm expert sitting next to a football fan. Knows the numbers, respects the fan's
intelligence, never hypes, never lectures. Think **SofaScore's clarity + a friend who
actually reads the stats.**

### 2.2 Principles
1. **Data first.** State what the numbers say before any opinion.
2. **Honest about uncertainty.** If data is missing, say so. Never fill gaps with
   invented facts.
3. **Short.** One idea per line.
4. **Friendly, not loud.** Modern and warm, never salesy or shouty.
5. **Neutral on outcomes.** We describe probability and form; we do not root, promise,
   or dare the user to bet.

### 2.3 Tone & formality
- **Azerbaijani:** modern football-media register ("Arsenal formada", "ev sahibi
  favoritdir"). **Avoid unnecessary direct address, and never mix `sən` and `siz`.**
  Prefer neutral imperative forms with no explicit pronoun — `Matçı seçin`,
  `Yenidən cəhd et`, `Geri`. Keep system messages impersonal.
- **Russian:** modern sports-app register, **"ты"** (like SofaScore / sports media), not
  the corporate "вы".
- **Turkish:** informal **"sen"**, Mackolik/Nesine register.

### 2.4 Length limits
| Element | Limit |
|---|---|
| Button label | **1–2 words** |
| Section heading | 1–2 words |
| Notification | ≤ 2 short sentences |
| UI sentence | **≤ 12 words** |
| AI analysis line | ≤ 16 words, one idea |

### 2.5 How the AI should speak (forecast text)
- Lead with the statistic, then the read: *"Arsenal son 5 oyunun 4-də qol buraxmayıb —
  müdafiəsi güvənlidir."*
- Mark estimates honestly: *"(təxmini)"* / *"(оценочно)"* / *"(tahmini)"*.
- Never state a scoreline or injury as fact unless it came from the data.
- End with a measured read, not a command.

### 2.6 Examples

**Azerbaijani**

| ❌ Bad | ✅ Good | Why |
|---|---|---|
| "Bu matçı mütləq Arsenal udacaq!" | "Statistikaya görə Arsenal favoritdir." | No guarantees; state the basis. |
| "Zəmanətli proqnoz 🔥" | "Analiz əsaslı proqnoz." | Kills the tipster framing. |
| "İndi mərc et, qaçırma!" | "Rəqəmlərə baxın, qərarı özünüz verin." | We analyse, we don't push bets. Neutral imperative, no `sən`. |
| "Ehtimal olunur ki, hər iki komandanın qol vurma ehtimalı yüksəkdir." | "Hər iki komanda qol vura bilər — statistika bunu göstərir." | Shorter, natural, one idea. |

**Russian**

| ❌ Bad | ✅ Good |
|---|---|
| "Гарантированный прогноз, точно зайдёт!" | "Разбор по данным: Арсенал — фаворит." |
| "Ставь на Арсенал прямо сейчас!" | "Смотрим статистику и решаешь сам." |

**Turkish**

| ❌ Bad | ✅ Good |
|---|---|
| "Kesin kazanır, hemen oyna!" | "İstatistiğe göre Arsenal favori." |
| "Garantili tahmin 🔥" | "Verilere dayalı analiz." |

### 2.7 Forbidden phrases (voice level)
Guarantees, certainty claims, and betting calls-to-action are banned in every language.
Full list in §3.

---

## 3. Words we NEVER use (Task 3)

These are banned because they turn an **analysis product** into a **gambling pitch**, or
because they make a promise we cannot keep.

| Banned meaning | AZ (do not use) | RU (do not use) | TR (do not use) | Why |
|---|---|---|---|---|
| Guaranteed | zəmanətli, 100% dəqiq | гарантированно, 100% точно | garantili, %100 kesin | We never promise outcomes. |
| Will definitely win | mütləq qazanacaq | точно выиграет / зайдёт | kesin kazanır | Certainty we cannot back. |
| Win money | pul qazan | выиграй деньги | para kazan | Gambling promise, not analysis. |
| Best bet / sure bet | ən yaxşı mərc, dəqiq mərc | лучшая ставка, верняк | en iyi bahis, banko | Tipster framing. |
| Bet now / place a bet | indi mərc et | ставь сейчас | hemen oyna / bahis yap | Call-to-action to gamble. |
| Easy money / free money | asan pul | лёгкие деньги | kolay para | Harmful and untrue. |
| Don't miss out | qaçırma! (as hype) | не упусти! | kaçırma! | Pressure/FOMO selling. |

**Also avoid (register problems, not bans):**
- ❌ `kef` / `keflər` for odds in AZ — **not** a real Azerbaijani betting term. Use
  **`əmsal`** (§4, Betting-adjacent data).
- ❌ Government/textbook AZ ("müraciət edilməsi tövsiyə olunur"). Write like an app, not a
  ministry.
- ❌ Google-Translate literalism (e.g. RU→AZ word-for-word of "Please try again later").

> Note: the neutral words **proqnoz / прогноз / tahmin** and **əmsal / коэффициент / oran**
> are allowed as analytical vocabulary. It is the *promise* and the *call-to-action* that
> are forbidden, not the football/data nouns.

---

## 4. Brand Dictionary (Tasks 1 & 2)

RU and TR are provided for reference. The **Status** column rates the **Azerbaijani**
recommendation. Buttons/headings should use the short form shown in **bold**.

### 4.1 Core product

| EN | 🇷🇺 Russian | 🇦🇿 Azerbaijani (recommended) | 🇹🇷 Turkish | Status | Reason |
|---|---|---|---|---|---|
| Match | Матч | **Matç** (also *oyun*) | Maç | ✅ | "Matç" and "oyun" both standard in AZ football talk. |
| Matches | Матчи | **Matçlar** | Maçlar | ✅ | Plural of the above. |
| Analysis | Разбор / Анализ | **Analiz** (formal: *təhlil*) | Analiz | ✅ | "Analiz" is the everyday media word; "təhlil" is fine but more formal. |
| AI Analysis | AI-анализ | **AI analiz** | AI analizi | ✅ | "AI" is understood; keep it short. |
| Prediction / Forecast | Прогноз | **Proqnoz** | Tahmin | ✅ | Brand word; analytical, not a betting promise. |
| Statistics | Статистика | **Statistika** | İstatistik | ✅ | Direct, universally used. |
| Summary | Итог / Вывод | **Yekun** | Özet | ✅ | Canonical closing-line heading. |
| Key factors | Ключевые факторы | **Əsas faktorlar** | Öne çıkanlar | ✅ | Natural; "faktor" is common in AZ. |
| Important | Важно | **Vacib** (alt: *Önəmli*) | Önemli | ✅ | Everyday word. |

### 4.2 Football data

| EN | 🇷🇺 | 🇦🇿 (recommended) | 🇹🇷 | Status | Reason |
|---|---|---|---|---|---|
| Form | Форма | **Forma** | Form | ✅ | Same football term across all three. |
| Recent matches | Последние матчи | **Son oyunlar** (alt: *Son matçlar*) | Son maçlar | ✅ | "Son oyunlar" is how fans say it. |
| Previous meetings (H2H) | Личные встречи | **Son qarşılaşmalar** | Karşılıklı maçlar | ✅ | Canonical AZ heading; RU "личные встречи" & TR "karşılıklı" are the native equivalents. |
| Standings / Table | Турнирная таблица | **Cədvəl** (full: *Turnir cədvəli*) | Puan durumu | ✅ | "Cədvəl" = the table; short and clear. |
| League | Лига | **Liqa** | Lig | ✅ | Standard. |
| Tournament | Турнир | **Turnir** | Turnuva | ✅ | Standard. |
| Goals | Голы | **Qollar** | Goller | ✅ | Standard. |
| Home (team) | Хозяева | **Ev sahibi** | Ev sahibi | ✅ | Native for the hosting side. |
| Away (team) | Гости | **Qonaq** | Deplasman | ✅ | AZ "qonaq", TR "deplasman" — both native. |
| Draw | Ничья | **Bərabərlik** (colloquial alt: *heç-heçə*) | Beraberlik | ✅ | Canonical UI term; "heç-heçə" is only a spoken alternative, not the product standard. |
| Draws | Ничьи | **Bərabərliklər** | Beraberlikler | ✅ | Plural of the canonical term. |
| Win | Победа | **Qələbə** | Galibiyet | ✅ | Standard. |
| Loss | Поражение | **Məğlubiyyət** | Mağlubiyet | ✅ | Standard. |
| Injuries | Травмы | **Zədələr** | Sakatlıklar | ✅ | "Zədə" = injury, standard. |
| Lineups | Составы | **Heyət** (starting XI: *Əsas heyət*) | Kadrolar / İlk 11 | ✅ | "Heyət" = squad; the starting eleven is "Əsas heyət". |
| Probability | Вероятность | **Ehtimal** | Olasılık | ✅ | "Ehtimal" is the correct AZ word (as in "yağış ehtimalı"). |
| Risk | Риск | **Risk** | Risk | ✅ | Loanword, universal. |
| Odds / Coefficient | Коэффициент | **Əmsal** | Oran | ✅ | Canonical. Misli.az/Topaz use "əmsal". Shown as data only. **Never** "kef". |

### 4.3 States & time

| EN | 🇷🇺 | 🇦🇿 (recommended) | 🇹🇷 | Status | Reason |
|---|---|---|---|---|---|
| Live | Live / Лайв | **Canlı** | Canlı | ✅ | Universal football-app word. |
| Today | Сегодня | **Bu gün** | Bugün | ✅ | Standard. |
| Tomorrow | Завтра | **Sabah** | Yarın | ✅ | Standard. |
| Finished | Завершён | **Bitib** | Bitti | ✅ | Natural spoken form. |
| Cancelled | Отменён | **Ləğv edilib** | İptal | ✅ | Standard. |
| Unavailable | Нет данных | **Mövcud deyil** (short: *Yoxdur*) | Mevcut değil / Yok | ✅ | "Data yoxdur" reads naturally. |
| Loading | Загрузка | **Yüklənir** | Yükleniyor | ✅ | Standard. |
| Please wait | Подожди | **Gözləyin** | Bekle | ✅ | Neutral imperative, no explicit pronoun. |

### 4.4 Navigation & app chrome

| EN | 🇷🇺 | 🇦🇿 (recommended) | 🇹🇷 | Status | Reason |
|---|---|---|---|---|---|
| Back | Назад | **Geri** | Geri | ✅ | Standard. |
| Close | Закрыть | **Bağla** | Kapat | ✅ | Standard. |
| Refresh | Обновить | **Yenilə** | Yenile | ✅ | Standard. |
| Search | Поиск | **Axtarış** | Ara | ✅ | Standard. |
| Try again / Retry | Ещё раз | **Yenidən cəhd et** | Tekrar dene | ✅ | Canonical neutral imperative. |
| Something went wrong | Что-то пошло не так | **Xəta baş verdi** | Bir şeyler ters gitti | ✅ | Canonical generic-error wording. |
| Profile | Профиль | **Profil** | Profil | ✅ | Standard. |
| Account | Аккаунт | **Hesab** | Hesap | ✅ | Standard. |
| Settings | Настройки | **Ayarlar** | Ayarlar | ✅ | Canonical; standard in modern AZ apps. |
| Favorites | Избранное | **Favorilər** | Favoriler | ✅ | Canonical; matches the AZ app convention. |
| History | История | **Tarixçə** (short: *Tarix*) | Geçmiş | ✅ | "Tarixçə" is standard for a history list. |
| Notifications | Уведомления | **Bildirişlər** | Bildirimler | ✅ | Standard. |

---

## 5. Menu review (Task 5)

Current bot menu vs. recommended. Buttons keep an emoji + **one word**. Changing the
label is safe — it is not a `callback_data` value.

| Purpose | 🇷🇺 Recommended | 🇦🇿 Recommended | 🇹🇷 Recommended | Decision |
|---|---|---|---|---|
| Main analysis action | ⚽ Прогноз | ⚽ **Proqnoz** | ⚽ Tahmin | Hero action = the brand word, singular. Drops the wordy "Прогнозы матчей / Matç proqnozları". |
| Multi-match slip | ⚡ Экспресс | ⚡ **Ekspress** | ⚡ Kombine | RU/AZ keep the native "экспресс/ekspress"; TR fans say "kombine". Framed as *combined analysis*, not "place a bet". |
| Past forecasts | 📋 История | 📋 **Tarixçə** | 📋 Geçmiş | Standard list label in each language. |
| User profile | 👤 Профиль | 👤 **Profil** | 👤 Profil | Universal. |
| Help / contact | 🆘 Поддержка | 🆘 **Dəstək** | 🆘 Destek | Standard. |
| Language switch | 🌐 Язык | 🌐 **Dil** | 🌐 Dil | One word beats "Сменить язык / Dil dəyiş / Dil değiştir". |
| Watch a match | 👁 Следить | 👁 **İzləyin** | 👁 Takip et | Short, SofaScore-style; neutral imperative (no `sən`). |

**Why:** every label now passes the 1–2 word rule, uses the word a local fan expects, and
carries an icon so meaning survives even at one word.

---

## 6. Onboarding (Task 6)

Goal: premium football-app first impression. Short, friendly, modern. AZ is the master;
RU/TR mirror the rhythm, not the words.

### 6.1 Welcome (first screen)

**🇦🇿 Azerbaijani (master)**
```
ProqnozAI 👋

Mən futbol üzrə AI analitikəm.
Matçı seçin — statistikanı, formanı və analizi göstərim.

Başlayaq 👇
```

**🇷🇺 Russian**
```
ProqnozAI 👋

Я — AI-аналитик по футболу.
Выбери матч — покажу статистику, форму и разбор.

Погнали 👇
```

**🇹🇷 Turkish**
```
ProqnozAI 👋

Ben futbol için AI analistiyim.
Maçı seç — istatistik, form ve analizi göstereyim.

Başlayalım 👇
```

### 6.2 "Which sport?" step
| 🇷🇺 | 🇦🇿 | 🇹🇷 |
|---|---|---|
| Что смотришь чаще всего? | Ən çox nəyi izləyirsiniz? | En çok neyi izliyorsun? |

### 6.3 "Experience?" step
Reframed away from betting skill toward how deep an analysis to give.
| 🇷🇺 | 🇦🇿 | 🇹🇷 |
|---|---|---|
| Насколько глубоко разбираешь матчи? | Matçları nə qədər dərin izləyirsiniz? | Maçları ne kadar derin takip ediyorsun? |

### 6.4 Done
| 🇷🇺 | 🇦🇿 | 🇹🇷 |
|---|---|---|
| Готово! Погнали 👇 | Hazırdır! Başlayaq 👇 | Hazır! Başlayalım 👇 |

**Why:** three lines, one promise ("pick a match → I show data"), warm sign-off. No
gambling framing; "experience" now means analysis depth, not betting skill.

---

## 7. Forecast layout — section headings (Task 7)

Headings are scannable labels, **1–2 words**, with a fixed emoji so the eye finds each
block instantly. Order reflects reading priority.

| Block | 🇷🇺 | 🇦🇿 (recommended) | 🇹🇷 | Status | Why this heading |
|---|---|---|---|---|---|
| 📊 Statistics | Статистика | **Statistika** | İstatistik | ✅ | Short, universal; leads because we are a data product. |
| 📈 Form | Форма | **Forma** | Form | ✅ | The one word every fan reads first. |
| 🔁 Previous meetings | Личные встречи | **Son qarşılaşmalar** | Karşılıklı | ✅ | Canonical AZ heading. |
| 🏆 Standings | Таблица | **Cədvəl** | Puan durumu | ✅ | Position in the table at a glance. |
| 🩹 Injuries | Травмы | **Zədələr** | Sakatlıklar | ✅ | Only shown when data exists; never implies "no injuries" when data is missing. |
| 👥 Lineups | Составы | **Heyət** (starting XI: *Əsas heyət*) | Kadrolar | ✅ | Canonical; never label "confirmed" unless the source confirms. |
| 🤖 AI analysis | AI-разбор | **AI analiz** | AI analizi | ✅ | Signals the synthesised read, distinct from raw data. |
| 🎯 Summary | Итог | **Yekun** | Özet | ✅ | Canonical closing-line heading. |

**Why headings beat sentences:** a fan skims. "📈 Forma" is found in 200 ms; "Komandaların
son oyunlardakı vəziyyəti" is a paragraph. Short nouns + fixed emoji = a layout that reads
like Flashscore/SofaScore, not a report.

**Data-honesty rule for every block:** if a block has no verified data, show the localized
"Mövcud deyil / Нет данных / Mevcut değil" note. Never fabricate, and never let an empty
block read as a positive fact (e.g. empty injuries ≠ "no injuries").

---

## 8. Application checklist (for future PRs)

Before a string ships, it must:
- [ ] Use a term from §4 (or add a new one **with a status marker**).
- [ ] Respect the length limits in §2.4.
- [ ] Contain **no** §3 forbidden phrase.
- [ ] Keep the analytical, no-promises voice of §2.
- [ ] Ship AZ + RU + TR together; AZ leads.
- [ ] Leave `callback_data`, dict keys, variable names, admin text and logs untouched.

---

## 9. Resolved Azerbaijani terminology decisions

The following were previously open; they are now **finalized** and canonical. These
decisions override any earlier draft wording and are not optional.

| Term | ✅ Canonical AZ | Superseded / alternative |
|---|---|---|
| Draw / Ничья | **Bərabərlik** | `heç-heçə` (colloquial only, not the UI standard) |
| Draws / Ничьи | **Bərabərliklər** | — |
| Previous meetings / H2H | **Son qarşılaşmalar** | `Üz-üzə`, `Qarşılaşmalar` |
| Starting lineup | **Əsas heyət** | `Start heyəti`, `İlk 11` |
| Summary / Итог | **Yekun** | `Xülasə` |
| Settings | **Ayarlar** | `Parametrlər` |
| Favorites | **Favorilər** | `Sevimlilər`, `Seçilmişlər` |
| Generic error | **Xəta baş verdi** | `Nəsə alınmadı` |
| Retry | **Yenidən cəhd et** | `Yenidən yoxla` |
| Odds / coefficient | **Əmsal** | `kef` ❌ |

There are no remaining ⚠ items for the Azerbaijani UI vocabulary. Everything marked ✅ in
§4 and §7 is stable and may be used immediately. The ⚠ mechanism in §0 remains in force for
**future** additions to the dictionary.

---

## 10. Governance

- This file is the **canonical** localization standard. If code and this guide disagree,
  this guide is right and the code should be migrated (in a dedicated PR).
- Adding a language or a term = update this guide **first**, then the code.
- A term stays ⚠ until a native reviewer moves it to ✅ in this document.
- Changes to voice/positioning (§1–§3) require product-owner sign-off.

*v1.0 — living document. Update the version and note the change when editing.*

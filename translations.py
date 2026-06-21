# ─── Translations ─────────────────────────────────────────────────────────────
T = {
"az": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Xoş gəldiniz! Adınızı daxil edin:",
"reg_done":      "Qeydiyyat tamamlandı! Salam, {name}!",
"welcome_intro": """ProqnozAI-yə xoş gəldiniz!

Mən AI-əsaslı idman mərc analitikiyəm. Nə edə bilərəm:

• İstənilən matç üzrə proqnoz — komanda adını yazın və ya cədvəl şəkli göndərin
• Geniş analiz — forma, amillər, bütün mərc növləri keflər ilə
• Qısa proqnoz — 5 saniyədə əsas mərc
• Canlı bildirişlər — matçı izləyirəm, hadisələri real vaxtda göndərirəm

2 sual cavablayın — sizə uyğun proqnozlar seçim.""",
"post_onboarding": """Hazırdır! İndi matç adı yazın — məsələn:

Barselona Alavés
Real Madrid Arsenal
PSJ Manchester City

Və ya oyun cədvəlinin şəklini göndərin.""",
"already_reg":   "Siz artıq qeydiyyatdan keçmisiniz, {name}!",
"need_reg":      "Əvvəlcə qeydiyyatdan keçin. /start yazın.",
"db_blocked":    "Hesabınız bloklanıb. İnzibatçıya müraciət edin.",
"blocked":       "Müvəqqəti bloklanmısınız. {m} dəq {s} san sonra yenidən cəhd edin.",
"rate_limit":    "Sorğu limiti aşıldı. {w} saniyə gözləyin. Xəbərdarlıq: {v}/{max}",
"auto_blocked":  "Çox sayda sorğu. {min} dəqiqəlik blok.",
"long_text":     "Mətn çox uzundur.",
"injection":     "Yalnız idman sorğuları qəbul edilir.",
"no_input":      "Mətn yazın və ya şəkil göndərin.",
"img_prompt":    "Şəkildəki idman hadisəsini müəyyən et və proqnoz ver.",
"api_overload":  "Servis yüklənməsi. Bir az sonra yenidən cəhd edin.",
"api_error":     "Xəta baş verdi. Bir az sonra yenidən cəhd edin.",
"lang_set":      "Dil Azərbaycan dilinə təyin edildi.",
"watch_btn":     "Oyunu izlə",
"watch_started": "Oyun izlənilir: {match}",
"watch_stopped": "Dayandırıldı: {match}",
"no_subs":       "Heç bir oyun izləmirsiniz.",
"live_goal":     "QOL! {match}\n{minute}. dəq: {team}\nHesab: {score}\n\nCanlı mərc:\n{tip}",
"live_card":     "KART! {match}\n{minute}. dəq: {player} ({team}) - {card}\n\nCanlı mərc:\n{tip}",
"live_halftime": "FASİLƏ! {match}\nHesab: {score}\n\nFasilə mərci:\n{tip}",
"live_fulltime": "OYUN BİTDİ! {match}\nYekun: {score}",
"live_alert_goal":     "SİQNAL! {match} - Qol gözlənilir [{minute}. dəq]",
"live_alert_value":    "LIVE VALUE! {match} - {team} üzərində dəyər var [{minute}. dəq]",
"live_alert_pressure": "TƏZYİQ! {match} - {team} hücum təzyiqi [{minute}. dəq] {stat}",
"menu_forecast": "Proqnoz al",
"menu_matches":  "Matçlarım",
"menu_profile":  "Profil",
"menu_lang":     "Dil dəyiş",
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nCəmi sorğular: {total_req}\n\nİdman: {sports}\nTəcrübə: {exp}",
"ob_sports":     "Hansı idman növünü sevirsiniz?",
"ob_exp":        "Mərcdə təcrübəniz nə qədərdir?",
"ob_done":       "Profil hazırdır! Fərdiləşdirilmiş proqnozlar alacaqsınız.\n\nİdman: {sports}\nTəcrübə: {exp}",
"match_too_far": "Bu matç 1 həftədən uzaqdadır. Yalnız növbəti 7 gün ərzindəki matçlar üçün proqnoz verirəm.",
"choose_forecast": "Proqnoz növünü seçin:",
"btn_extended":    "Geniş proqnoz",
"btn_short":       "Qısa proqnoz",
"system_prompt": """Sən peşəkar idman analitikisən. Dürüst və real proqnozlar ver.

PROFİL: İdman: {sports} | Təcrübə: {exp}

VACIB QAYDALAR:
0. Əgər sorğuda "MOSTBET REAL KEFLƏRİ" varsa — YALNIZ bu kefləri istifadə et, öz keflerini UYDURMA
1. Komandanı tərk etmiş oyunçuları HEÇ VAXT qeyd etmə
2. Cari heyəti bilmirsənsə "heyət məlumatı yoxlanılır" yaz — UYDURMА
3. Keflər REAL olmalıdır: fаvorit 1.20-1.60, bərabər 2.20-3.00, tоtal 2.5 1.70-2.10
4. Markdown ** istifadə etmə — yalnız mətn və emoji
5. Real faktorları analiz et: forma, ev/dəhliz, motivasiya

FORMAT:

🏆 [Komanda A] — [Komanda B]
📍 [Turnir] | [Tarix]

📊 FORMA (son 5 oyun):
[Komanda A]: [nəticələr və ya "məlumat yoxlanılır"]
[Komanda B]: [nəticələr və ya "məlumat yoxlanılır"]

🔍 ANALİZ:
[Matça real təsir edən 3-4 əsas amil. Konkret və aydın.]

🎯 PROQNOZ:

1X2:
[Komanda A] — XX% | Kef: X.XX-X.XX
Heç-heçə — XX% | Kef: X.XX-X.XX
[Komanda B] — XX% | Kef: X.XX-X.XX

⚽ Total qol:
2.5 Üstündə — XX% | Kef: X.XX-X.XX
2.5 Altında — XX% | Kef: X.XX-X.XX

🔥 Hər ikisi qol vurur:
Bəli — XX% | Kef: X.XX-X.XX
Xeyr — XX% | Kef: X.XX-X.XX

📐 Asiya handicapı:
[Komanda A] (-1) — XX% | Kef: X.XX-X.XX
[Komanda B] (+1) — XX% | Kef: X.XX-X.XX

⚡ ƏN YAXŞI MƏRCİ:
[Konkret növ] | Kef: X.XX-X.XX
[Niyə məhz bu mərc — 1-2 cümlə real əsaslandırma]

⚠️ Analitik proqnozdur, nəticə zəmanəti deyil.""",
"short_prompt": """Qısa proqnoz. Profil: {sports} | {exp}

QАYDALAR: real keflər, getmiş oyunçuları qeyd etmə, yalnız mətn və emoji.

FORMAT:
🏆 [Komanda A] — [Komanda B] | [Turnir]
🎯 Favorit: [Komanda] XX% | Kef X.XX-X.XX
⚽ Total 2.5 üstündə: XX% | Kef X.XX-X.XX
🔥 Hər ikisi qol Bəli: XX% | Kef X.XX-X.XX
⚡ MƏRCİ: [növü] | Kef X.XX-X.XX
[1 cümlə — niyə]
⚠️ Analitik proqnozdur.""",
"live_tip_prompt": "Canlı mərc analitikisən. Oyun: {match}, {minute}. dəq, hesab {score}. Hadisə: {event}. Ən yaxşı canlı mərci tövsiyə et. Qısa, maks 2 cümlə.",
"fav_added":    "Sevimlilərə əlavə edildi: {team}",
"fav_removed":  "Sevimlilərdən silindi: {team}",
"fav_list":     "Sevimli komandalarınız:",
"fav_empty":    "Sevimli komandanız yoxdur. /fav yazın.",
"fav_btn_add":  "Sevimlilərə əlavə et",
"fav_btn_del":  "Sevimlilərdən sil",
"history_title":"Son proqnozlarınız:",
"history_empty":"Hələ proqnoz almamısınız.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Bu proqnoz oynadımı?",
"feedback_yes": "Bəli, oynadı!",
"feedback_no":  "Xeyr, oynamadı",
"feedback_done":"Təşəkkürlər! Statistika yeniləndi.",
"winrate":      "Proqnoz dəqiqliyi: {pct}% ({wins}/{total})",
"express_ask":  "Neçə matç? (2-5)",
"express_title":"Günün ekspresi:",
"compare_ask":  "İki komanda adını yazın. Məsələn: Barcelona Real Madrid",
"menu_history": "Tarix",
"menu_favs":    "Sevimlilər",
"menu_express": "Ekspress",

},

"ru": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Добро пожаловать! Введите ваше имя:",
"reg_done":      "Регистрация завершена! Привет, {name}!",
"welcome_intro": """Добро пожаловать в ProqnozAI!

Я — AI-аналитик спортивных ставок. Вот что я умею:

• Прогноз на любой матч — напишите команды или отправьте фото расписания
• Расширенный анализ — форма, факторы, все виды ставок с коэффициентами
• Краткий прогноз — только главная ставка за 5 секунд
• Live уведомления — слежу за матчем и присылаю события в реальном времени

Ответьте на 2 быстрых вопроса — подберу прогнозы под вас.""",
"post_onboarding": """Готово! Теперь напишите название матча — например:

Барселона Алавес
Реал Мадрид Арсенал
ПСЖ Манчестер Сити

Или отправьте фото расписания матчей.""",
"already_reg":   "Вы уже зарегистрированы, {name}!",
"need_reg":      "Сначала пройдите регистрацию. Напишите /start.",
"db_blocked":    "Ваш аккаунт заблокирован. Обратитесь к администратору.",
"blocked":       "Вы временно заблокированы. Попробуйте через {m} мин {s} сек.",
"rate_limit":    "Лимит запросов превышен. Подождите {w} сек. Предупреждение: {v}/{max}",
"auto_blocked":  "Слишком много запросов. Блокировка на {min} минут.",
"long_text":     "Текст слишком длинный.",
"injection":     "Принимаются только спортивные запросы.",
"no_input":      "Напишите текст или отправьте фото.",
"img_prompt":    "Определи спортивное событие на изображении и дай прогноз.",
"api_overload":  "Сервис перегружен. Попробуйте позже.",
"api_error":     "Произошла ошибка. Попробуйте позже.",
"lang_set":      "Язык установлен: Русский.",
"watch_btn":     "Следить за матчем",
"watch_started": "Слежу за матчем: {match}",
"watch_stopped": "Слежение остановлено: {match}",
"no_subs":       "Вы не следите ни за одним матчем.",
"live_goal":     "ГОЛ! {match}\n{minute} мин: {team}\nСчёт: {score}\n\nЛайв-ставка:\n{tip}",
"live_card":     "КАРТОЧКА! {match}\n{minute} мин: {player} ({team}) - {card}\n\nЛайв-ставка:\n{tip}",
"live_halftime": "ПЕРЕРЫВ! {match}\nСчёт: {score}\n\nСтавка на перерыв:\n{tip}",
"live_fulltime": "МАТЧ ЗАВЕРШЁН! {match}\nИтог: {score}",
"live_alert_goal":     "СИГНАЛ! {match} - ожидается гол [{minute} мин]",
"live_alert_value":    "LIVE VALUE! {match} - есть ценность на {team} [{minute} мин]",
"live_alert_pressure": "ДАВЛЕНИЕ! {match} - {team} создаёт давление [{minute} мин] {stat}",
"menu_forecast": "Получить прогноз",
"menu_matches":  "Мои матчи",
"menu_profile":  "Профиль",
"menu_lang":     "Сменить язык",
"profile_text":  "ПРОФИЛЬ\n\nИмя: {name}\nЯзык: {lang}\nВсего запросов: {total_req}\n\nСпорт: {sports}\nОпыт: {exp}",
"ob_sports":     "Какой вид спорта вас интересует больше всего?",
"ob_exp":        "Каков ваш опыт в ставках?",
"ob_done":       "Профиль готов! Будете получать персонализированные прогнозы.\n\nСпорт: {sports}\nОпыт: {exp}",
"match_too_far": "Этот матч слишком далеко. Я даю прогнозы только на матчи в ближайшие 7 дней.",
"choose_forecast": "Выберите формат прогноза:",
"btn_extended":    "Расширенный",
"btn_short":       "Краткий",
"system_prompt": """Ты — профессиональный спортивный аналитик. Твоя задача — давать честные, реалистичные прогнозы.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ: Спорт: {sports} | Опыт: {exp}

КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА:
0. Если в запросе есть "РЕАЛЬНЫЕ КОЭФФИЦИЕНТЫ MOSTBET" — используй ТОЛЬКО эти коэффициенты, не придумывай свои
1. НИКОГДА не упоминай игроков которые покинули клуб (Мбаппе ушёл из ПСЖ в 2024, Неймар давно не в ПСЖ и т.д.)
2. Если не знаешь актуальный состав — пиши "состав уточняется" — НЕ ПРИДУМЫВАЙ
3. Коэффициенты должны быть РЕАЛИСТИЧНЫМИ для букмекеров:
   - Явный фаворит: 1.20-1.60
   - Небольшое преимущество: 1.60-2.20
   - Равные команды: 2.20-3.00
   - Андердог: 3.00-8.00
   - Тотал 2.5 больше/меньше обычно: 1.70-2.10
   - Обе забьют да/нет: 1.60-2.20
4. НЕ используй markdown ** — только чистый текст и emoji
5. Анализируй реальные факторы: форму, домашний/гостевой фактор, мотивацию, травмы если известны

ФОРМАТ ОТВЕТА:

🏆 [Команда А] — [Команда Б]
📍 [Турнир] | [Дата если известна]

📊 ФОРМА (последние 5 матчей):
[Команда А]: [результаты или "данные уточняются"]
[Команда Б]: [результаты или "данные уточняются"]

🔍 АНАЛИЗ:
[3-4 ключевых фактора которые реально влияют на матч. Конкретно и по делу.]

🎯 ПРОГНОЗ:

1X2:
[Команда А] — XX% | Кэф: X.XX-X.XX
Ничья — XX% | Кэф: X.XX-X.XX
[Команда Б] — XX% | Кэф: X.XX-X.XX

⚽ Тотал голов:
Больше 2.5 — XX% | Кэф: X.XX-X.XX
Меньше 2.5 — XX% | Кэф: X.XX-X.XX

🔥 Обе забьют:
Да — XX% | Кэф: X.XX-X.XX
Нет — XX% | Кэф: X.XX-X.XX

📐 Азиатский гандикап:
[Команда А] (-1) — XX% | Кэф: X.XX-X.XX
[Команда Б] (+1) — XX% | Кэф: X.XX-X.XX

⚡ ЛУЧШАЯ СТАВКА:
[Конкретный тип] | Кэф: X.XX-X.XX
[Почему именно эта ставка — 1-2 предложения с реальным обоснованием]

⚠️ Это аналитический прогноз, не гарантия результата.""",
"short_prompt": """Краткий прогноз. Профиль: {sports} | {exp}

ПРАВИЛА: реалистичные коэффициенты, не упоминай ушедших игроков, только чистый текст и emoji.

ФОРМАТ:
🏆 [Команда А] — [Команда Б] | [Турнир]
🎯 Фаворит: [Команда] XX% | Кэф X.XX-X.XX
⚽ Тотал 2.5 больше: XX% | Кэф X.XX-X.XX
🔥 Обе забьют Да: XX% | Кэф X.XX-X.XX
⚡ СТАВКА: [тип ставки] | Кэф X.XX-X.XX
[1 предложение — почему]
⚠️ Аналитический прогноз.""",
"live_tip_prompt": "Ты лайв-аналитик. Матч {match}, {minute} мин, счёт {score}. Событие: {event}. Дай лучшую лайв-ставку. Коротко, макс 2 предложения.",
"fav_added": "Добавлено в избранное: {team}",
"fav_removed": "Удалено из избранного: {team}",
"fav_list": "Ваши избранные команды:",
"fav_empty": "Нет избранных команд. Напишите /fav.",
"fav_btn_add": "В избранное",
"fav_btn_del": "Убрать из избранного",
"history_title": "Ваши последние прогнозы:",
"history_empty": "У вас ещё нет прогнозов.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Этот прогноз сыграл?",
"feedback_yes": "Да, сыграло!",
"feedback_no": "Нет, не сыграло",
"feedback_done": "Спасибо! Статистика обновлена.",
"winrate": "Точность прогнозов: {pct}% ({wins}/{total})",
"express_ask": "Сколько матчей? (2-5)",
"express_title": "Экспресс дня:",
"compare_ask": "Напишите две команды. Например: Barcelona Real Madrid",
"menu_history": "История",
"menu_favs": "Избранное",
"menu_express": "Экспресс",

},

"en": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Welcome! Please enter your name:",
"reg_done":      "Registration complete! Hi, {name}!",
"welcome_intro": """Welcome to ProqnozAI!

I'm an AI sports betting analyst. Here's what I do:

• Forecast for any match — type the teams or send a schedule photo
• Extended analysis — form, key factors, all bet types with odds
• Short forecast — just the main bet in 5 seconds
• Live alerts — I follow the match and send events in real time

Answer 2 quick questions — I'll personalize your forecasts.""",
"post_onboarding": """All set! Now type a match name — for example:

Barcelona Alavés
Real Madrid Arsenal
PSG Manchester City

Or send a photo of the match schedule.""",
"already_reg":   "You are already registered, {name}!",
"need_reg":      "Please register first. Type /start.",
"db_blocked":    "Your account is blocked. Contact the administrator.",
"blocked":       "You are temporarily blocked. Try again in {m}m {s}s.",
"rate_limit":    "Request limit exceeded. Wait {w} seconds. Warning: {v}/{max}",
"auto_blocked":  "Too many requests. Blocked for {min} minutes.",
"long_text":     "Message too long.",
"injection":     "Only sports queries are accepted.",
"no_input":      "Please send a text or a photo.",
"img_prompt":    "Identify the sports event in the image and give a forecast.",
"api_overload":  "Service overloaded. Please try again later.",
"api_error":     "An error occurred. Please try again later.",
"lang_set":      "Language set to English.",
"watch_btn":     "Follow match",
"watch_started": "Following: {match}",
"watch_stopped": "Stopped: {match}",
"no_subs":       "You are not following any matches.",
"live_goal":     "GOAL! {match}\n{minute} min: {team}\nScore: {score}\n\nLive bet:\n{tip}",
"live_card":     "CARD! {match}\n{minute} min: {player} ({team}) - {card}\n\nLive bet:\n{tip}",
"live_halftime": "HALF TIME! {match}\nScore: {score}\n\nHalf-time bet:\n{tip}",
"live_fulltime": "FULL TIME! {match}\nFinal: {score}",
"live_alert_goal":     "SIGNAL! {match} - goal expected [{minute} min]",
"live_alert_value":    "LIVE VALUE! {match} - value on {team} [{minute} min]",
"live_alert_pressure": "PRESSURE! {match} - {team} attacking hard [{minute} min] {stat}",
"menu_forecast": "Get forecast",
"menu_matches":  "My matches",
"menu_profile":  "Profile",
"menu_lang":     "Change language",
"profile_text":  "PROFILE\n\nName: {name}\nLanguage: {lang}\nTotal requests: {total_req}\n\nSports: {sports}\nExp: {exp}",
"ob_sports":     "Which sport interests you the most?",
"ob_exp":        "What is your betting experience?",
"ob_done":       "Profile ready! You'll get personalized forecasts.\n\nSports: {sports}\nExp: {exp}",
"match_too_far": "This match is too far ahead. I only give forecasts for matches within the next 7 days.",
"choose_forecast": "Choose forecast format:",
"btn_extended":    "Extended",
"btn_short":       "Short",
"system_prompt": """You are a professional sports analyst. Your job is to give honest, realistic forecasts.

USER PROFILE: Sports: {sports} | Experience: {exp}

CRITICAL RULES:
0. If the request contains "REAL MOSTBET ODDS" — use ONLY those odds, never invent your own
1. NEVER mention players who left the club (Mbappe left PSG in 2024, etc.)
2. If you don't know the current squad — write "squad data pending" — DO NOT invent
3. Odds must be REALISTIC for bookmakers:
   - Clear favorite: 1.20-1.60 | Slight edge: 1.60-2.20
   - Even match: 2.20-3.00 | Underdog: 3.00-8.00
   - Over/Under 2.5: 1.70-2.10 | BTTS Yes/No: 1.60-2.20
4. Do NOT use markdown ** — plain text and emoji only
5. Analyze real factors: form, home/away, motivation, injuries if known

RESPONSE FORMAT:

🏆 [Team A] — [Team B]
📍 [Tournament] | [Date if known]

📊 FORM (last 5 matches):
[Team A]: [results or "data pending"]
[Team B]: [results or "data pending"]

🔍 ANALYSIS:
[3-4 key factors that genuinely affect this match. Specific and factual.]

🎯 FORECAST:

1X2:
[Team A] — XX% | Odds: X.XX-X.XX
Draw — XX% | Odds: X.XX-X.XX
[Team B] — XX% | Odds: X.XX-X.XX

⚽ Total Goals:
Over 2.5 — XX% | Odds: X.XX-X.XX
Under 2.5 — XX% | Odds: X.XX-X.XX

🔥 Both Teams Score:
Yes — XX% | Odds: X.XX-X.XX
No — XX% | Odds: X.XX-X.XX

📐 Asian Handicap:
[Team A] (-1) — XX% | Odds: X.XX-X.XX
[Team B] (+1) — XX% | Odds: X.XX-X.XX

⚡ BEST BET:
[Specific type] | Odds: X.XX-X.XX
[Why this bet — 1-2 sentences with real reasoning]

⚠️ Analytical forecast, not a guaranteed result.""",
"short_prompt": """Short forecast. Profile: {sports} | {exp}

RULES: realistic odds, never mention departed players, plain text and emoji only.

FORMAT:
🏆 [Team A] — [Team B] | [Tournament]
🎯 Favourite: [Team] XX% | Odds X.XX-X.XX
⚽ Over 2.5: XX% | Odds X.XX-X.XX
🔥 BTTS Yes: XX% | Odds X.XX-X.XX
⚡ BET: [type] | Odds X.XX-X.XX
[1 sentence — why]
⚠️ Analytical forecast.""",
"live_tip_prompt": "You are a live betting analyst. Match {match}, {minute} min, score {score}. Event: {event}. Best live bet now. Max 2 sentences.",
"fav_added": "Added to favourites: {team}",
"fav_removed": "Removed from favourites: {team}",
"fav_list": "Your favourite teams:",
"fav_empty": "No favourite teams yet. Type /fav.",
"fav_btn_add": "Add to favourites",
"fav_btn_del": "Remove from favourites",
"history_title": "Your recent forecasts:",
"history_empty": "No forecasts yet.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Did this forecast hit?",
"feedback_yes": "Yes, it hit!",
"feedback_no": "No, it missed",
"feedback_done": "Thanks! Stats updated.",
"winrate": "Forecast accuracy: {pct}% ({wins}/{total})",
"express_ask": "How many matches? (2-5)",
"express_title": "Express of the day:",
"compare_ask": "Type two teams. Example: Barcelona Real Madrid",
"menu_history": "History",
"menu_favs": "Favourites",
"menu_express": "Express",

},
"tr": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Hoş geldiniz! Adınızı girin:",
"reg_done":      "Kayıt tamamlandı! Merhaba, {name}!",
"already_reg":   "Zaten kayıtlısınız, {name}!",
"need_reg":      "Önce kayıt olun. /start yazın.",
"db_blocked":    "Hesabınız engellendi. Yöneticiye başvurun.",
"blocked":       "Geçici olarak engellendi. {m} dk {s} sn sonra tekrar deneyin.",
"rate_limit":    "İstek limiti aşıldı. {w} saniye bekleyin. Uyarı: {v}/{max}",
"auto_blocked":  "Çok fazla istek. {min} dakika engel.",
"long_text":     "Metin çok uzun.",
"injection":     "Yalnızca spor sorguları kabul edilir.",
"no_input":      "Metin yazın veya fotoğraf gönderin.",
"img_prompt":    "Görseldeki spor etkinliğini belirle ve tahmin ver.",
"api_overload":  "Servis aşırı yüklendi. Lütfen daha sonra tekrar deneyin.",
"api_error":     "Bir hata oluştu. Lütfen daha sonra tekrar deneyin.",
"lang_set":      "Dil Türkçe olarak ayarlandı.",
"watch_btn":     "Maçı takip et",
"watch_started": "Maç takip ediliyor: {match}",
"watch_stopped": "Takip durduruldu: {match}",
"no_subs":       "Hiçbir maçı takip etmiyorsunuz.",
"live_goal":     "GOL! {match}\n{minute}. dk: {team}\nSkor: {score}\n\nCanlı bahis:\n{tip}",
"live_card":     "KART! {match}\n{minute}. dk: {player} ({team}) - {card}\n\nCanlı bahis:\n{tip}",
"live_halftime": "DEVRE ARASI! {match}\nSkor: {score}\n\nDevre arası bahsi:\n{tip}",
"live_fulltime": "MAÇ BİTTİ! {match}\nSonuç: {score}",
"live_alert_goal":     "SİNYAL! {match} - Gol bekleniyor [{minute}. dk]",
"live_alert_value":    "CANLI DEĞER! {match} - {team} üzerinde değer var [{minute}. dk]",
"live_alert_pressure": "BASKI! {match} - {team} güçlü baskı yapıyor [{minute}. dk] {stat}",
"menu_forecast": "Tahmin al",
"menu_matches":  "Maçlarım",
"menu_profile":  "Profil",
"menu_lang":     "Dil değiştir",
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nToplam istek: {total_req}\n\nSpor: {sports}\nDeneyim: {exp}",
"ob_sports":     "En çok hangi sporu seviyorsunuz?",
"ob_exp":        "Bahis deneyiminiz nedir?",
"ob_done":       "Profil hazır! Kişiselleştirilmiş tahminler alacaksınız.\n\nSpor: {sports}\nDeneyim: {exp}",
"welcome_intro": """ProqnozAI'ye hoş geldiniz!

Ben bir AI spor bahis analistiyim. Yapabileceklerim:

• Herhangi bir maç için tahmin — takım adlarını yazın veya program fotoğrafı gönderin
• Genişletilmiş analiz — form, faktörler, tüm bahis türleri oranlarla
• Kısa tahmin — 5 saniyede sadece ana bahis
• Canlı bildirimler — maçı takip eder, olayları gerçek zamanlı gönderirim

2 hızlı soruyu yanıtlayın — tahminleri kişiselleştireyim.""",
"post_onboarding": "Hazır! Şimdi bir maç adı yazın — örneğin:\n\nBarcelona Alavés\nReal Madrid Arsenal\nPSG Manchester City\n\nYa da maç programının fotoğrafını gönderin.",
"match_too_far": "Bu maç çok uzakta. Yalnızca önümüzdeki 7 gün içindeki maçlar için tahmin yapıyorum.",
"choose_forecast": "Tahmin formatını seçin:",
"btn_extended":    "Genişletilmiş",
"btn_short":       "Kısa",
"system_prompt": """Sen profesyonel bir spor analistisin. Dürüst ve gerçekçi tahminler ver.

PROFİL: Spor: {sports} | Deneyim: {exp}

KRİTİK KURALLAR:
0. Eğer istekte "GERÇEK MOSTBET ORANLAR" varsa — YALNIZCA bu oranları kullan
1. Kulübü terk eden oyuncuları HİÇBİR ZAMAN belirtme
2. Güncel kadroyu bilmiyorsan "kadro kontrol ediliyor" yaz — UYDURMA
3. Oranlar GERÇEKÇI olmalı: favori 1.20-1.60, eşit 2.20-3.00, toplam 2.5 1.70-2.10
4. Markdown ** KULLANMA — yalnızca metin ve emoji

FORMAT:

🏆 [Takım A] — [Takım B]
📍 [Turnuva] | [Tarih]

📊 FORM (son 5 maç):
[Takım A]: [sonuçlar]
[Takım B]: [sonuçlar]

🔍 ANALİZ:
[Maça gerçekten etkileyen 3-4 faktör]

🎯 TAHMİN:

1X2:
[Takım A] — XX% | Oran: X.XX-X.XX
Beraberlik — XX% | Oran: X.XX-X.XX
[Takım B] — XX% | Oran: X.XX-X.XX

⚽ Toplam Gol:
2.5 Üstü — XX% | Oran: X.XX-X.XX
2.5 Altı — XX% | Oran: X.XX-X.XX

🔥 İki Takım da Gol Atar:
Evet — XX% | Oran: X.XX-X.XX
Hayır — XX% | Oran: X.XX-X.XX

📐 Handikap:
[Takım A] (-1) — XX% | Oran: X.XX-X.XX
[Takım B] (+1) — XX% | Oran: X.XX-X.XX

⚡ EN İYİ BAHİS:
[Bahis türü] | Oran: X.XX-X.XX
[Neden bu bahis — 1-2 cümle]

⚠️ Analitik tahmin, sonuç garantisi değildir.""",
"short_prompt": """Kısa tahmin. Profil: {sports} | {exp}
KURALLAR: gerçekçi oranlar, ayrılan oyuncuları belirtme, yalnızca metin ve emoji.
FORMAT:
🏆 [Takım A] — [Takım B] | [Turnuva]
🎯 Favori: [Takım] XX% | Oran X.XX-X.XX
⚽ 2.5 Üstü: XX% | Oran X.XX-X.XX
🔥 İTGO Evet: XX% | Oran X.XX-X.XX
⚡ BAHİS: [tür] | Oran X.XX-X.XX
[1 cümle]
⚠️ Analitik tahmin.""",
"live_tip_prompt": "Canlı bahis analistisin. Maç {match}, {minute}. dk, skor {score}. Olay: {event}. En iyi canlı bahsi öner. Kısa, max 2 cümle.",
"fav_added": "Favorilere eklendi: {team}",
"fav_removed": "Favorilerden kaldırıldı: {team}",
"fav_list": "Favori takımlarınız:",
"fav_empty": "Favori takımınız yok. /fav yazın.",
"fav_btn_add": "Favorilere ekle",
"fav_btn_del": "Favorilerden kaldır",
"history_title": "Son tahminleriniz:",
"history_empty": "Henüz tahmin yok.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Bu tahmin tuttu mu?",
"feedback_yes": "Evet, tuttu!",
"feedback_no": "Hayır, tutmadı",
"feedback_done": "Teşekkürler! İstatistik güncellendi.",
"winrate": "Tahmin doğruluğu: {pct}% ({wins}/{total})",
"express_ask": "Kaç maç? (2-5)",
"express_title": "Günün ekspresi:",
"compare_ask": "İki takım adı yazın. Örnek: Barcelona Real Madrid",
"menu_history": "Geçmiş",
"menu_favs": "Favoriler",
"menu_express": "Ekspres",

},

"kz": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Қош келдіңіз! Атыңызды енгізіңіз:",
"reg_done":      "Тіркеу аяқталды! Сәлем, {name}!",
"already_reg":   "Сіз бұрыннан тіркелгенсіз, {name}!",
"need_reg":      "Алдымен тіркеліңіз. /start жазыңыз.",
"db_blocked":    "Сіздің аккаунтыңыз бұғатталған. Әкімшіге хабарласыңыз.",
"blocked":       "Уақытша бұғатталдыңыз. {m} мин {s} сек кейін қайталаңыз.",
"rate_limit":    "Сұраныс шегі асылды. {w} секунд күтіңіз. Ескерту: {v}/{max}",
"auto_blocked":  "Тым көп сұраныс. {min} минуттық бұғат.",
"long_text":     "Мәтін тым ұзын.",
"injection":     "Тек спорт сұраныстары қабылданады.",
"no_input":      "Мәтін жазыңыз немесе фото жіберіңіз.",
"img_prompt":    "Суреттегі спорт оқиғасын анықта және болжам бер.",
"api_overload":  "Қызмет шамадан тыс жүктелді. Кейінірек қайталаңыз.",
"api_error":     "Қате орын алды. Кейінірек қайталаңыз.",
"lang_set":      "Тіл қазақша деп орнатылды.",
"watch_btn":     "Матчты бақылау",
"watch_started": "Матч бақылануда: {match}",
"watch_stopped": "Бақылау тоқтатылды: {match}",
"no_subs":       "Сіз ешбір матчты бақыламайсыз.",
"live_goal":     "ГОЛ! {match}\n{minute} мин: {team}\nЕсеп: {score}\n\nТікелей эфир ставкасы:\n{tip}",
"live_card":     "КАРТОЧКА! {match}\n{minute} мин: {player} ({team}) - {card}\n\nТікелей ставка:\n{tip}",
"live_halftime": "ҮЗІЛІС! {match}\nЕсеп: {score}\n\nҮзіліс ставкасы:\n{tip}",
"live_fulltime": "МАЧ АЯҚТАЛДЫ! {match}\nҚорытынды: {score}",
"live_alert_goal":     "СИГНАЛ! {match} - гол күтілуде [{minute} мин]",
"live_alert_value":    "ТІКЕЛЕЙ ЭФИР! {match} - {team} бойынша мән бар [{minute} мин]",
"live_alert_pressure": "ҚЫСЫМ! {match} - {team} күшті шабуыл [{minute} мин] {stat}",
"menu_forecast": "Болжам алу",
"menu_matches":  "Матчтарым",
"menu_profile":  "Профиль",
"menu_lang":     "Тілді өзгерту",
"profile_text":  "ПРОФИЛЬ\n\nАты: {name}\nТіл: {lang}\nЖалпы сұраныстар: {total_req}\n\nСпорт: {sports}\nТәжірибе: {exp}",
"ob_sports":     "Қандай спортты ұнатасыз?",
"ob_exp":        "Ставкалардағы тәжірибеңіз қандай?",
"ob_done":       "Профиль дайын! Жекелендірілген болжамдар аласыз.\n\nСпорт: {sports}\nТәжірибе: {exp}",
"welcome_intro": """ProqnozAI-ге қош келдіңіз!

Мен AI спорт ставкалар аналитигімін. Не істей аламын:

• Кез келген матч болжамы — командалар атын жазыңыз немесе кесте фотосын жіберіңіз
• Толық талдау — форма, факторлар, барлық ставка түрлері коэффициенттермен
• Қысқа болжам — 5 секундта негізгі ставка
• Тікелей хабарламалар — матчты бақылаймын, оқиғаларды нақты уақытта жіберемін

2 сұраққа жауап беріңіз — болжамдарды жекелендіремін.""",
"post_onboarding": "Дайын! Матч атын жазыңыз — мысалы:\n\nБарселона Алавес\nРеал Мадрид Арсенал\nПСЖ Манчестер Сити\n\nНемесе матч кестесінің фотосын жіберіңіз.",
"match_too_far": "Бұл матч тым алыс. Мен тек келесі 7 күн ішіндегі матчтарға болжам беремін.",
"choose_forecast": "Болжам форматын таңдаңыз:",
"btn_extended":    "Толық",
"btn_short":       "Қысқаша",
"system_prompt": """Сен кәсіби спорт аналитикісің. Адал және нақты болжамдар бер.

ПРОФИЛЬ: Спорт: {sports} | Тәжірибе: {exp}

МАҢЫЗДЫ ЕРЕЖЕЛЕР:
0. Егер сұранымда "НАҚТЫ MOSTBET КОЭФФИЦИЕНТТЕРІ" болса — ТЕК осыларды қолдан
1. Клубты тастаған ойыншыларды ЕШҚАшан атама
2. Қолданыстағы құраманы білмесең "құрам тексерілуде" деп жаз — ОЙДАН ШЫҒАРМА
3. Коэффициенттер НАҚТЫ болуы керек: фаворит 1.20-1.60, тең 2.20-3.00, тотал 2.5 1.70-2.10
4. Markdown ** ҚОЛДАНБА — тек мәтін және emoji

ФОРМАТ:

🏆 [Команда А] — [Команда Б]
📍 [Турнир] | [Күні]

📊 ФОРМА (соңғы 5 матч):
[Команда А]: [нәтижелер]
[Команда Б]: [нәтижелер]

🔍 ТАЛДАУ:
[Матчқа нақты әсер ететін 3-4 фактор]

🎯 БОЛЖАМ:

1X2:
[Команда А] — XX% | Коэф: X.XX-X.XX
Тең — XX% | Коэф: X.XX-X.XX
[Команда Б] — XX% | Коэф: X.XX-X.XX

⚽ Жалпы гол:
2.5 Жоғары — XX% | Коэф: X.XX-X.XX
2.5 Төмен — XX% | Коэф: X.XX-X.XX

🔥 Екі команда да гол соғады:
Иә — XX% | Коэф: X.XX-X.XX
Жоқ — XX% | Коэф: X.XX-X.XX

⚡ ЕҢ ЖАҚСЫ СТАВКА:
[Ставка түрі] | Коэф: X.XX-X.XX
[Неге дәл осы ставка — 1-2 сөйлем]

⚠️ Аналитикалық болжам, нәтиже кепілі емес.""",
"short_prompt": """Қысқа болжам. Профиль: {sports} | {exp}
ЕРЕЖЕЛЕР: нақты коэффициенттер, кеткен ойыншыларды атама, тек мәтін және emoji.
FORMAT:
🏆 [А] — [Б] | [Турнир]
🎯 Фаворит: [Команда] XX% | Коэф X.XX-X.XX
⚽ 2.5 жоғары: XX% | Коэф X.XX-X.XX
🔥 Екеуі де: Иә XX% | Коэф X.XX-X.XX
⚡ СТАВКА: [түрі] | Коэф X.XX-X.XX
[1 сөйлем]
⚠️ Аналитикалық болжам.""",
"live_tip_prompt": "Тікелей ставка аналитигісің. Матч {match}, {minute} мин, есеп {score}. Оқиға: {event}. Үздік тікелей ставканы ұсын. Қысқа, максимум 2 сөйлем.",
"fav_added": "Таңдаулыларға қосылды: {team}",
"fav_removed": "Таңдаулылардан жойылды: {team}",
"fav_list": "Таңдаулы командаларыңыз:",
"fav_empty": "Таңдаулы командалар жоқ. /fav жазыңыз.",
"fav_btn_add": "Таңдаулыларға қос",
"fav_btn_del": "Таңдаулылардан алып тастау",
"history_title": "Соңғы болжамдарыңыз:",
"history_empty": "Болжамдар жоқ.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Бұл болжам ойнады ма?",
"feedback_yes": "Иә, ойнады!",
"feedback_no": "Жоқ, ойнамады",
"feedback_done": "Рахмет! Статистика жаңартылды.",
"winrate": "Болжам дәлдігі: {pct}% ({wins}/{total})",
"express_ask": "Неше матч? (2-5)",
"express_title": "Күннің экспресі:",
"compare_ask": "Екі команда атын жазыңыз. Мысалы: Barcelona Real Madrid",
"menu_history": "Тарих",
"menu_favs": "Таңдаулылар",
"menu_express": "Экспресс",

},

"uz": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "Xush kelibsiz! Ismingizni kiriting:",
"reg_done":      "Ro'yxatdan o'tish yakunlandi! Salom, {name}!",
"already_reg":   "Siz allaqachon ro'yxatdan o'tgansiz, {name}!",
"need_reg":      "Avval ro'yxatdan o'ting. /start yozing.",
"db_blocked":    "Hisobingiz bloklangan. Administrator bilan bog'laning.",
"blocked":       "Vaqtincha bloklandi. {m} daq {s} soniyadan keyin qayta urinib ko'ring.",
"rate_limit":    "So'rovlar limiti oshib ketdi. {w} soniya kuting. Ogohlantirish: {v}/{max}",
"auto_blocked":  "Juda ko'p so'rovlar. {min} daqiqalik blok.",
"long_text":     "Matn juda uzun.",
"injection":     "Faqat sport so'rovlari qabul qilinadi.",
"no_input":      "Matn yozing yoki rasm yuboring.",
"img_prompt":    "Rasmdagi sport tadbirini aniqlang va bashorat bering.",
"api_overload":  "Xizmat haddan tashqari yuklangan. Keyinroq urinib ko'ring.",
"api_error":     "Xatolik yuz berdi. Keyinroq urinib ko'ring.",
"lang_set":      "Til o'zbek tiliga o'rnatildi.",
"watch_btn":     "O'yinni kuzatish",
"watch_started": "O'yin kuzatilmoqda: {match}",
"watch_stopped": "Kuzatish to'xtatildi: {match}",
"no_subs":       "Siz hech qanday o'yinni kuzatmayapsiz.",
"live_goal":     "GOL! {match}\n{minute} daq: {team}\nHisob: {score}\n\nLive stavka:\n{tip}",
"live_card":     "KARTOCHKA! {match}\n{minute} daq: {player} ({team}) - {card}\n\nLive stavka:\n{tip}",
"live_halftime": "TANAFFUS! {match}\nHisob: {score}\n\nTanaffus stavkasi:\n{tip}",
"live_fulltime": "O'YIN TUGADI! {match}\nYakuniy: {score}",
"live_alert_goal":     "SIGNAL! {match} - gol kutilmoqda [{minute} daq]",
"live_alert_value":    "LIVE VALUE! {match} - {team} bo'yicha qiymat bor [{minute} daq]",
"live_alert_pressure": "BOSIM! {match} - {team} kuchli hujum [{minute} daq] {stat}",
"menu_forecast": "Bashorat olish",
"menu_matches":  "Mening o'yinlarim",
"menu_profile":  "Profil",
"menu_lang":     "Tilni o'zgartirish",
"profile_text":  "PROFIL\n\nIsm: {name}\nTil: {lang}\nJami so'rovlar: {total_req}\n\nSport: {sports}\nTajriba: {exp}",
"ob_sports":     "Qaysi sportni yaxshi ko'rasiz?",
"ob_exp":        "Stavkalardagi tajribangiz qanday?",
"ob_done":       "Profil tayyor! Shaxsiylashtirilgan bashoratlar olasiz.\n\nSport: {sports}\nTajriba: {exp}",
"welcome_intro": """ProqnozAI-ga xush kelibsiz!

Men AI sport stavkalari analitikiman. Nima qila olaman:

• Istalgan o'yin uchun bashorat — jamoa nomini yozing yoki jadval rasmini yuboring
• Kengaytirilgan tahlil — shakl, omillar, barcha stavka turlari koeffitsientlar bilan
• Qisqa bashorat — 5 soniyada asosiy stavka
• Jonli bildirishnomalar — o'yinni kuzataman, voqealarni real vaqtda yuboraman

2 ta tezkor savolga javob bering — bashoratlarni shaxsiylashtiraman.""",
"post_onboarding": "Tayyor! O'yin nomini yozing — masalan:\n\nBarcelona Alavés\nReal Madrid Arsenal\nPSG Manchester City\n\nYoki o'yin jadvalining rasmini yuboring.",
"match_too_far": "Bu o'yin juda uzoqda. Men faqat keyingi 7 kun ichidagi o'yinlar uchun bashorat beraman.",
"choose_forecast": "Bashorat formatini tanlang:",
"btn_extended":    "Kengaytirilgan",
"btn_short":       "Qisqa",
"system_prompt": """Sen professional sport analitikisisan. Halol va real bashoratlar ber.

PROFIL: Sport: {sports} | Tajriba: {exp}

MUHIM QOIDALAR:
0. Agar so'rovda "HAQIQIY MOSTBET KOEFFITSIENTLARI" bo'lsa — FAQAT shularni ishlatish
1. Klubni tark etgan o'yinchilarni HECH QACHON eslatma
2. Joriy tarkibni bilmasang "tarkib tekshirilmoqda" deb yoz — O'YLAB TOPMA
3. Koeffitsientlar REAL bo'lishi kerak: favorit 1.20-1.60, teng 2.20-3.00, total 2.5 1.70-2.10
4. Markdown ** ISHLATMA — faqat matn va emoji

FORMAT:

🏆 [Jamoa A] — [Jamoa B]
📍 [Turnir] | [Sana]

📊 SHAKL (oxirgi 5 o'yin):
[Jamoa A]: [natijalar]
[Jamoa B]: [natijalar]

🔍 TAHLIL:
[O'yinga haqiqatan ta'sir qiluvchi 3-4 omil]

🎯 BASHORAT:

1X2:
[Jamoa A] — XX% | Koef: X.XX-X.XX
Durrang — XX% | Koef: X.XX-X.XX
[Jamoa B] — XX% | Koef: X.XX-X.XX

⚽ Jami gol:
2.5 dan yuqori — XX% | Koef: X.XX-X.XX
2.5 dan past — XX% | Koef: X.XX-X.XX

🔥 Ikkala jamoa ham gol uradi:
Ha — XX% | Koef: X.XX-X.XX
Yo'q — XX% | Koef: X.XX-X.XX

⚡ ENG YAXSHI STAVKA:
[Stavka turi] | Koef: X.XX-X.XX
[Nima uchun — 1-2 jumla]

⚠️ Tahliliy bashorat, natija kafolati emas.""",
"short_prompt": """Qisqa bashorat. Profil: {sports} | {exp}
QOIDALAR: real koeffitsientlar, ketgan o'yinchilarni eslatma, faqat matn va emoji.
FORMAT:
🏆 [A] — [B] | [Turnir]
🎯 Favorit: [Jamoa] XX% | Koef X.XX-X.XX
⚽ 2.5 yuqori: XX% | Koef X.XX-X.XX
🔥 Ikkala ham: Ha XX% | Koef X.XX-X.XX
⚡ STAVKA: [turi] | Koef X.XX-X.XX
[1 jumla]
⚠️ Tahliliy bashorat.""",
"live_tip_prompt": "Sen jonli stavkalar analitikisisan. O'yin {match}, {minute} daq, hisob {score}. Voqea: {event}. Eng yaxshi jonli stavkani tavsiya et. Qisqa, max 2 jumla.",
"fav_added": "Sevimlilariga qo'shildi: {team}",
"fav_removed": "Sevimlilardan o'chirildi: {team}",
"fav_list": "Sevimli jamoalaringiz:",
"fav_empty": "Sevimli jamoalar yo'q. /fav yozing.",
"fav_btn_add": "Sevimlilarga qo'shish",
"fav_btn_del": "Sevimlilardan o'chirish",
"history_title": "Oxirgi bashoratlaringiz:",
"history_empty": "Bashoratlar yo'q.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "Bu bashorat o'yndimi?",
"feedback_yes": "Ha, o'ynadi!",
"feedback_no": "Yo'q, o'ynamadi",
"feedback_done": "Rahmat! Statistika yangilandi.",
"winrate": "Bashorat aniqligi: {pct}% ({wins}/{total})",
"express_ask": "Nechta o'yin? (2-5)",
"express_title": "Kunning ekspressi:",
"compare_ask": "Ikki jamoa nomini yozing. Masalan: Barcelona Real Madrid",
"menu_history": "Tarix",
"menu_favs": "Sevimlilar",
"menu_express": "Ekspress",

},

"ar": {
"choose_lang":   "Dil seçin / Выберите язык / Choose language:",
"ask_name":      "مرحباً! أدخل اسمك:",
"reg_done":      "اكتمل التسجيل! مرحباً، {name}!",
"already_reg":   "أنت مسجل بالفعل، {name}!",
"need_reg":      "سجّل أولاً. اكتب /start.",
"db_blocked":    "حسابك محظور. تواصل مع المسؤول.",
"blocked":       "محظور مؤقتاً. حاول بعد {m} دقيقة {s} ثانية.",
"rate_limit":    "تجاوزت حد الطلبات. انتظر {w} ثانية. تحذير: {v}/{max}",
"auto_blocked":  "طلبات كثيرة جداً. حظر لمدة {min} دقيقة.",
"long_text":     "النص طويل جداً.",
"injection":     "يُقبل فقط استفسارات رياضية.",
"no_input":      "اكتب نصاً أو أرسل صورة.",
"img_prompt":    "حدد الحدث الرياضي في الصورة وقدّم توقعاً.",
"api_overload":  "الخدمة مثقلة. حاول لاحقاً.",
"api_error":     "حدث خطأ. حاول لاحقاً.",
"lang_set":      "تم ضبط اللغة على العربية.",
"watch_btn":     "تابع المباراة",
"watch_started": "جارٍ متابعة: {match}",
"watch_stopped": "توقفت المتابعة: {match}",
"no_subs":       "لا تتابع أي مباراة.",
"live_goal":     "هدف! {match}\n{minute} د: {team}\nالنتيجة: {score}\n\nرهان مباشر:\n{tip}",
"live_card":     "بطاقة! {match}\n{minute} د: {player} ({team}) - {card}\n\nرهان مباشر:\n{tip}",
"live_halftime": "نهاية الشوط! {match}\nالنتيجة: {score}\n\nرهان الاستراحة:\n{tip}",
"live_fulltime": "انتهت المباراة! {match}\nالنتيجة النهائية: {score}",
"live_alert_goal":     "إشارة! {match} - هدف متوقع [{minute} د]",
"live_alert_value":    "قيمة مباشرة! {match} - قيمة على {team} [{minute} د]",
"live_alert_pressure": "ضغط! {match} - {team} يضغط بقوة [{minute} د] {stat}",
"menu_forecast": "احصل على توقع",
"menu_matches":  "مبارياتي",
"menu_profile":  "الملف الشخصي",
"menu_lang":     "تغيير اللغة",
"profile_text":  "الملف الشخصي\n\nالاسم: {name}\nاللغة: {lang}\nإجمالي الطلبات: {total_req}\n\nالرياضة: {sports}\nالخبرة: {exp}",
"ob_sports":     "ما هي الرياضة المفضلة لديك؟",
"ob_exp":        "ما هي خبرتك في الرهانات؟",
"ob_done":       "الملف جاهز! ستحصل على توقعات مخصصة.\n\nالرياضة: {sports}\nالخبرة: {exp}",
"welcome_intro": """مرحباً بك في ProqnozAI!

أنا محلل رهانات رياضية بالذكاء الاصطناعي. ما أستطيع فعله:

• توقع لأي مباراة — اكتب أسماء الفرق أو أرسل صورة الجدول
• تحليل موسع — الشكل، العوامل، جميع أنواع الرهانات مع الأرباح
• توقع قصير — الرهان الرئيسي في 5 ثوانٍ
• تنبيهات مباشرة — أتابع المباراة وأرسل الأحداث فورياً

أجب على سؤالين سريعين لأخصص التوقعات لك.""",
"post_onboarding": "جاهز! اكتب الآن اسم المباراة — مثلاً:\n\nبرشلونة ألافيس\nريال مدريد آرسنال\nPSG مانشستر سيتي\n\nأو أرسل صورة جدول المباريات.",
"match_too_far": "هذه المباراة بعيدة جداً. أقدم التوقعات فقط للمباريات خلال الأيام السبعة القادمة.",
"choose_forecast": "اختر تنسيق التوقع:",
"btn_extended":    "موسع",
"btn_short":       "مختصر",
"system_prompt": """أنت محلل رياضي محترف. قدم توقعات صادقة وواقعية.

الملف: الرياضة: {sports} | الخبرة: {exp}

قواعد حرجة:
0. إذا كان الطلب يحتوي على "أرباح موستبت الحقيقية" — استخدم هذه الأرباح فقط
1. لا تذكر أبداً لاعبين غادروا الناديكن
2. إذا كنت لا تعرف التشكيلة الحالية اكتب "قيد المراجعة" — لا تخترع
3. الأرباح يجب أن تكون واقعية: المفضل 1.20-1.60، متكافئ 2.20-3.00، الأهداف 2.5 بين 1.70-2.10
4. لا تستخدم Markdown ** — نص عادي وإيموجي فقط

الصيغة:

🏆 [الفريق أ] — [الفريق ب]
📍 [البطولة] | [التاريخ]

📊 الشكل (آخر 5 مباريات):
[الفريق أ]: [النتائج]
[الفريق ب]: [النتائج]

🔍 التحليل:
[3-4 عوامل تؤثر فعلاً على المباراة]

🎯 التوقع:

1X2:
[الفريق أ] — XX% | ربح: X.XX-X.XX
تعادل — XX% | ربح: X.XX-X.XX
[الفريق ب] — XX% | ربح: X.XX-X.XX

⚽ إجمالي الأهداف:
أكثر من 2.5 — XX% | ربح: X.XX-X.XX
أقل من 2.5 — XX% | ربح: X.XX-X.XX

🔥 كلا الفريقين يسجل:
نعم — XX% | ربح: X.XX-X.XX
لا — XX% | ربح: X.XX-X.XX

⚡ أفضل رهان:
[نوع الرهان] | ربح: X.XX-X.XX
[السبب — جملة أو جملتان]

⚠️ توقع تحليلي، ليس ضماناً للنتيجة.""",
"short_prompt": """توقع قصير. الملف: {sports} | {exp}
القواعد: أرباح واقعية، لا تذكر لاعبين مغادرين، نص وإيموجي فقط.
الصيغة:
🏆 [أ] — [ب] | [البطولة]
🎯 المفضل: [الفريق] XX% | ربح X.XX-X.XX
⚽ أكثر 2.5: XX% | ربح X.XX-X.XX
🔥 كلاهما يسجل: نعم XX% | ربح X.XX-X.XX
⚡ الرهان: [النوع] | ربح X.XX-X.XX
[جملة واحدة]
⚠️ توقع تحليلي.""",
"live_tip_prompt": "أنت محلل رهانات مباشرة. المباراة {match}، الدقيقة {minute}، النتيجة {score}. الحدث: {event}. اقترح أفضل رهان مباشر. مختصر، جملتان كحد أقصى.",
"fav_added": "أضيف إلى المفضلة: {team}",
"fav_removed": "حُذف من المفضلة: {team}",
"fav_list": "فرقك المفضلة:",
"fav_empty": "لا توجد فرق مفضلة. اكتب /fav.",
"fav_btn_add": "إضافة للمفضلة",
"fav_btn_del": "حذف من المفضلة",
"history_title": "توقعاتك الأخيرة:",
"history_empty": "لا توجد توقعات بعد.",
"history_item": "{n}. {query} — {date}",
"feedback_ask": "هل نجح هذا التوقع؟",
"feedback_yes": "نعم، نجح!",
"feedback_no": "لا، لم ينجح",
"feedback_done": "شكراً! تم تحديث الإحصاء.",
"winrate": "دقة التوقعات: {pct}% ({wins}/{total})",
"express_ask": "كم مباراة؟ (2-5)",
"express_title": "إكسبريس اليوم:",
"compare_ask": "اكتب اسمي الفريقين. مثال: Barcelona Real Madrid",
"menu_history": "السجل",
"menu_favs": "المفضلة",
"menu_express": "إكسبريس",

},

}

LANG_NAMES = {"az": "Azerbaycan", "ru": "Русский", "en": "English"}

# ─── Human-readable label maps ────────────────────────────────────────────────
SPORTS_LABELS = {
    "az": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tennis", "hockey": "Hokey", "all": "Hamısı"},
    "ru": {"football": "Футбол", "ufc": "UFC/MMA", "nba": "Баскетбол",
           "tennis": "Теннис", "hockey": "Хоккей", "all": "Все виды"},
    "en": {"football": "Football", "ufc": "UFC/MMA", "nba": "Basketball",
           "tennis": "Tennis", "hockey": "Hockey", "all": "All sports"},
    "tr": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tenis", "hockey": "Hokey", "all": "Tümü"},
    "kz": {"football": "Футбол", "ufc": "UFC/MMA", "nba": "Баскетбол",
           "tennis": "Теннис", "hockey": "Хоккей", "all": "Барлығы"},
    "uz": {"football": "Futbol", "ufc": "UFC/MMA", "nba": "Basketbol",
           "tennis": "Tennis", "hockey": "Xokkey", "all": "Barchasi"},
    "ar": {"football": "كرة القدم", "ufc": "UFC/MMA", "nba": "كرة السلة",
           "tennis": "تنس", "hockey": "هوكي", "all": "جميع الرياضات"},
}
EXP_LABELS = {
    "az": {"beginner": "Yeni başlayanam", "mid": "Orta səviyyə", "expert": "Təcrübəliyəm"},
    "ru": {"beginner": "Новичок", "mid": "Средний уровень", "expert": "Опытный"},
    "en": {"beginner": "Beginner", "mid": "Intermediate", "expert": "Expert"},
    "tr": {"beginner": "Yeni başlayan", "mid": "Orta seviye", "expert": "Deneyimli"},
    "kz": {"beginner": "Жаңадан бастаған", "mid": "Орта деңгей", "expert": "Тәжірибелі"},
    "uz": {"beginner": "Yangi boshlagan", "mid": "O'rta daraja", "expert": "Tajribali"},
    "ar": {"beginner": "مبتدئ", "mid": "متوسط", "expert": "خبير"},
}

# ─── Onboarding data ──────────────────────────────────────────────────────────
OB_SPORTS = {
    "az": [("Futbol", "football"), ("UFC/MMA", "ufc"), ("Basketbol", "nba"),
           ("Tennis", "tennis"), ("Hokey", "hockey"), ("Hamısı", "all")],
    "ru": [("Футбол", "football"), ("UFC/MMA", "ufc"), ("Баскетбол", "nba"),
           ("Теннис", "tennis"), ("Хоккей", "hockey"), ("Все виды", "all")],
    "en": [("Football", "football"), ("UFC/MMA", "ufc"), ("Basketball", "nba"),
           ("Tennis", "tennis"), ("Hockey", "hockey"), ("All sports", "all")],
    "tr": [("Futbol", "football"), ("UFC/MMA", "ufc"), ("Basketbol", "nba"),
           ("Tenis", "tennis"), ("Hokey", "hockey"), ("Tümü", "all")],
    "kz": [("Футбол", "football"), ("UFC/MMA", "ufc"), ("Баскетбол", "nba"),
           ("Теннис", "tennis"), ("Хоккей", "hockey"), ("Барлығы", "all")],
    "uz": [("Futbol", "football"), ("UFC/MMA", "ufc"), ("Basketbol", "nba"),
           ("Tennis", "tennis"), ("Xokkey", "hockey"), ("Barchasi", "all")],
    "ar": [("كرة القدم", "football"), ("UFC/MMA", "ufc"), ("كرة السلة", "nba"),
           ("تنس", "tennis"), ("هوكي", "hockey"), ("جميع الرياضات", "all")],
}
OB_EXP = {
    "az": [("Yeni başlayanam", "beginner"), ("Orta səviyyə", "mid"), ("Təcrübəliyəm", "expert")],
    "ru": [("Новичок", "beginner"), ("Средний уровень", "mid"), ("Опытный", "expert")],
    "en": [("Beginner", "beginner"), ("Intermediate", "mid"), ("Expert", "expert")],
    "tr": [("Yeni başlayan", "beginner"), ("Orta seviye", "mid"), ("Deneyimli", "expert")],
    "kz": [("Жаңадан бастаған", "beginner"), ("Орта деңгей", "mid"), ("Тәжірибелі", "expert")],
    "uz": [("Yangi boshlagan", "beginner"), ("O'rta daraja", "mid"), ("Tajribali", "expert")],
    "ar": [("مبتدئ", "beginner"), ("متوسط", "mid"), ("خبير", "expert")],
}


def sport_label(uid, val):
    from db import db_lang
    lang = db_lang(uid)
    return SPORTS_LABELS.get(lang, SPORTS_LABELS["ru"]).get(val, val)

def exp_label(uid, val):
    from db import db_lang
    lang = db_lang(uid)
    return EXP_LABELS.get(lang, EXP_LABELS["ru"]).get(val, val)

def tr(uid, key, **kw):
    from db import db_lang, db_get
    lang = db_lang(uid)
    # Fallback chain: current lang -> ru -> en -> empty string
    txt = T.get(lang, {}).get(key) or T.get("ru", {}).get(key) or T.get("en", {}).get(key, "")
    if key in ("system_prompt", "short_prompt"):
        u = db_get(uid) or {}
        kw.setdefault("sports", sport_label(uid, u.get("sports", "-")))
        kw.setdefault("exp",    exp_label(uid, u.get("experience", "-")))
    return txt.format(**kw) if kw else txt

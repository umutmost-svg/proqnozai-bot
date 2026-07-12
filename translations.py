# ─── Translations ─────────────────────────────────────────────────────────────
T = {
"az": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Xoş gəldiniz! Adınızı daxil edin:",
"reg_done":      "Qeydiyyat tamamlandı! Salam, {name}!",
"welcome_intro": """ProqnozAI-yə xoş gəldiniz!

Mən AI-əsaslı idman analitikiyəm. Nə edə bilərəm:

⚽ Matç proqnozları — idman növünü seçin, matçı tapın, dərhal analiz alın
⚡ Ekspress — 2-5 matçdan ibarət ekspress kupon
📸 Foto — cədvəl və ya afişanın şəklini göndərin
📋 Tarixçə — əvvəlki proqnozlarınıza baxın

İdman növünü seçin — başlayaq!""",
"post_onboarding": """Hazırdır! İndi nə etmək istəyirsiniz?

⚽ Matç proqnozu üçün "Matç proqnozları" düyməsini basın
⚡ Ekspress üçün "Ekspress" düyməsini basın
📸 Cədvəl şəklini göndərə bilərsiniz""",
"already_reg":   "Siz artıq qeydiyyatdan keçmisiniz, {name}!",
"need_reg":      "Əvvəlcə qeydiyyatdan keçin. /start yazın.",
"db_blocked":    "Hesabınız bloklanıb. İnzibatçıya müraciət edin.",
"blocked":       "Müvəqqəti bloklanmısınız. {m} dəq {s} san sonra yenidən cəhd edin.",
"rate_limit":    "Sorğu limiti aşıldı. {w} saniyə gözləyin. Xəbərdarlıq: {v}/{max}",
"auto_blocked":  "Çox sayda sorğu. {min} dəqiqəlik blok.",
"long_text":     "Mətn çox uzundur.",
"injection":     "Yalnız idman sorğuları qəbul edilir.",
"img_too_big":   "Şəkil çox böyükdür (maks 5 MB). Daha kiçik şəkil göndərin.",
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
"menu_forecast": "⚽ Matç proqnozları",
"menu_profile":  "👤 Profil",
"menu_lang":     "Dil dəyiş",
"profile_text":  "👤 PROFİL\n\nAd: {name}\nDil: {lang}\nSaat qurşağı: {tz}",
"ob_sports":     "Hansı idman növünü sevirsiniz?",
"ob_exp":        "Mərcdə təcrübəniz nə qədərdir?",
"ob_done":       "Hazırdır! Seçdiyiniz idman: {sports}. Artıq proqnoz ala bilərsiniz.",
"match_too_far": "Bu matç 1 həftədən uzaqdadır. Yalnız növbəti 7 gün ərzindəki matçlar üçün proqnoz verirəm.",
"ev_menu_expired":        "⚠️ Matç siyahısı köhnəlib. Zəhmət olmasa, siyahını yenidən açın.",
"ev_provider_unavailable": "⚠️ Matç məlumatları müvəqqəti əlçatmazdır. Bir az sonra yenidən cəhd edin.",
"ev_no_live":             "Canlı matç yoxdur.",
"ev_no_today":            "Bu gün matç yoxdur.",
"ev_no_tomorrow":         "Sabah matç yoxdur.",
"ev_more_leagues":        "ℹ️ Prioritet üzrə ilk 15 turnir göstərilir; başqaları da var.",
"ev_more_matches":        "ℹ️ Ən erkən 10 matç göstərilir; başqaları da var.",
"enr_football_unavailable": "ℹ️ Bu matç üçün doğrulanmış futbol məlumatı əlçatan deyil — proqnoz keflərə əsaslanır.",
"enr_standings_unavailable": "ℹ️ Turnir cədvəli əlçatan deyil.",
"enr_lineups_unavailable": "ℹ️ Heyətlər hələ məlum deyil.",
"enr_injuries_unavailable": "ℹ️ Zədə/diskvalifikasiya məlumatı əlçatan deyil.",
"enr_unverified":         "ℹ️ Matç doğrulana bilmədi — yalnız keflər göstərilir.",
"system_prompt": """Sən idman analitikisən. Qısa və aydın yaz. İstənilən idmanı analiz et.

QAYDALAR:
- Keflər varsa — yalnız onları istifadə et
- Yalnız mətn və emoji, ** markdown yox (mərc sətri istisna)
- FORMA: hər iştirakçının formasını YALNIZ verilən real məlumat əsasında yaz; məlumat yoxdursa "məlumat mövcud deyil" yaz — UYDURMA
- Real matç verileri varsa — ortalama qol sayını göstər
- Real məlumat yoxdursa bunu açıq yaz ("məlumat mövcud deyil") — fakt, nəticə, zədə və ya heyət UYDURMA
- Bütün proqnoz — maksimum 12 sətir

FORMAT (bütün sətirler MƏCBURI):

🏆 [A] — [B]
📍 [Turnir - matç verilerindən al] | [Tarix]

📊 [A]: [forma, 1 cümlə] | ⚽ ort. X.X/oyun (real data varsa)
📊 [B]: [forma, 1 cümlə] | ⚽ ort. X.X/oyun (real data varsa)

🎯 [A] — XX% | X.XX
Bərabərlik — XX% | X.XX  (tətbiq olunarsa)
[B] — XX% | X.XX

⚡ **Mərc: [növ] @ X.XX** — [1 cümlə, niyə]""",
"live_tip_prompt": "Canlı mərc analitikisən. Oyun: {match}, {minute}. dəq, hesab {score}. Hadisə: {event}. Ən yaxşı canlı mərci tövsiyə et. Qısa, maks 2 cümlə.",
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
"menu_history": "📋 Tarix",
"menu_express": "⚡ Ekspress",
"menu_support": "🆘 Dəstək",
"support_text": "🆘 *Dəstək*\n\nSuallarınız və ya problemləriniz varsa — bizə yazın:",
"support_btn":  "💬 Dəstəyə yazın",
"bot_updated":  "Bot yeniləndi! Yeni menyu aktiv oldu.",

},

"ru": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Добро пожаловать! Введите ваше имя:",
"reg_done":      "Регистрация завершена! Привет, {name}!",
"welcome_intro": """Добро пожаловать в ProqnozAI!

Я — AI-аналитик спортивных событий. Что умею:

⚽ Прогнозы матчей — выберите вид спорта, найдите матч, получите анализ
⚡ Экспресс — купон из 2-5 матчей с расчётом коэффициента
📸 Фото — отправьте фото расписания или афиши
📋 История — ваши предыдущие прогнозы

Выберите вид спорта и начнём!""",
"post_onboarding": """Готово! Что хотите сделать?

⚽ Для прогноза нажмите «Прогнозы матчей»
⚡ Для купона нажмите «Экспресс»
📸 Или отправьте фото расписания""",
"already_reg":   "Вы уже зарегистрированы, {name}!",
"need_reg":      "Сначала пройдите регистрацию. Напишите /start.",
"db_blocked":    "Ваш аккаунт заблокирован. Обратитесь к администратору.",
"blocked":       "Вы временно заблокированы. Попробуйте через {m} мин {s} сек.",
"rate_limit":    "Лимит запросов превышен. Подождите {w} сек. Предупреждение: {v}/{max}",
"auto_blocked":  "Слишком много запросов. Блокировка на {min} минут.",
"long_text":     "Текст слишком длинный.",
"injection":     "Принимаются только спортивные запросы.",
"img_too_big":   "Изображение слишком большое (макс 5 МБ). Отправьте фото поменьше.",
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
"menu_forecast": "⚽ Прогнозы матчей",
"menu_profile":  "👤 Профиль",
"menu_lang":     "Сменить язык",
"profile_text":  "👤 ПРОФИЛЬ\n\nИмя: {name}\nЯзык: {lang}\nЧасовой пояс: {tz}",
"ob_sports":     "Какой вид спорта вас интересует больше всего?",
"ob_exp":        "Каков ваш опыт в ставках?",
"ob_done":       "Готово! Выбранный спорт: {sports}. Можете запрашивать прогнозы.",
"match_too_far": "Этот матч слишком далеко. Я даю прогнозы только на матчи в ближайшие 7 дней.",
"ev_menu_expired":        "⚠️ Список матчей устарел. Пожалуйста, откройте его заново.",
"ev_provider_unavailable": "⚠️ Данные матчей временно недоступны. Попробуйте позже.",
"ev_no_live":             "Нет матчей вживую.",
"ev_no_today":            "Нет матчей сегодня.",
"ev_no_tomorrow":         "Нет матчей завтра.",
"ev_more_leagues":        "ℹ️ Показаны 15 приоритетных турниров; есть и другие.",
"ev_more_matches":        "ℹ️ Показаны первые 10 матчей по времени; есть и другие.",
"enr_football_unavailable": "ℹ️ Проверенные футбольные данные для этого матча недоступны — прогноз на основе коэффициентов.",
"enr_standings_unavailable": "ℹ️ Турнирная таблица недоступна.",
"enr_lineups_unavailable": "ℹ️ Составы пока неизвестны.",
"enr_injuries_unavailable": "ℹ️ Данные о травмах/дисквалификациях недоступны.",
"enr_unverified":         "ℹ️ Матч не удалось подтвердить — показаны только коэффициенты.",
"system_prompt": """Ты — спортивный аналитик. Пиши коротко и по делу. Анализируй любой спорт.

ПРАВИЛА:
- Если есть коэффициенты — используй только их
- Только текст и emoji, без ** markdown (кроме строки ставки)
- ФОРМА: описывай форму каждого ТОЛЬКО по предоставленным реальным данным; если данных нет — напиши "данные недоступны", НЕ выдумывай
- Если есть реальные данные матчей — укажи среднее количество голов
- Если реальных данных нет — честно укажи это ("данные недоступны"); НЕ выдумывай факты, результаты, травмы и составы
- Весь прогноз — максимум 12 строк

ФОРМАТ (все строки ОБЯЗАТЕЛЬНЫ):

🏆 [А] — [Б]
📍 [Турнир — взять из данных матча] | [Дата]

📊 [А]: [форма, 1 предложение] | ⚽ ср. X.X/игру (если есть данные)
📊 [Б]: [форма, 1 предложение] | ⚽ ср. X.X/игру (если есть данные)

🎯 [А] — XX% | X.XX
Ничья — XX% | X.XX  (если применимо)
[Б] — XX% | X.XX

⚡ **Ставка: [тип] @ X.XX** — [причина, 1 предложение]""",
"live_tip_prompt": "Ты лайв-аналитик. Матч {match}, {minute} мин, счёт {score}. Событие: {event}. Дай лучшую лайв-ставку. Коротко, макс 2 предложения.",
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
"menu_history": "📋 История",
"menu_express": "⚡ Экспресс",
"menu_support": "🆘 Поддержка",
"support_text": "🆘 *Поддержка*\n\nЕсли у вас вопросы или проблемы — напишите нам:",
"support_btn":  "💬 Написать в поддержку",
"bot_updated":  "Бот обновлён! Активировано новое меню.",

},

"en": {
"choose_lang":   "Dil secin / Выберите язык / Choose language:",
"ask_name":      "Welcome! Please enter your name:",
"reg_done":      "Registration complete! Hi, {name}!",
"welcome_intro": """Welcome to ProqnozAI!

I'm an AI sports analyst. Here's what I do:

⚽ Match forecasts — pick a sport, find a match, get instant analysis
⚡ Express — build a 2–5 match coupon with combined odds
📸 Photo — send a schedule or fixture photo
📋 History — view your previous forecasts

Choose your sport and let's go!""",
"post_onboarding": """All set! What would you like to do?

⚽ Tap «Match forecasts» for a prediction
⚡ Tap «Express» for a coupon
📸 Or send a photo of a fixture""",
"already_reg":   "You are already registered, {name}!",
"need_reg":      "Please register first. Type /start.",
"db_blocked":    "Your account is blocked. Contact the administrator.",
"blocked":       "You are temporarily blocked. Try again in {m}m {s}s.",
"rate_limit":    "Request limit exceeded. Wait {w} seconds. Warning: {v}/{max}",
"auto_blocked":  "Too many requests. Blocked for {min} minutes.",
"long_text":     "Message too long.",
"injection":     "Only sports queries are accepted.",
"img_too_big":   "Image too large (max 5 MB). Please send a smaller photo.",
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
"menu_forecast": "⚽ Match forecasts",
"menu_profile":  "👤 Profile",
"menu_lang":     "Change language",
"profile_text":  "👤 PROFILE\n\nName: {name}\nLanguage: {lang}\nTimezone: {tz}",
"ob_sports":     "Which sport interests you the most?",
"ob_exp":        "What is your betting experience?",
"ob_done":       "Done! Your sport: {sports}. You can now request forecasts.",
"match_too_far": "This match is too far ahead. I only give forecasts for matches within the next 7 days.",
"ev_menu_expired":        "⚠️ This match list has expired. Please reopen it.",
"ev_provider_unavailable": "⚠️ Match data is temporarily unavailable. Please try again later.",
"ev_no_live":             "No live matches.",
"ev_no_today":            "No matches today.",
"ev_no_tomorrow":         "No matches tomorrow.",
"ev_more_leagues":        "ℹ️ Showing the top 15 tournaments by priority; more exist.",
"ev_more_matches":        "ℹ️ Showing the earliest 10 matches; more exist.",
"enr_football_unavailable": "ℹ️ Verified football data is unavailable for this match — the forecast is based on the odds.",
"enr_standings_unavailable": "ℹ️ League standings are unavailable.",
"enr_lineups_unavailable": "ℹ️ Lineups are not yet available.",
"enr_injuries_unavailable": "ℹ️ Injury/suspension data is unavailable.",
"enr_unverified":         "ℹ️ The match could not be verified — showing odds only.",
"system_prompt": """You are a sports analyst. Be brief and direct. Analyse any sport.

RULES:
- If odds are provided — use only those
- Plain text and emoji only, no ** markdown (except the bet line)
- FORM: describe each participant's form ONLY from the provided real data; if none is provided, write "data unavailable" — do NOT invent
- If real match data is provided — include avg goals per game
- If real data is missing, state it honestly ("data unavailable"); do NOT fabricate facts, results, injuries or lineups
- Entire forecast — 12 lines max

FORMAT (all lines MANDATORY):

🏆 [A] — [B]
📍 [Tournament — take from match data] | [Date]

📊 [A]: [form, 1 sentence] | ⚽ avg X.X/game (if data available)
📊 [B]: [form, 1 sentence] | ⚽ avg X.X/game (if data available)

🎯 [A] — XX% | X.XX
Draw — XX% | X.XX  (if applicable)
[B] — XX% | X.XX

⚡ **Bet: [type] @ X.XX** — [1 sentence why]""",
"live_tip_prompt": "You are a live betting analyst. Match {match}, {minute} min, score {score}. Event: {event}. Best live bet now. Max 2 sentences.",
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
"menu_history": "📋 History",
"menu_express": "⚡ Express",
"menu_support": "🆘 Support",
"support_text": "🆘 *Support*\n\nIf you have questions or issues — contact us:",
"support_btn":  "💬 Contact support",
"bot_updated":  "Bot updated! New menu is now active.",

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
"img_too_big":   "Görsel çok büyük (maks 5 MB). Daha küçük bir fotoğraf gönderin.",
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
"menu_forecast": "⚽ Maç tahminleri",
"menu_profile":  "👤 Profil",
"menu_lang":     "Dil değiştir",
"profile_text":  "👤 PROFİL\n\nAd: {name}\nDil: {lang}\nSaat dilimi: {tz}",
"ob_sports":     "En çok hangi sporu seviyorsunuz?",
"ob_exp":        "Bahis deneyiminiz nedir?",
"ob_done":       "Hazır! Seçilen spor: {sports}. Artık tahmin alabilirsiniz.",
"welcome_intro": """ProqnozAI'ye hoş geldiniz!

Ben bir AI spor analistiyim. Yapabileceklerim:

⚽ Maç tahminleri — sporu seçin, maçı bulun, anında analiz alın
⚡ Ekspress — 2–5 maçlık kupon oluşturun
📸 Fotoğraf — fikstür veya program fotoğrafı gönderin
📋 Geçmiş — önceki tahminlerinizi görüntüleyin

Spor seçin ve başlayalım!""",
"post_onboarding": "Hazır! Ne yapmak istersiniz?\n\n⚽ Tahmin için «Maç tahminleri» tuşuna basın\n⚡ Kupon için «Ekspress» tuşuna basın\n📸 Ya da fikstür fotoğrafı gönderin",
"match_too_far": "Bu maç çok uzakta. Yalnızca önümüzdeki 7 gün içindeki maçlar için tahmin yapıyorum.",
"ev_menu_expired":        "⚠️ Maç listesi zaman aşımına uğradı. Lütfen yeniden açın.",
"ev_provider_unavailable": "⚠️ Maç verileri geçici olarak kullanılamıyor. Lütfen daha sonra tekrar deneyin.",
"ev_no_live":             "Canlı maç yok.",
"ev_no_today":            "Bugün maç yok.",
"ev_no_tomorrow":         "Yarın maç yok.",
"ev_more_leagues":        "ℹ️ Önceliğe göre ilk 15 turnuva gösteriliyor; başkaları da var.",
"ev_more_matches":        "ℹ️ En erken 10 maç gösteriliyor; başkaları da var.",
"enr_football_unavailable": "ℹ️ Bu maç için doğrulanmış futbol verisi yok — tahmin oranlara dayanıyor.",
"enr_standings_unavailable": "ℹ️ Puan durumu mevcut değil.",
"enr_lineups_unavailable": "ℹ️ Kadrolar henüz belli değil.",
"enr_injuries_unavailable": "ℹ️ Sakatlık/ceza verisi mevcut değil.",
"enr_unverified":         "ℹ️ Maç doğrulanamadı — yalnızca oranlar gösteriliyor.",
"system_prompt": """Sen spor analistisin. Kısa ve net yaz. Her sporu analiz et.

KURALLAR:
- Oran varsa — sadece onları kullan
- Sadece metin ve emoji, ** markdown yok (bahis satırı hariç)
- FORM: her katılımcının formunu YALNIZCA verilen gerçek veriye göre yaz; veri yoksa "veri mevcut değil" yaz — UYDURMA
- Gerçek maç verisi varsa — ortalama gol sayısını göster
- Gerçek veri yoksa bunu dürüstçe belirt ("veri mevcut değil"); gerçek, sonuç, sakatlık veya kadro UYDURMA
- Tüm tahmin — maksimum 12 satır

FORMAT (tüm satırlar ZORUNLU):

🏆 [A] — [B]
📍 [Turnuva — maç verisinden al] | [Tarih]

📊 [A]: [form, 1 cümle] | ⚽ ort. X.X/maç (veri varsa)
📊 [B]: [form, 1 cümle] | ⚽ ort. X.X/maç (veri varsa)

🎯 [A] — XX% | X.XX
Beraberlik — XX% | X.XX  (geçerliyse)
[B] — XX% | X.XX

⚡ **Bahis: [tür] @ X.XX** — [1 cümle neden]""",
"live_tip_prompt": "Canlı bahis analistisin. Maç {match}, {minute}. dk, skor {score}. Olay: {event}. En iyi canlı bahsi öner. Kısa, max 2 cümle.",
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
"menu_history": "📋 Geçmiş",
"menu_express": "⚡ Ekspres",
"menu_support": "🆘 Destek",
"support_text": "🆘 *Destek*\n\nSorularınız veya sorunlarınız varsa — bize yazın:",
"support_btn":  "💬 Desteğe yaz",
"bot_updated":  "Bot güncellendi! Yeni menü aktif oldu.",

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
"img_too_big":   "Сурет тым үлкен (макс 5 МБ). Кішірек сурет жіберіңіз.",
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
"menu_forecast": "⚽ Матч болжамдары",
"menu_profile":  "👤 Профиль",
"menu_lang":     "Тілді өзгерту",
"profile_text":  "👤 ПРОФИЛЬ\n\nАты: {name}\nТіл: {lang}\nУақыт белдеуі: {tz}",
"ob_sports":     "Қандай спортты ұнатасыз?",
"ob_exp":        "Ставкалардағы тәжірибеңіз қандай?",
"ob_done":       "Дайын! Спорт: {sports}. Болжам сұрай аласыз.",
"welcome_intro": """ProqnozAI-ге қош келдіңіз!

Мен AI спорт аналитигімін. Не істей аламын:

⚽ Матч болжамдары — спорт түрін таңдаңыз, матч тауып, талдау алыңыз
⚡ Экспресс — 2–5 матчтан купон жасаңыз
📸 Фото — кесте немесе афиша фотосын жіберіңіз
📋 Тарих — алдыңғы болжамдарыңызды қараңыз

Спорт түрін таңдап, бастайық!""",
"post_onboarding": "Дайын! Не істегіңіз келеді?\n\n⚽ Болжам үшін «Матч болжамдары» батырмасын басыңыз\n⚡ Купон үшін «Экспресс» батырмасын басыңыз\n📸 Немесе кесте фотосын жіберіңіз",
"match_too_far": "Бұл матч тым алыс. Мен тек келесі 7 күн ішіндегі матчтарға болжам беремін.",
"ev_menu_expired":        "⚠️ Матчтар тізімі ескірді. Оны қайта ашыңыз.",
"ev_provider_unavailable": "⚠️ Матч деректері уақытша қолжетімсіз. Кейінірек қайталаңыз.",
"ev_no_live":             "Тікелей эфирде матч жоқ.",
"ev_no_today":            "Бүгін матч жоқ.",
"ev_no_tomorrow":         "Ертең матч жоқ.",
"ev_more_leagues":        "ℹ️ Басымдық бойынша алғашқы 15 турнир көрсетілген; тағы бар.",
"ev_more_matches":        "ℹ️ Ең ерте 10 матч көрсетілген; тағы бар.",
"enr_football_unavailable": "ℹ️ Бұл матч бойынша расталған футбол деректері жоқ — болжам коэффициенттерге негізделген.",
"enr_standings_unavailable": "ℹ️ Турнир кестесі қолжетімсіз.",
"enr_lineups_unavailable": "ℹ️ Құрамдар әзірге белгісіз.",
"enr_injuries_unavailable": "ℹ️ Жарақат/дисквалификация деректері қолжетімсіз.",
"enr_unverified":         "ℹ️ Матчты растау мүмкін болмады — тек коэффициенттер көрсетілген.",
"system_prompt": """Сен спорт аналитикісің. Қысқа және нақты жаз. Кез келген спортты талда.

ЕРЕЖЕЛЕР:
- Коэффициенттер берілсе — тек соларды қолдан
- Тек мәтін және emoji, ** markdown жоқ (ставка жолы ерекше)
- ФОРМА: әр қатысушының формасын ТЕК берілген нақты дерекпен жаз; дерек болмаса "деректер қолжетімсіз" деп жаз — ОЙДАН ШЫҒАРМА
- Нақты матч деректері болса — орташа гол санын көрсет
- Нақты дерек болмаса — оны шыншыл көрсет ("деректер қолжетімсіз"); факті, нәтиже, жарақат, құрам ОЙДАН ШЫҒАРМА
- Бүкіл болжам — максимум 12 жол

ФОРМАТ (барлық жолдар МІНДЕТТІ):

🏆 [А] — [Б]
📍 [Турнир — матч деректерінен ал] | [Күні]

📊 [А]: [форма, 1 сөйлем] | ⚽ орт. X.X/ойын (деректер болса)
📊 [Б]: [форма, 1 сөйлем] | ⚽ орт. X.X/ойын (деректер болса)

🎯 [А] — XX% | X.XX
Тең — XX% | X.XX  (қолданылса)
[Б] — XX% | X.XX

⚡ **Ставка: [түрі] @ X.XX** — [1 сөйлем, неге]""",
"live_tip_prompt": "Тікелей ставка аналитигісің. Матч {match}, {minute} мин, есеп {score}. Оқиға: {event}. Үздік тікелей ставканы ұсын. Қысқа, максимум 2 сөйлем.",
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
"menu_history": "📋 Тарих",
"menu_express": "⚡ Экспресс",
"menu_support": "🆘 Қолдау",
"support_text": "🆘 *Қолдау*\n\nСұрақтарыңыз немесе мәселелеріңіз болса — бізге жазыңыз:",
"support_btn":  "💬 Қолдауға жазу",
"bot_updated":  "Бот жаңартылды! Жаңа мәзір белсенді болды.",

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
"img_too_big":   "Rasm juda katta (maks 5 MB). Kichikroq rasm yuboring.",
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
"menu_forecast": "⚽ O'yin bashoratlari",
"menu_profile":  "👤 Profil",
"menu_lang":     "Tilni o'zgartirish",
"profile_text":  "👤 PROFIL\n\nIsm: {name}\nTil: {lang}\nVaqt mintaqasi: {tz}",
"ob_sports":     "Qaysi sportni yaxshi ko'rasiz?",
"ob_exp":        "Stavkalardagi tajribangiz qanday?",
"ob_done":       "Tayyor! Sport: {sports}. Bashorat so'rasangiz bo'ladi.",
"welcome_intro": """ProqnozAI-ga xush kelibsiz!

Men AI sport analitikiman. Nima qila olaman:

⚽ O'yin bashoratlari — sport turini tanlang, o'yin toping, tahlil oling
⚡ Ekspress — 2–5 ta o'yindan kupon tuzing
📸 Rasm — jadval yoki afisha rasmini yuboring
📋 Tarix — oldingi bashoratlaringizni ko'ring

Sport turini tanlang va boshlaymiz!""",
"post_onboarding": "Tayyor! Nima qilishni xohlaysiz?\n\n⚽ Bashorat uchun «O'yin bashoratlari» tugmasini bosing\n⚡ Kupon uchun «Ekspress» tugmasini bosing\n📸 Yoki jadval rasmini yuboring",
"match_too_far": "Bu o'yin juda uzoqda. Men faqat keyingi 7 kun ichidagi o'yinlar uchun bashorat beraman.",
"ev_menu_expired":        "⚠️ O'yinlar ro'yxati eskirdi. Iltimos, uni qayta oching.",
"ev_provider_unavailable": "⚠️ O'yin ma'lumotlari vaqtincha mavjud emas. Keyinroq urinib ko'ring.",
"ev_no_live":             "Jonli o'yinlar yo'q.",
"ev_no_today":            "Bugun o'yin yo'q.",
"ev_no_tomorrow":         "Ertaga o'yin yo'q.",
"ev_more_leagues":        "ℹ️ Muhimlik bo'yicha dastlabki 15 turnir ko'rsatilmoqda; yana bor.",
"ev_more_matches":        "ℹ️ Eng erta 10 o'yin ko'rsatilmoqda; yana bor.",
"enr_football_unavailable": "ℹ️ Bu o'yin uchun tasdiqlangan futbol ma'lumotlari yo'q — bashorat koeffitsientlarga asoslangan.",
"enr_standings_unavailable": "ℹ️ Turnir jadvali mavjud emas.",
"enr_lineups_unavailable": "ℹ️ Tarkiblar hali ma'lum emas.",
"enr_injuries_unavailable": "ℹ️ Jarohat/diskvalifikatsiya ma'lumotlari mavjud emas.",
"enr_unverified":         "ℹ️ O'yinni tasdiqlab bo'lmadi — faqat koeffitsientlar ko'rsatilmoqda.",
"system_prompt": """Sen sport analitikisisan. Qisqa va aniq yoz. Har qanday sportni tahlil qil.

QOIDALAR:
- Koeffitsientlar berilsa — faqat shularni ishlatish
- Faqat matn va emoji, ** markdown yo'q (garov satri bundan mustasno)
- SHAKL: har bir ishtirokchining formasini FAQAT berilgan haqiqiy ma'lumot asosida yoz; ma'lumot bo'lmasa "ma'lumot mavjud emas" deb yoz — O'YLAB TOPMA
- Haqiqiy ma'lumotlar bo'lsa — o'rtacha gol sonini ko'rsat
- Haqiqiy ma'lumot bo'lmasa, buni rostini yoz ("ma'lumot mavjud emas"); fakt, natija, jarohat yoki tarkib O'YLAB TOPMA
- Butun bashorat — maksimum 12 qator

FORMAT (barcha satrlar MAJBURIY):

🏆 [A] — [B]
📍 [Turnir — o'yin ma'lumotidan ol] | [Sana]

📊 [A]: [shakl, 1 jumla] | ⚽ o'rt. X.X/o'yin (ma'lumot bo'lsa)
📊 [B]: [shakl, 1 jumla] | ⚽ o'rt. X.X/o'yin (ma'lumot bo'lsa)

🎯 [A] — XX% | X.XX
Durrang — XX% | X.XX  (taalluqli bo'lsa)
[B] — XX% | X.XX

⚡ **Stavka: [turi] @ X.XX** — [1 jumla, nima uchun]""",
"live_tip_prompt": "Sen jonli stavkalar analitikisisan. O'yin {match}, {minute} daq, hisob {score}. Voqea: {event}. Eng yaxshi jonli stavkani tavsiya et. Qisqa, max 2 jumla.",
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
"menu_history": "📋 Tarix",
"menu_express": "⚡ Ekspress",
"menu_support": "🆘 Yordam",
"support_text": "🆘 *Yordam*\n\nSavollaringiz yoki muammolaringiz bo'lsa — bizga yozing:",
"support_btn":  "💬 Yordamga yozish",
"bot_updated":  "Bot yangilandi! Yangi menyu faollashtirildi.",

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
"img_too_big":   "الصورة كبيرة جداً (الحد الأقصى 5 ميغابايت). أرسل صورة أصغر.",
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
"menu_forecast": "⚽ توقعات المباريات",
"menu_profile":  "👤 الملف",
"menu_lang":     "تغيير اللغة",
"profile_text":  "👤 الملف الشخصي\n\nالاسم: {name}\nاللغة: {lang}\nالمنطقة الزمنية: {tz}",
"ob_sports":     "ما هي الرياضة المفضلة لديك؟",
"ob_exp":        "ما هي خبرتك في الرهانات؟",
"ob_done":       "جاهز! الرياضة: {sports}. يمكنك طلب التوقعات.",
"welcome_intro": """مرحباً بك في ProqnozAI!

أنا محلل رياضي بالذكاء الاصطناعي. ما أستطيع فعله:

⚽ توقعات المباريات — اختر رياضة، ابحث عن مباراة، احصل على تحليل فوري
⚡ رهان مركب — بناء كوبون من 2–5 مباريات
📸 صورة — أرسل صورة الجدول أو الملصق
📋 السجل — عرض توقعاتك السابقة

اختر رياضتك ولنبدأ!""",
"post_onboarding": "جاهز! ماذا تريد أن تفعل؟\n\n⚽ للتوقع اضغط «توقعات المباريات»\n⚡ للكوبون اضغط «رهان مركب»\n📸 أو أرسل صورة الجدول",
"match_too_far": "هذه المباراة بعيدة جداً. أقدم التوقعات فقط للمباريات خلال الأيام السبعة القادمة.",
"ev_menu_expired":        "⚠️ انتهت صلاحية قائمة المباريات. يرجى إعادة فتحها.",
"ev_provider_unavailable": "⚠️ بيانات المباريات غير متاحة مؤقتاً. يرجى المحاولة لاحقاً.",
"ev_no_live":             "لا توجد مباريات مباشرة.",
"ev_no_today":            "لا توجد مباريات اليوم.",
"ev_no_tomorrow":         "لا توجد مباريات غداً.",
"ev_more_leagues":        "ℹ️ يتم عرض أهم 15 بطولة حسب الأولوية؛ وهناك المزيد.",
"ev_more_matches":        "ℹ️ يتم عرض أقرب 10 مباريات موعداً؛ وهناك المزيد.",
"enr_football_unavailable": "ℹ️ لا تتوفر بيانات كرة قدم موثّقة لهذه المباراة — يعتمد التوقع على الأرباح.",
"enr_standings_unavailable": "ℹ️ جدول الترتيب غير متوفر.",
"enr_lineups_unavailable": "ℹ️ التشكيلات غير متوفرة بعد.",
"enr_injuries_unavailable": "ℹ️ بيانات الإصابات/الإيقافات غير متوفرة.",
"enr_unverified":         "ℹ️ تعذّر التحقق من المباراة — يتم عرض الأرباح فقط.",
"system_prompt": """أنت محلل رياضي. اكتب بإيجاز ووضوح. حلّل أي رياضة.

القواعد:
- إذا وُجدت أرباح — استخدمها فقط
- نص عادي وإيموجي فقط، لا Markdown ** (إلا سطر الرهان)
- الشكل: صف حالة كل مشارك من البيانات الحقيقية المتوفرة فقط؛ إن لم تتوفر بيانات فاكتب "لا تتوفر بيانات" — لا تختلق
- إذا توفرت بيانات حقيقية — أضف متوسط الأهداف
- إن غابت البيانات الحقيقية فاذكر ذلك بصدق ("لا تتوفر بيانات")؛ لا تختلق وقائع أو نتائج أو إصابات أو تشكيلات
- التوقع كله — 12 سطراً كحد أقصى

الصيغة (جميع الأسطر إلزامية):

🏆 [أ] — [ب]
📍 [البطولة — من بيانات المباراة] | [التاريخ]

📊 [أ]: [الشكل، جملة واحدة] | ⚽ متوسط X.X/مباراة (إن توفر)
📊 [ب]: [الشكل، جملة واحدة] | ⚽ متوسط X.X/مباراة (إن توفر)

🎯 [أ] — XX% | X.XX
تعادل — XX% | X.XX  (إن انطبق)
[ب] — XX% | X.XX

⚡ **الرهان: [النوع] @ X.XX** — [جملة واحدة لماذا]""",
"live_tip_prompt": "أنت محلل رهانات مباشرة. المباراة {match}، الدقيقة {minute}، النتيجة {score}. الحدث: {event}. اقترح أفضل رهان مباشر. مختصر، جملتان كحد أقصى.",
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
"menu_history": "📋 السجل",
"menu_express": "⚡ إكسبريس",
"menu_support": "🆘 الدعم",
"support_text": "🆘 *الدعم*\n\nإذا كان لديك أسئلة أو مشاكل — تواصل معنا:",
"support_btn":  "💬 تواصل مع الدعم",
"bot_updated":  "تم تحديث البوت! القائمة الجديدة نشطة الآن.",

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
    "az": [("⚽ Futbol", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 Basketbol", "nba"),
           ("🎾 Tennis", "tennis"), ("🏒 Hokey", "hockey"), ("🏆 Hamısı", "all")],
    "ru": [("⚽ Футбол", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 Баскетбол", "nba"),
           ("🎾 Теннис", "tennis"), ("🏒 Хоккей", "hockey"), ("🏆 Все виды", "all")],
    "en": [("⚽ Football", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 Basketball", "nba"),
           ("🎾 Tennis", "tennis"), ("🏒 Hockey", "hockey"), ("🏆 All sports", "all")],
    "tr": [("⚽ Futbol", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 Basketbol", "nba"),
           ("🎾 Tenis", "tennis"), ("🏒 Hokey", "hockey"), ("🏆 Tümü", "all")],
    "kz": [("⚽ Футбол", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 Баскетбол", "nba"),
           ("🎾 Теннис", "tennis"), ("🏒 Хоккей", "hockey"), ("🏆 Барлығы", "all")],
    "uz": [("⚽ Futbol", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 Basketbol", "nba"),
           ("🎾 Tennis", "tennis"), ("🏒 Xokkey", "hockey"), ("🏆 Barchasi", "all")],
    "ar": [("⚽ كرة القدم", "football"), ("🥊 UFC/MMA", "ufc"), ("🏀 كرة السلة", "nba"),
           ("🎾 تنس", "tennis"), ("🏒 هوكي", "hockey"), ("🏆 جميع الرياضات", "all")],
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
    from db import db_lang
    lang = db_lang(uid)  # already normalized to a supported code
    # Fallback chain: requested language -> ru -> en -> the key itself (never
    # empty), so a missing string degrades to a visible, debuggable token.
    txt = (T.get(lang, {}).get(key)
           or T.get("ru", {}).get(key)
           or T.get("en", {}).get(key)
           or key)
    return txt.format(**kw) if kw else txt

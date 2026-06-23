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
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nSaat qurşağı: {tz}\nCəmi sorğular: {total_req}\n\nİdman: {sports}",
"ob_sports":     "Hansı idman növünü sevirsiniz?",
"ob_exp":        "Mərcdə təcrübəniz nə qədərdir?",
"ob_done":       "Hazırdır! Seçdiyiniz idman: {sports}. Artıq proqnoz ala bilərsiniz.",
"match_too_far": "Bu matç 1 həftədən uzaqdadır. Yalnız növbəti 7 gün ərzindəki matçlar üçün proqnoz verirəm.",
"system_prompt": """Sən idman analitikisən. Qısa və aydın yaz. İstənilən idmanı analiz et.

QAYDALAR:
- Keflər varsa — yalnız onları istifadə et
- Yalnız mətn və emoji, ** markdown yox
- FORMA: hər iştirakçı üçün 1 qısa cümlə. Məlumat yoxdursa (təxmini) qeydi ilə yaz
- Bütün proqnoz — maksimum 10 sətir

FORMAT:

🏆 [A] — [B]
📍 [Turnir] | [Tarix]

📊 [A]: [forma, 1 cümlə]
📊 [B]: [forma, 1 cümlə]

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
"profile_text":  "ПРОФИЛЬ\n\nИмя: {name}\nЯзык: {lang}\nЧасовой пояс: {tz}\nВсего запросов: {total_req}\n\nСпорт: {sports}",
"ob_sports":     "Какой вид спорта вас интересует больше всего?",
"ob_exp":        "Каков ваш опыт в ставках?",
"ob_done":       "Готово! Выбранный спорт: {sports}. Можете запрашивать прогнозы.",
"match_too_far": "Этот матч слишком далеко. Я даю прогнозы только на матчи в ближайшие 7 дней.",
"system_prompt": """Ты — спортивный аналитик. Пиши коротко и по делу. Анализируй любой спорт.

ПРАВИЛА:
- Если есть коэффициенты — используй только их
- Только текст и emoji, без ** markdown (кроме строки ставки)
- ФОРМА: 1 короткое предложение на каждого участника. Если нет данных — пиши оценку с (оценочно)
- Весь прогноз — максимум 10 строк

ФОРМАТ:

🏆 [А] — [Б]
📍 [Турнир] | [Дата]

📊 [А]: [форма, 1 предложение]
📊 [Б]: [форма, 1 предложение]

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
"profile_text":  "PROFILE\n\nName: {name}\nLanguage: {lang}\nTimezone: {tz}\nTotal requests: {total_req}\n\nSports: {sports}",
"ob_sports":     "Which sport interests you the most?",
"ob_exp":        "What is your betting experience?",
"ob_done":       "Done! Your sport: {sports}. You can now request forecasts.",
"match_too_far": "This match is too far ahead. I only give forecasts for matches within the next 7 days.",
"system_prompt": """You are a sports analyst. Be brief and direct. Analyse any sport.

RULES:
- If odds are provided — use only those
- Plain text and emoji only, no ** markdown
- FORM: 1 short sentence per participant. If unknown, write estimate with (est.)
- Entire forecast — 10 lines max

FORMAT:

🏆 [A] — [B]
📍 [Tournament] | [Date]

📊 [A]: [form, 1 sentence]
📊 [B]: [form, 1 sentence]

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
"profile_text":  "PROFİL\n\nAd: {name}\nDil: {lang}\nSaat dilimi: {tz}\nToplam istek: {total_req}\n\nSpor: {sports}",
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
"system_prompt": """Sen spor analistisin. Kısa ve net yaz. Her sporu analiz et.

KURALLAR:
- Oran varsa — sadece onları kullan
- Sadece metin ve emoji, ** markdown yok
- FORM: her katılımcı için 1 kısa cümle. Bilinmiyorsa (tahmini) etiketiyle yaz
- Tüm tahmin — maksimum 10 satır

FORMAT:

🏆 [A] — [B]
📍 [Turnuva] | [Tarih]

📊 [A]: [form, 1 cümle]
📊 [B]: [form, 1 cümle]

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
"profile_text":  "ПРОФИЛЬ\n\nАты: {name}\nТіл: {lang}\nУақыт белдеуі: {tz}\nЖалпы сұраныстар: {total_req}\n\nСпорт: {sports}",
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
"system_prompt": """Сен спорт аналитикісің. Қысқа және нақты жаз. Кез келген спортты талда.

ЕРЕЖЕЛЕР:
- Коэффициенттер берілсе — тек соларды қолдан
- Тек мәтін және emoji, ** markdown жоқ
- ФОРМА: әр қатысушы үшін 1 қысқа сөйлем. Белгісіз болса (бағалау) белгісімен жаз
- Бүкіл болжам — максимум 10 жол

ФОРМАТ:

🏆 [А] — [Б]
📍 [Турнир] | [Күні]

📊 [А]: [форма, 1 сөйлем]
📊 [Б]: [форма, 1 сөйлем]

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
"profile_text":  "PROFIL\n\nIsm: {name}\nTil: {lang}\nVaqt mintaqasi: {tz}\nJami so'rovlar: {total_req}\n\nSport: {sports}",
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
"system_prompt": """Sen sport analitikisisan. Qisqa va aniq yoz. Har qanday sportni tahlil qil.

QOIDALAR:
- Koeffitsientlar berilsa — faqat shularni ishlatish
- Faqat matn va emoji, ** markdown yo'q
- SHAKL: har bir ishtirokchi uchun 1 qisqa jumla. Noma'lum bo'lsa (taxminiy) belgisi bilan yoz
- Butun bashorat — maksimum 10 qator

FORMAT:

🏆 [A] — [B]
📍 [Turnir] | [Sana]

📊 [A]: [shakl, 1 jumla]
📊 [B]: [shakl, 1 jumla]

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
"profile_text":  "الملف الشخصي\n\nالاسم: {name}\nاللغة: {lang}\nالمنطقة الزمنية: {tz}\nإجمالي الطلبات: {total_req}\n\nالرياضة: {sports}",
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
"system_prompt": """أنت محلل رياضي. اكتب بإيجاز ووضوح. حلّل أي رياضة.

القواعد:
- إذا وُجدت أرباح — استخدمها فقط
- نص عادي وإيموجي فقط، لا Markdown **
- الشكل: جملة واحدة قصيرة لكل مشارك. إذا كانت غير مؤكدة اكتب (تقديري)
- التوقع كله — 10 أسطر كحد أقصى

الصيغة:

🏆 [أ] — [ب]
📍 [البطولة] | [التاريخ]

📊 [أ]: [الشكل، جملة واحدة]
📊 [ب]: [الشكل، جملة واحدة]

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
    lang = db_lang(uid)
    # Fallback chain: current lang -> ru -> en -> empty string
    txt = T.get(lang, {}).get(key) or T.get("ru", {}).get(key) or T.get("en", {}).get(key, "")
    return txt.format(**kw) if kw else txt

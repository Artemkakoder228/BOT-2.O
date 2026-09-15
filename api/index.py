import os
import re
import requests
import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
TEACHER_SECRET_CODE = os.environ.get("TEACHER_SECRET_CODE", "1234")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

def get_db():
    return psycopg2.connect(DATABASE_URL)

def send_msg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

# Клавіатури
def kb_start():
    return {"keyboard": [[{"text": "🔑 Вхід"}]], "resize_keyboard": True}

def kb_student():
    return {
        "keyboard": [
            [{"text": "🏫 Прибув до школи"}, {"text": "🏠 Вдома"}],
            [{"text": "🚪 Вийти з акаунту"}]
        ],
        "resize_keyboard": True
    }

def kb_teacher():
    return {
        "keyboard": [
            [{"text": "📋 Візити сьогодні"}, {"text": "❌ Хто сьогодні відсутній"}],
            [{"text": "📊 Статистика за місяць"}],
            [{"text": "🚪 Вийти з акаунту"}]
        ],
        "resize_keyboard": True
    }

def kb_back():
    return {"keyboard": [[{"text": "⬅️ Назад"}]], "resize_keyboard": True}

def get_user(chat_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, last_name, role, is_verified, state FROM users_whitelist WHERE telegram_id = %s;", (chat_id,))
            return cur.fetchone()

# Роут для щоденних нагадувань (Vercel Cron)
@app.route('/api/remind', methods=['GET'])
def send_reminders():
    # Надсилаємо нагадування лише учням, які авторизовані, але ще не відмітилися сьогодні
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.telegram_id, u.last_name 
                FROM users_whitelist u
                LEFT JOIN attendance a ON u.id = a.user_id AND a.date = CURRENT_DATE
                WHERE u.role = 'student' 
                  AND u.telegram_id IS NOT NULL 
                  AND u.is_verified = TRUE 
                  AND a.id IS NULL;
            """)
            students = cur.fetchall()

    count = 0
    for tg_id, name in students:
        send_msg(tg_id, f"⏰ Привіт, {name}! Не забудь відмітити свій статус на сьогодні:", kb_student())
        count += 1

    return jsonify({"status": "ok", "reminded_count": count}), 200

# Головний обробник повідомлень
@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def webhook(path):
    if request.method != 'POST':
        return "Bot is running", 200

    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"status": "ok"}), 200

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    user = get_user(chat_id)

    # 1. СТАРТ / НАЗАД
    if text == "/start" or text == "⬅️ Назад":
        if user and user[3]:  # is_verified == True
            role = user[2]
            if role == "teacher":
                send_msg(chat_id, f"Вітаємо, {user[1]}! Меню вчителя:", kb_teacher())
            else:
                send_msg(chat_id, f"Привіт, {user[1]}! Обери статус:", kb_student())
        else:
            # Скидаємо стан якщо не завершив вхід
            if user:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users_whitelist SET state = NULL WHERE telegram_id = %s;", (chat_id,))
                        conn.commit()
            send_msg(chat_id, "Вітаємо у системі обліку відвідуваності!\nНатисніть кнопку нижче для входу:", kb_start())
        return jsonify({"status": "ok"}), 200

    # 2. КНОПКА "ВХІД"
    if text == "🔑 Вхід":
        send_msg(chat_id, "Введіть вашу електронну пошту зі списку:", kb_back())
        return jsonify({"status": "ok"}), 200

    # 3. ВИХІД З АКАУНТУ
    if text == "🚪 Вийти з акаунту":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users_whitelist SET telegram_id = NULL, is_verified = FALSE, state = NULL WHERE telegram_id = %s;", (chat_id,))
                conn.commit()
        send_msg(chat_id, "Ви вийшли з облікового запису.", kb_start())
        return jsonify({"status": "ok"}), 200

    # 4. ПЕРЕВІРКА ПАРОЛЯ ДЛЯ ВЧИТЕЛЯ (якщо користувач у стані очікування коду)
    if user and user[4] == "awaiting_teacher_code":
        if text == TEACHER_SECRET_CODE:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users_whitelist SET is_verified = TRUE, state = NULL WHERE telegram_id = %s;", (chat_id,))
                    conn.commit()
            send_msg(chat_id, f"✅ Пароль вірний! Вітаємо, {user[1]}.\nВам надано доступ викладача.", kb_teacher())
        else:
            send_msg(chat_id, "❌ Невірний секретний код! Спробуйте ще раз або натисніть «⬅️ Назад».", kb_back())
        return jsonify({"status": "ok"}), 200

    # 5. ДІЇ АВТОРИЗОВАНОГО КОРИСТУВАЧА
    if user and user[3]:
        user_id, last_name, role, _, _ = user

        # --- Для учня ---
        if role == "student":
            if text in ["🏫 Прибув до школи", "🏠 Вдома"]:
                st = "at_school" if text == "🏫 Прибув до школи" else "at_home"
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO attendance (user_id, status, date)
                            VALUES (%s, %s, CURRENT_DATE)
                            ON CONFLICT (user_id, date)
                            DO UPDATE SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP;
                        """, (user_id, st))
                        conn.commit()
                msg_txt = "🏫 Чудово! Твій статус збережено: «У школі»." if st == "at_school" else "🏠 Статус збережено: «Вдома»."
                send_msg(chat_id, msg_txt, kb_student())
                return jsonify({"status": "ok"}), 200

        # --- Для вчителя ---
        elif role == "teacher":
            if text == "📋 Візити сьогодні":
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT u.last_name, COALESCE(a.status, 'none')
                            FROM users_whitelist u
                            LEFT JOIN attendance a ON u.id = a.user_id AND a.date = CURRENT_DATE
                            WHERE u.role = 'student'
                            ORDER BY u.last_name;
                        """)
                        rows = cur.fetchall()
                icons = {"at_school": "🏫 У школі", "at_home": "🏠 Вдома", "none": "⏳ Не відмітився"}
                res = "📋 Статус учнів на сьогодні:\n\n"
                for name, st in rows:
                    res += f"• {name}: {icons.get(st, st)}\n"
                send_msg(chat_id, res, kb_teacher())
                return jsonify({"status": "ok"}), 200

            elif text == "❌ Хто сьогодні відсутній":
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT u.last_name, COALESCE(a.status, 'none')
                            FROM users_whitelist u
                            LEFT JOIN attendance a ON u.id = a.user_id AND a.date = CURRENT_DATE
                            WHERE u.role = 'student' AND (a.status = 'at_home' OR a.status IS NULL)
                            ORDER BY u.last_name;
                        """)
                        rows = cur.fetchall()
                if not rows:
                    res = "🎉 Усі учні сьогодні присутні у школі!"
                else:
                    res = "❌ Відсутні сьогодні:\n\n"
                    for name, st in rows:
                        reason = "🏠 Вдома" if st == "at_home" else "⏳ Не відмітився"
                        res += f"• {name} — {reason}\n"
                send_msg(chat_id, res, kb_teacher())
                return jsonify({"status": "ok"}), 200

            elif text == "📊 Статистика за місяць":
                # Рахуємо дні відсутності (статус 'at_home') за останні 30 днів
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT u.last_name, 
                                   COUNT(CASE WHEN a.status = 'at_home' THEN 1 END) AS home_count,
                                   COUNT(CASE WHEN a.status = 'at_school' THEN 1 END) AS school_count
                            FROM users_whitelist u
                            LEFT JOIN attendance a ON u.id = a.user_id AND a.date >= CURRENT_DATE - INTERVAL '30 days'
                            WHERE u.role = 'student'
                            GROUP BY u.last_name
                            ORDER BY home_count DESC, u.last_name;
                        """)
                        rows = cur.fetchall()
                res = "📊 Звіт відвідуваності за останні 30 днів:\n(Прізвище: пропусків / відвідано)\n\n"
                for name, h_count, s_count in rows:
                    res += f"• {name}: 🏠 {h_count} дн. пропусків | 🏫 {s_count} дн. у школі\n"
                send_msg(chat_id, res, kb_teacher())
                return jsonify({"status": "ok"}), 200

    # 6. АВТОРИЗАЦІЯ ЗА EMAIL
    if re.match(r"[^@]+@[^@]+\.[^@]+", text.lower()):
        email = text.lower()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, last_name, role, telegram_id FROM users_whitelist WHERE email = %s;", (email,))
                row = cur.fetchone()

                if not row:
                    send_msg(chat_id, "❌ Пошти немає у списках. Зверніться до класного керівника.", kb_back())
                    return jsonify({"status": "ok"}), 200

                target_id, last_name, role, existing_tg = row
                if existing_tg and existing_tg != chat_id:
                    send_msg(chat_id, "⚠️ Ця пошта вже прив'язана до іншого акаунта Telegram.", kb_back())
                    return jsonify({"status": "ok"}), 200

                if role == "teacher":
                    # Вчителю ставимо стан очікування коду
                    cur.execute("""
                        UPDATE users_whitelist 
                        SET telegram_id = %s, is_verified = FALSE, state = 'awaiting_teacher_code' 
                        WHERE id = %s;
                    """, (chat_id, target_id))
                    conn.commit()
                    send_msg(chat_id, f"Впізнано викладача: {last_name}.\n🔒 Для завершення входу введіть секретний код доступу:", kb_back())
                else:
                    # Учень одразу верифікується
                    cur.execute("""
                        UPDATE users_whitelist 
                        SET telegram_id = %s, is_verified = TRUE, state = NULL 
                        WHERE id = %s;
                    """, (chat_id, target_id))
                    conn.commit()
                    send_msg(chat_id, f"👋 Привіт, {last_name}!\nТи успішно увійшов як учень. Обери свій статус:", kb_student())
        return jsonify({"status": "ok"}), 200

    send_msg(chat_id, "Команду не розпізнано. Скористайтеся кнопками.")
    return jsonify({"status": "ok"}), 200
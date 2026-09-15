import os
import re
import requests
import psycopg2
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
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
            [{"text": "📋 Показати всі візити"}, {"text": "❌ Хто відсутній"}],
            [{"text": "🚪 Вийти з акаунту"}]
        ],
        "resize_keyboard": True
    }

def kb_back():
    return {"keyboard": [[{"text": "⬅️ Назад"}]], "resize_keyboard": True}

# Отримання даних користувача за chat_id
def get_user(chat_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, last_name, role, is_verified FROM users_whitelist WHERE telegram_id = %s;", (chat_id,))
            return cur.fetchone()

@app.route('/', defaults={'path': ''}, methods=['GET', 'POST'])
@app.route('/<path:path>', methods=['GET', 'POST'])
def webhook(path):
    if request.method != 'POST':
        return "Bot active", 200

    data = request.get_json()
    if not data or "message" not in data:
        return jsonify({"status": "ok"}), 200

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    user = get_user(chat_id)

    # 1. СТАРТ / НАЗАД
    if text == "/start" or text == "⬅️ Назад":
        if user and user[3]:  # Вже авторизований
            role = user[2]
            if role == "teacher":
                send_msg(chat_id, f"Вітаємо, {user[1]}! Головне меню вчителя:", kb_teacher())
            else:
                send_msg(chat_id, f"Привіт, {user[1]}! Обери свій статус:", kb_student())
        else:
            send_msg(chat_id, "Вітаємо у системі контролю відвідуваності!\nНатисніть кнопку нижче, щоб увійти.", kb_start())
        return jsonify({"status": "ok"}), 200

    # 2. КНОПКА "ВХІД"
    if text == "🔑 Вхід":
        send_msg(chat_id, "Введіть вашу електронну пошту для авторизації:", kb_back())
        return jsonify({"status": "ok"}), 200

    # 3. ВИХІД З АКАУНТУ
    if text == "🚪 Вийти з акаунту":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users_whitelist SET telegram_id = NULL, is_verified = FALSE WHERE telegram_id = %s;", (chat_id,))
                conn.commit()
        send_msg(chat_id, "Ви вийшли з облікового запису.", kb_start())
        return jsonify({"status": "ok"}), 200

    # 4. ОБРОБКА ДІЙ АВТОРИЗОВАНОГО КОРИСТУВАЧА
    if user and user[3]:
        user_id, last_name, role, _ = user

        # Логіка учня
        if role == "student":
            if text in ["🏫 Прибув до школи", "🏠 Вдома"]:
                status = "at_school" if text == "🏫 Прибув до школи" else "at_home"
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO attendance (user_id, status, date)
                            VALUES (%s, %s, CURRENT_DATE)
                            ON CONFLICT (user_id, date) 
                            DO UPDATE SET status = EXCLUDED.status, updated_at = CURRENT_TIMESTAMP;
                        """, (user_id, status))
                        conn.commit()
                response = "✅ Статус успішно оновлено: Ви в школі!" if status == "at_school" else "🏠 Статус оновлено: Залишилися вдома."
                send_msg(chat_id, response, kb_student())
                return jsonify({"status": "ok"}), 200

        # Логіка вчителя
        elif role == "teacher":
            if text == "📋 Показати всі візити":
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
                report = "📋 Статус учнів на сьогодні:\n\n"
                icons = {"at_school": "🏫 У школі", "at_home": "🏠 Вдома", "none": "⏳ Не відмітився"}
                for name, st in rows:
                    report += f"• {name}: {icons.get(st, st)}\n"
                send_msg(chat_id, report, kb_teacher())
                return jsonify({"status": "ok"}), 200

            elif text == "❌ Хто відсутній":
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
                    report = "🎉 Усі учні сьогодні присутні в школі!"
                else:
                    report = "❌ Відсутні учні (або не відмітились):\n\n"
                    for name, st in rows:
                        reason = "🏠 Вдома" if st == "at_home" else "⏳ Немає відмітки"
                        report += f"• {name} ({reason})\n"
                send_msg(chat_id, report, kb_teacher())
                return jsonify({"status": "ok"}), 200

    # 5. АВТОРИЗАЦІЯ ЗА EMAIL (якщо користувач не авторизований)
    if re.match(r"[^@]+@[^@]+\.[^@]+", text.lower()):
        email = text.lower()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, last_name, role, telegram_id FROM users_whitelist WHERE email = %s;", (email,))
                row = cur.fetchone()

                if not row:
                    send_msg(chat_id, "❌ Пошти немає у списках доступу. Перевірте правильність введення або напишіть вчителю.", kb_back())
                    return jsonify({"status": "ok"}), 200

                target_id, last_name, role, existing_tg = row
                if existing_tg and existing_tg != chat_id:
                    send_msg(chat_id, "⚠️ Ця пошта вже прив'язана до іншого акаунта Telegram.", kb_back())
                    return jsonify({"status": "ok"}), 200

                cur.execute("UPDATE users_whitelist SET telegram_id = %s, is_verified = TRUE WHERE id = %s;", (chat_id, target_id))
                conn.commit()

        if role == "teacher":
            send_msg(chat_id, f"👋 Вітаємо, {last_name}!\nВи авторизовані як вчитель.", kb_teacher())
        else:
            send_msg(chat_id, f"👋 Привіт, {last_name}!\nТи успішно увійшов як учень. Обери свій статус:", kb_student())
        return jsonify({"status": "ok"}), 200

    send_msg(chat_id, "Команда не розпізнана. Скористайтеся кнопками меню.")
    return jsonify({"status": "ok"}), 200
from fastapi import FastAPI, Request
import sqlite3
from datetime import datetime
import random

app = FastAPI()

DB_PATH = "users.db"

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
               kakao_user_id TEXT PRIMARY KEY,
               level INTEGER NOT NULL DEFAULT 1,
               gold INTEGER NOT NULL DEFAULT 100,
               weapon_level INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL, 
               pending TEXT
        )   
    """)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN pending TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

def get_or_create_user(kakao_user_id: str):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE kakao_user_id = ?", (kakao_user_id,))
    row = cur.fetchone()

    if row is None:
        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT INTO users (kakao_user_id, level, gold, weapon_level, created_at) VALUES (?, 1, 100, 0, ?)",
            (kakao_user_id, now)
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE kakao_user_id = ?", (kakao_user_id,))
        row = cur.fetchone()

    conn.close()
    return row

def update_user(kakao_user_id: str, level: int, gold: int, weapon_level: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET level = ?, gold = ?, weapon_level = ? WHERE kakao_user_id = ?",
        (level, gold, weapon_level, kakao_user_id)
    )
    conn.commit()
    conn.close()

def set_pending(kakao_user_id: str, pending: str | None):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET pending = ? WHERE kakao_user_id = ?",
        (pending, kakao_user_id)
    )
    conn.commit()
    conn.close()

def kakao_text_response(text: str):
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {"simpleText": {"text": text}}
            ]
        }
    }

def kakao_text_with_quick_replies(text: str, replies: list[tuple[str, str]]):
    """
    replies: [(버튼표시label, 눌렀을 때 채팅창에 입력될 messageText), ...]
    """
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text":text}}],
            "quickReplies": [
                {"label": label, "action": "message", "messageText": message_text}
                for (label, message_text) in replies
            ]
        }
    }

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/")
def root():
    return {"ok": True, "service": "kakao-idlebot"}

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()

    kakao_user_id = body["userRequest"]["user"]["id"]
    msg = body["userRequest"]["utterance"].strip()

    user = get_or_create_user(kakao_user_id)
    level = int(user["level"])
    gold = int(user["gold"])
    weapon_level = int(user["weapon_level"])
    pending = user["pending"]

    # 0) 도움말
    if msg in ["/도움", "도움", "help", "/help"]:
        return kakao_text_response(
            "명령어:\n"
            "- /내정보\n"
            "- /모험\n"
            "- /강화\n"
            "- /도움"
        )
    
    if pending == "BATTLE_SELECT":
        if msg.startswith("모험 "):
            difficulty = msg.split(" ", 1)[1].strip()
            set_pending(kakao_user_id, None)

            if difficulty == "쉬움":
                gain = 10 + weapon_level * 1
            elif difficulty == "보통":
                gain = 20 + weapon_level * 2
            elif difficulty == "어려움":
                gain = 35 + weapon_level * 3
            else:
                return kakao_text_response("난이도 선택이 이상해. 다시 '모험'를 입력해줘.")

            gold += gain

            new_level = 1 + (gold // 200)
            if new_level > level:
                level = new_level

            update_user(kakao_user_id, level, gold, weapon_level)
            return kakao_text_response(
                f"⚔️ {difficulty} 모험 성공!\n"
                f"+{gain} 골드\n"
                f"현재 골드: {gold}"
            )

        # 난이도 대기 중 엉뚱한 입력을 하면 안내 + 버튼 다시 제공
        return kakao_text_with_quick_replies(
            "난이도를 버튼으로 선택해주세요.",
            [("쉬움", "모험 쉬움"), ("보통", "모험 보통"), ("어려움", "모험 어려움")]
        )

    if msg in ["/내정보", "내정보", "/me"]:
        return kakao_text_response(
            f"📌 내정보\n"
            f"레벨: {level}\n"
            f"골드: {gold}\n"
            f"무기강화: +{weapon_level}"
        )

    if msg in ["/모험", "모험"]:
        set_pending(kakao_user_id, "BATTLE_SELECT")
        return kakao_text_with_quick_replies(
            "난이도를 선택해주세요.",
            [("쉬움", "모험 쉬움"), ("보통", "모험 보통"), ("어려움", "모험 어려움")]
        )

    if msg in ["/강화", "강화"]:
        cost = 50 + weapon_level * 25
        if gold < cost:
            return kakao_text_response(f"💸 골드 부족!\n강화 비용: {cost}\n현재 골드: {gold}")

        # 확률(데모): 강화가 올라갈수록 성공률 하락
        # 성공률 = max(10, 70 - weapon_level*10)
        import random
        success_rate = max(10, 70 - weapon_level * 10)
        roll = random.randint(1, 100)

        gold -= cost
        if roll <= success_rate:
            weapon_level += 1
            update_user(kakao_user_id, level, gold, weapon_level)
            return kakao_text_response(
                f"✨ 강화 성공! (+{weapon_level})\n"
                f"(성공률 {success_rate}%, 비용 {cost})\n"
                f"남은 골드: {gold}"
            )
        else:
            update_user(kakao_user_id, level, gold, weapon_level)
            return kakao_text_response(
                f"💥 강화 실패…\n"
                f"(성공률 {success_rate}%, 비용 {cost})\n"
                f"남은 골드: {gold}"
            )

    # 기본 응답
    return kakao_text_response("모르는 명령어야. /도움 을 입력해봐.")
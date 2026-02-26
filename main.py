from fastapi import FastAPI, Request
import sqlite3
from datetime import datetime, timezone, timedelta
import random

app = FastAPI()
DB_PATH = "users.db"

# ====== Time helpers (KST) ======
KST = timezone(timedelta(hours=9))

def now_kst_iso():
    return datetime.now(KST).isoformat()

def today_kst_str():
    return datetime.now(KST).date().isoformat()


# ====== DB ======
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl_sql: str):
    """
    decl_sql example: "ALTER TABLE users ADD COLUMN job TEXT NOT NULL DEFAULT 'WARRIOR'"
    """
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}  # (cid, name, type, notnull, dflt_value, pk)
    if col not in cols:
        cur.execute(decl_sql)

def init_db():
    conn = db_connect()
    cur = conn.cursor()

    # Base table (최초 생성용)
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

    # 신규 컬럼들 (기존 DB에도 안전하게 추가)
    _ensure_column(conn, "users", "job",
                    "ALTER TABLE users ADD COLUMN job TEXT")
    _ensure_column(conn, "users", "stat_points",
                   "ALTER TABLE users ADD COLUMN stat_points INTEGER NOT NULL DEFAULT 0")

    # Stats: HP(<=999), ATK/INT/SPD(<=99), LUK(<=999)
    _ensure_column(conn, "users", "hp",
                   "ALTER TABLE users ADD COLUMN hp INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "users", "atk",
                   "ALTER TABLE users ADD COLUMN atk INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "users", "int_stat",
                   "ALTER TABLE users ADD COLUMN int_stat INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "users", "spd",
                   "ALTER TABLE users ADD COLUMN spd INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "users", "luk",
                   "ALTER TABLE users ADD COLUMN luk INTEGER NOT NULL DEFAULT 1")

    # Fatigue
    _ensure_column(conn, "users", "fatigue",
                   "ALTER TABLE users ADD COLUMN fatigue INTEGER NOT NULL DEFAULT 0")

    # Attendance (KST date string)
    _ensure_column(conn, "users", "last_attendance",
                   "ALTER TABLE users ADD COLUMN last_attendance TEXT")

    conn.commit()
    conn.close()

def get_or_create_user(kakao_user_id: str):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE kakao_user_id = ?", (kakao_user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO users (
                kakao_user_id, level, gold, weapon_level, created_at, pending,
                job, stat_points, hp, atk, int_stat, spd, luk, fatigue, last_attendance
            ) VALUES (?, 1, 100, 0, ?, NULL, NULL, 0, 1, 1, 1, 1, 1, 0, NULL)
            """,
            (kakao_user_id, now_kst_iso())
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE kakao_user_id = ?", (kakao_user_id,))
        row = cur.fetchone()

    conn.close()
    return row

def update_user_fields(kakao_user_id: str, **fields):
    """
    Example: update_user_fields(id, level=2, gold=150, job='MAGE')
    """
    if not fields:
        return
    keys = list(fields.keys())
    values = [fields[k] for k in keys]
    set_clause = ", ".join([f"{k} = ?" for k in keys])
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(f"UPDATE users SET {set_clause} WHERE kakao_user_id = ?", (*values, kakao_user_id))
    conn.commit()
    conn.close()

def set_pending(kakao_user_id: str, pending: str | None):
    update_user_fields(kakao_user_id, pending=pending)


# ====== Kakao responses ======
def kakao_text_response(text: str):
    return {
        "version": "2.0",
        "template": {"outputs": [{"simpleText": {"text": text}}]}
    }

def kakao_text_with_quick_replies(text: str, replies: list[tuple[str, str]]):
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}],
            "quickReplies": [
                {"label": label, "action": "message", "messageText": message_text}
                for (label, message_text) in replies
            ],
        },
    }


# ====== Game logic ======
HP_CAP = 999
ATK_CAP = 99
INT_CAP = 99
SPD_CAP = 99
LUK_CAP = 999
LEVEL_CAP = 99

def clamp(x: int, lo: int, hi: int) -> int:
    return lo if x < lo else hi if x > hi else x

def job_main_stat(job: str) -> str:
    # job: WARRIOR / MAGE / NINJA
    return {"WARRIOR": "atk", "MAGE": "int_stat", "NINJA": "spd"}.get(job, "atk")

def combat_power(user_row: sqlite3.Row) -> int:
    """
    스펙 3.4 해석:
    전투력 = HP + 주스탯*3 + (부스탯 + 부스탯)
    여기서 '부스탯 2개'는 LUK 제외, 나머지 2개(ATK/INT/SPD 중 주스탯 제외)를 더하는 방식으로 고정.
    """
    hp = int(user_row["hp"])
    atk = int(user_row["atk"])
    it = int(user_row["int_stat"])
    spd = int(user_row["spd"])
    job = user_row["job"]
    main = job_main_stat(job)

    if main == "atk":
        main_v = atk
        sub1, sub2 = it, spd
    elif main == "int_stat":
        main_v = it
        sub1, sub2 = atk, spd
    else:  # spd
        main_v = spd
        sub1, sub2 = atk, it

    return hp + main_v * 3 + sub1 + sub2

def level_up_rolls(difficulty: str, luk: int) -> int:
    """
    4.2:
      - 쉬움: 30%로 +1
      - 보통: 40%로 +1, 10%로 +2 (중복 상승 불가)
      - 어려움: 70%로 +1, 30%로 +2 (중복 상승 불가)
    LUK 보정: 각 상승 확률에 (LUK / 10000) 만큼 더함.
    """
    bonus = luk / 10000.0  # e.g. luk=100 -> +1%
    r = random.random()

    if difficulty == "쉬움":
        p1 = 0.30 + bonus
        return 1 if r < p1 else 0

    if difficulty == "보통":
        p2 = 0.10 + bonus
        p1 = 0.40 + bonus
        # 중복 불가 -> 우선 +2 판정 후, 실패면 +1 판정
        if r < p2:
            return 2
        return 1 if r < (p2 + p1) else 0

    if difficulty == "어려움":
        p2 = 0.30 + bonus
        p1 = 0.70 + bonus
        if r < p2:
            return 2
        return 1 if r < (p2 + p1) else 0

    return 0

def fatigue_cost(difficulty: str) -> int:
    return {"쉬움": 1, "보통": 2, "어려움": 3}.get(difficulty, 999)


# ====== FastAPI ======
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

    # Load user state
    level = int(user["level"])
    gold = int(user["gold"])
    weapon_level = int(user["weapon_level"])
    pending = user["pending"]
    job = user["job"]
    stat_points = int(user["stat_points"])
    hp = int(user["hp"])
    atk = int(user["atk"])
    int_stat = int(user["int_stat"])
    spd = int(user["spd"])
    luk = int(user["luk"])
    fatigue = int(user["fatigue"])
    last_att = user["last_attendance"]

    # ====== 0) 도움말 ======
    if msg in ["/도움", "도움", "help", "/help"]:
        return kakao_text_response(
            "명령어:\n"
            "- /내정보\n"
            "- /직업\n"
            "- /모험\n"
            "- 스탯 사용\n"
            "- /스탯\n"
            "- /출석\n"
            "- /강화\n"
            "- /도움"
        )

    # ====== Pending: JOB SELECT ======
    if pending == "JOB_SELECT":

        # 이미 직업이 설정되어 있으면 차단
        if job is not None:
            set_pending(kakao_user_id, None)
            return kakao_text_response("❌ 현재는 직업 변경이 불가능합니다.")

        if msg.startswith("직업 "):
            choice = msg.split(" ", 1)[1].strip()
            mapping = {"전사": "WARRIOR", "마법사": "MAGE", "닌자": "NINJA"}

            if choice not in mapping:
                return kakao_text_with_quick_replies(
                    "직업 선택이 이상해. 버튼으로 골라줘.",
                    [("전사", "직업 전사"), ("마법사", "직업 마법사"), ("닌자", "직업 닌자")]
                )

            selected_job = mapping[choice]
            set_pending(kakao_user_id, None)
            update_user_fields(kakao_user_id, job=selected_job)

            return kakao_text_response(f"✅ 직업이 {choice}로 설정됐어.")

        return kakao_text_with_quick_replies(
            "직업을 버튼으로 선택해주세요.",
            [("전사", "직업 전사"), ("마법사", "직업 마법사"), ("닌자", "직업 닌자")]
        )

    # ====== Pending: STAT ALLOC ======
    if pending == "STAT_ALLOC":
        # 입력 형식: "HP 5" / "ATK 3" / "INT 2" / "SPD 1" / "LUK 4"
        parts = msg.upper().split()
        if len(parts) == 2 and parts[0] in ["HP", "ATK", "INT", "SPD", "LUK"]:
            try:
                amount = int(parts[1])
            except ValueError:
                amount = -1

            if amount <= 0:
                return kakao_text_response("숫자는 1 이상으로 입력해줘. 예: HP 5")

            if amount > stat_points:
                return kakao_text_response(f"스탯 포인트가 부족해. (보유: {stat_points})")

            # apply with caps
            if parts[0] == "HP":
                new_hp = clamp(hp + amount, 1, HP_CAP)
                used = new_hp - hp
                hp = new_hp
                update_user_fields(kakao_user_id, hp=hp, stat_points=stat_points - used)
                set_pending(kakao_user_id, None)
                return kakao_text_response(f"✅ HP +{used} (현재 HP {hp})\n남은 포인트: {stat_points - used}")

            if parts[0] == "ATK":
                new_atk = clamp(atk + amount, 1, ATK_CAP)
                used = new_atk - atk
                atk = new_atk
                update_user_fields(kakao_user_id, atk=atk, stat_points=stat_points - used)
                set_pending(kakao_user_id, None)
                return kakao_text_response(f"✅ ATK +{used} (현재 ATK {atk})\n남은 포인트: {stat_points - used}")

            if parts[0] == "INT":
                new_int = clamp(int_stat + amount, 1, INT_CAP)
                used = new_int - int_stat
                int_stat = new_int
                update_user_fields(kakao_user_id, int_stat=int_stat, stat_points=stat_points - used)
                set_pending(kakao_user_id, None)
                return kakao_text_response(f"✅ INT +{used} (현재 INT {int_stat})\n남은 포인트: {stat_points - used}")

            if parts[0] == "SPD":
                new_spd = clamp(spd + amount, 1, SPD_CAP)
                used = new_spd - spd
                spd = new_spd
                update_user_fields(kakao_user_id, spd=spd, stat_points=stat_points - used)
                set_pending(kakao_user_id, None)
                return kakao_text_response(f"✅ SPD +{used} (현재 SPD {spd})\n남은 포인트: {stat_points - used}")

            if parts[0] == "LUK":
                new_luk = clamp(luk + amount, 1, LUK_CAP)
                used = new_luk - luk
                luk = new_luk
                update_user_fields(kakao_user_id, luk=luk, stat_points=stat_points - used)
                set_pending(kakao_user_id, None)
                return kakao_text_response(f"✅ LUK +{used} (현재 LUK {luk})\n남은 포인트: {stat_points - used}")

        # 안내
        return kakao_text_response(
            "스탯 사용 형식이 이상해.\n"
            "예시: HP 5 / ATK 3 / INT 2 / SPD 1 / LUK 4"
        )

    # ====== Pending: ADVENTURE SELECT ======
    if pending == "ADVENTURE_SELECT":
        if msg.startswith("모험 "):
            difficulty = msg.split(" ", 1)[1].strip()
            if difficulty not in ["쉬움", "보통", "어려움"]:
                return kakao_text_with_quick_replies(
                    "난이도를 버튼으로 선택해주세요.",
                    [("쉬움", "모험 쉬움"), ("보통", "모험 보통"), ("어려움", "모험 어려움")]
                )

            cost = fatigue_cost(difficulty)
            if fatigue < cost:
                return kakao_text_response(f"😵 피로도 부족!\n필요: {cost}\n현재: {fatigue}")

            # fatigue spend
            fatigue -= cost

            # 레벨 상승 판정
            inc = level_up_rolls(difficulty, luk)
            if inc > 0:
                # 레벨 상한 적용
                real_inc = min(inc, LEVEL_CAP - level)
                level += real_inc

                # 스탯 포인트: 레벨 1 오를 때마다 1~10 랜덤
                gained_points = sum(random.randint(1, 10) for _ in range(real_inc))
                stat_points += gained_points
            else:
                real_inc = 0
                gained_points = 0

            # (임시) 모험 보상 골드: 난이도별 고정 + 약간 랜덤
            base_gold = {"쉬움": 10, "보통": 20, "어려움": 35}[difficulty]
            gain_gold = base_gold + random.randint(0, 5)
            gold += gain_gold

            update_user_fields(
                kakao_user_id,
                level=level,
                gold=gold,
                stat_points=stat_points,
                fatigue=fatigue,
                pending=None
            )

            return kakao_text_response(
                f"🧭 {difficulty} 모험 완료!\n"
                f"피로도 -{cost} (남음 {fatigue})\n"
                f"골드 +{gain_gold} (총 {gold})\n"
                f"레벨 +{real_inc} (Lv.{level})\n"
                f"스탯포인트 +{gained_points} (보유 {stat_points})"
            )

        return kakao_text_with_quick_replies(
            "난이도를 버튼으로 선택해주세요.",
            [("쉬움", "모험 쉬움"), ("보통", "모험 보통"), ("어려움", "모험 어려움")]
        )

    # ====== Commands ======
    if msg in ["/내정보", "내정보", "/me"]:
        # 최신 전투력 계산용으로 row 다시 읽는 대신 현재 변수로 간단 표기
        # (정확 전투력은 DB row 기반이지만 값 동기화돼 있으니 OK)
        tmp_user = dict(user)
        tmp_user.update({"job": job, "hp": hp, "atk": atk, "int_stat": int_stat, "spd": spd, "luk": luk})
        # sqlite3.Row처럼 접근 가능하게 만들기 귀찮으니, 그냥 간단 계산을 여기서 동일 로직으로 재계산
        main = job_main_stat(job)
        if main == "atk":
            power = hp + atk * 3 + int_stat + spd
        elif main == "int_stat":
            power = hp + int_stat * 3 + atk + spd
        else:
            power = hp + spd * 3 + atk + int_stat

        job_kr = {"WARRIOR": "전사", "MAGE": "마법사", "NINJA": "닌자"}.get(job, job)

        return kakao_text_response(
            f"📌 내정보\n"
            f"직업: {job_kr}\n"
            f"레벨: {level}\n"
            f"피로도: {fatigue}\n"
            f"스탯포인트: {stat_points}\n"
            f"골드: {gold}\n"
            f"무기강화: +{weapon_level}\n"
            f"\n[스탯]\n"
            f"HP {hp}/{HP_CAP}\n"
            f"ATK {atk}/{ATK_CAP}\n"
            f"INT {int_stat}/{INT_CAP}\n"
            f"SPD {spd}/{SPD_CAP}\n"
            f"LUK {luk}/{LUK_CAP}\n"
            f"\n전투력: {power}"
        )

    if msg in ["/스탯", "스탯"]:
        return kakao_text_response(
            f"[스탯]\n"
            f"HP {hp}/{HP_CAP}\n"
            f"ATK {atk}/{ATK_CAP}\n"
            f"INT {int_stat}/{INT_CAP}\n"
            f"SPD {spd}/{SPD_CAP}\n"
            f"LUK {luk}/{LUK_CAP}\n"
            f"\n스탯포인트: {stat_points}\n"
            f"투자하려면 '스탯 사용'을 입력해줘."
        )

    if msg in ["/직업", "직업"]:
        # 이미 직업이 있으면 변경 불가
        if job is not None:
            return kakao_text_response("❌ 현재는 직업 변경이 불가능합니다.")

        set_pending(kakao_user_id, "JOB_SELECT")
        return kakao_text_with_quick_replies(
            "직업을 선택해주세요.",
            [("전사", "직업 전사"), ("마법사", "직업 마법사"), ("닌자", "직업 닌자")]
        )

    if msg in ["스탯 사용", "/스탯사용"]:
        if stat_points <= 0:
            return kakao_text_response("스탯 포인트가 없어. 모험으로 레벨업을 노려봐.")
        set_pending(kakao_user_id, "STAT_ALLOC")
        return kakao_text_response(
            "어느 스탯에 몇 포인트 투자할지 입력해줘.\n"
            "예시: HP 5 / ATK 3 / INT 2 / SPD 1 / LUK 4"
        )

    if msg in ["/모험", "모험"]:
        set_pending(kakao_user_id, "ADVENTURE_SELECT")
        return kakao_text_with_quick_replies(
            "난이도를 선택해주세요.",
            [("쉬움(피로1)", "모험 쉬움"), ("보통(피로2)", "모험 보통"), ("어려움(피로3)", "모험 어려움")]
        )

    if msg in ["/출석", "출석", "출석체크", "출석 체크"]:
        today = today_kst_str()
        if last_att == today:
            return kakao_text_response("✅ 오늘은 이미 출석했어. (피로도 +30은 하루 1회)")
        fatigue = fatigue + 30
        update_user_fields(kakao_user_id, fatigue=fatigue, last_attendance=today)
        return kakao_text_response(f"✅ 출석 완료!\n피로도 +30\n현재 피로도: {fatigue}")

    if msg in ["/강화", "강화"]:
        cost = 50 + weapon_level * 25
        if gold < cost:
            return kakao_text_response(f"💸 골드 부족!\n강화 비용: {cost}\n현재 골드: {gold}")

        success_rate = max(10, 70 - weapon_level * 10)
        roll = random.randint(1, 100)

        gold -= cost
        if roll <= success_rate:
            weapon_level += 1
            update_user_fields(kakao_user_id, gold=gold, weapon_level=weapon_level)
            return kakao_text_response(
                f"✨ 강화 성공! (+{weapon_level})\n"
                f"(성공률 {success_rate}%, 비용 {cost})\n"
                f"남은 골드: {gold}"
            )
        else:
            update_user_fields(kakao_user_id, gold=gold, weapon_level=weapon_level)
            return kakao_text_response(
                f"💥 강화 실패…\n"
                f"(성공률 {success_rate}%, 비용 {cost})\n"
                f"남은 골드: {gold}"
            )

    return kakao_text_response("모르는 명령어야. /도움 을 입력해봐.")
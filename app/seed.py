"""数据导入与初始化
  python seed.py words   # 词表 jsonl → SQLite（可重复执行，幂等）
  python seed.py users   # 创建 mily/mido（密码仅首次生成时打印一次）
  python seed.py audio   # 批量下载真人发音 mp3 → data/audio/
"""
import json
import re
import secrets
import sys
import time
import urllib.request
from pathlib import Path

import auth
from database import get_db, init_db

BASE = Path(__file__).parent
PARSED = BASE.parent / "data" / "parsed"
AUDIO = BASE / "data" / "audio"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}


def _clean_zh(s):
    if not s:
        return None
    s = re.sub(r"\s+\.", ".", s)      # "n ." → "n."
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip() or None


def seed_words():
    init_db()
    db = get_db()
    n_primary = n_junior = 0
    # 小学（koolearn 详情，含音标/释义/例句/mp3）
    f = PARSED / "koolearn_details.jsonl"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            word = re.sub(r"\s+", " ", d["word"].strip().lower())
            if not word or len(word) > 40:
                continue
            db.execute("""INSERT INTO words(word, phonetic_uk, phonetic_us, definition_zh,
                          definition_en, example_en, mp3)
                          VALUES(?,?,?,?,?,?,?)
                          ON CONFLICT(word) DO UPDATE SET
                            phonetic_uk=COALESCE(excluded.phonetic_uk, phonetic_uk),
                            phonetic_us=COALESCE(excluded.phonetic_us, phonetic_us),
                            definition_zh=COALESCE(excluded.definition_zh, definition_zh),
                            definition_en=COALESCE(excluded.definition_en, definition_en),
                            example_en=COALESCE(excluded.example_en, example_en),
                            mp3=COALESCE(excluded.mp3, mp3)""",
                       (word, d.get("phonetic_uk"), d.get("phonetic_us"),
                        _clean_zh(d.get("definition_zh")), d.get("definition_en"),
                        d.get("example_en"), d.get("mp3")))
            wid = db.execute("SELECT id FROM words WHERE word=?", (word,)).fetchone()["id"]
            grade = "P13" if d.get("grade") == "primary_1_3" else "P45"
            db.execute("""INSERT OR IGNORE INTO word_levels(word_id, textbook, grade_code)
                          VALUES(?, 'oxford_sh', ?)""", (wid, grade))
            n_primary += 1
    # 初中考纲（官方2020版，词+中文释义）
    f = PARSED / "junior_2020.jsonl"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            word = re.sub(r"\s+", " ", d["word"].strip().lower())
            if not word or len(word) > 40:
                continue
            db.execute("""INSERT INTO words(word, definition_zh) VALUES(?,?)
                          ON CONFLICT(word) DO UPDATE SET
                            definition_zh=COALESCE(excluded.definition_zh, definition_zh)""",
                       (word, _clean_zh(d.get("definition_zh"))))
            wid = db.execute("SELECT id FROM words WHERE word=?", (word,)).fetchone()["id"]
            db.execute("""INSERT OR IGNORE INTO word_levels(word_id, textbook, grade_code)
                          VALUES(?, 'zhongkao', 'JUNIOR')""", (wid,))
            n_junior += 1
    # ---- 数据修正（幂等，防重导回退）----
    # 1) 教材角色名/人名剔除
    for name in ("paul", "zoe", "kitty", "jean"):
        row = db.execute("SELECT id FROM words WHERE word=?", (name,)).fetchone()
        if row:
            db.execute("DELETE FROM word_levels WHERE word_id=?", (row["id"],))
            db.execute("DELETE FROM plan_words WHERE word_id=?", (row["id"],))
            db.execute("DELETE FROM words WHERE id=?", (row["id"],))
    # 2) 小学教材义补齐/纠偏（koolearn 详情缺失或抓成通用词义）
    import shutil as _shutil, os as _os
    for w, zh in {"june": "n. 六月", "ring": "n. 戒指；铃声 v. 打电话；按铃",
                  "hay": "n. 干草", "castle": "n. 城堡", "hammer": "n. 锤子 v. 锤打",
                  "cherry": "n. 樱桃", "march": "n. 三月"}.items():
        db.execute("UPDATE words SET definition_zh=? WHERE word=? AND (definition_zh IS NULL OR definition_zh='' OR word='march')", (zh, w))
    # 3) 专有名词大写化（教材以大写形式教学）+ 音频随复制
    AUDIO = BASE / "data" / "audio"
    for w in ("june","july","august","september","october","november","december","march",
              "monday","tuesday","wednesday","thursday","friday","saturday","sunday",
              "china","chinese","england","english","america","london","christmas"):
        row = db.execute("SELECT id FROM words WHERE word=?", (w,)).fetchone()
        if row:
            cap = w[0].upper() + w[1:]
            src, dst = AUDIO / (w + ".mp3"), AUDIO / (cap + ".mp3")
            if src.exists() and not dst.exists():
                _shutil.copy(src, dst)
            db.execute("UPDATE words SET word=? WHERE id=?", (cap, row["id"]))
    db.commit()
    total = db.execute("SELECT COUNT(*) c FROM words").fetchone()["c"]
    lv = {r["grade_code"]: r["c"] for r in db.execute(
        "SELECT grade_code, COUNT(*) c FROM word_levels GROUP BY grade_code")}
    db.close()
    print(f"导入完成: 小学详情 {n_primary} 条, 初中考纲 {n_junior} 条; words 总计 {total}; 分级 {lv}")


def seed_users():
    init_db()
    db = get_db()
    for uname, mode, display in (("mily", "standard", "Mily"), ("mido", "junior", "Mido")):
        exists = db.execute("SELECT id FROM users WHERE username=?", (uname,)).fetchone()
        if exists:
            print(f"用户 {uname} 已存在，跳过")
            continue
        pw = secrets.token_urlsafe(9)
        db.execute("""INSERT INTO users(username, password_hash, display_name, mode)
                      VALUES(?,?,?,?)""", (uname, auth.hash_password(pw), display, mode))
        print(f"创建用户 {uname} (模式 {mode}) 初始密码: {pw}  ← 请立即保存到密码管理器，此后不再显示")
    db.commit()
    db.close()


def seed_audio():
    init_db()
    AUDIO.mkdir(parents=True, exist_ok=True)
    db = get_db()
    rows = db.execute("SELECT word, mp3 FROM words WHERE mp3 IS NOT NULL").fetchall()
    db.close()
    done = skip = fail = 0
    for r in rows:
        dest = AUDIO / f"{r['word']}.mp3"
        if dest.exists():
            skip += 1
            continue
        try:
            req = urllib.request.Request(r["mp3"], headers=UA)
            with urllib.request.urlopen(req, timeout=20) as resp, dest.open("wb") as fo:
                fo.write(resp.read())
            done += 1
        except Exception as e:
            print(f"  ✗ {r['word']}: {e}")
            fail += 1
        time.sleep(0.25)
    print(f"音频下载完成: 新下载 {done}, 已存在 {skip}, 失败 {fail}")


# 手工核心词典：高频词的确定视觉锚点（自动匹配对抽象词/动词不可靠，宁缺毋滥）
EMOJI_MANUAL = {
    "run": "🏃", "swim": "🏊", "jump": "🤸", "sing": "🎤", "dance": "💃", "draw": "🎨",
    "read": "📖", "write": "✍️", "walk": "🚶", "play": "⚽", "eat": "🍽️", "drink": "🥤",
    "sleep": "😴", "fly": "🛫", "cry": "😢", "smile": "😊", "laugh": "😂",
    "happy": "😊", "sad": "😢", "angry": "😠", "tired": "😪", "afraid": "😨",
    "hot": "🥵", "cold": "🥶", "cool": "🧊", "good": "👍", "bad": "👎", "new": "✨",
    "love": "❤️", "like": "❤️", "big": "🐘", "small": "🐜", "long": "🦒", "short": "🐣",
    "red": "🔴", "blue": "🔵", "green": "🟢", "yellow": "🟡", "orange": "🟠",
    "purple": "🟣", "white": "⚪", "black": "⚫", "pink": "🌸", "brown": "🟤",
    "water": "💧", "fire": "🔥", "rain": "🌧️", "snow": "❄️", "wind": "💨",
    "sun": "☀️", "moon": "🌙", "star": "⭐", "sky": "🌤️", "sea": "🌊", "cloud": "☁️",
    "tree": "🌳", "flower": "🌸", "grass": "🌿", "leaf": "🍃", "mountain": "⛰️",
    "fish": "🐟", "bird": "🐦", "cat": "🐈", "dog": "🐕", "pig": "🐷", "cow": "🐄",
    "horse": "🐴", "sheep": "🐑", "duck": "🦆", "chicken": "🐔", "rabbit": "🐰",
    "mouse": "🐭", "bear": "🐻", "panda": "🐼", "monkey": "🐵", "tiger": "🐅",
    "lion": "🦁", "elephant": "🐘", "snake": "🐍", "frog": "🐸", "bee": "🐝",
    "butterfly": "🦋", "ant": "🐜", "egg": "🥚", "milk": "🥛", "rice": "🍚",
    "noodles": "🍜", "bread": "🍞", "dumpling": "🥟", "hamburger": "🍔", "pizza": "🍕",
    "cake": "🎂", "ice cream": "🍦", "candy": "🍬", "chocolate": "🍫",
    "apple": "🍎", "banana": "🍌", "grape": "🍇", "peach": "🍑", "pear": "🍐",
    "orange": "🍊", "melon": "🍈", "carrot": "🥕", "potato": "🥔", "tomato": "🍅",
    "car": "🚗", "bus": "🚌", "train": "🚂", "plane": "✈️", "ship": "🚢",
    "bike": "🚲", "boat": "⛵", "taxi": "🚕", "truck": "🚚",
    "door": "🚪", "window": "🪟", "chair": "🪑", "bed": "🛏️", "table": "🪵",
    "clock": "🕒", "watch": "⌚", "key": "🔑", "lock": "🔒", "light": "💡",
    "pen": "🖊️", "pencil": "✏️", "ruler": "📏", "book": "📖", "bag": "🎒",
    "shoe": "👟", "sock": "🧦", "shirt": "👕", "dress": "👗", "hat": "👒",
    "head": "🙆", "face": "😀", "eye": "👁️", "ear": "👂", "nose": "👃",
    "mouth": "👄", "tooth": "🦷", "hand": "✋", "arm": "💪", "leg": "🦵",
    "foot": "🦶", "hair": "💇", "heart": "❤️",
    "boy": "👦", "girl": "👧", "man": "👨", "woman": "👩", "baby": "👶",
    "family": "👨‍👩‍👧", "friend": "🤝", "mother": "👩", "father": "👨",
    "doctor": "🧑‍⚕️", "teacher": "🧑‍🏫", "student": "🧑‍🎓", "farmer": "🧑‍🌾",
    "police": "👮", "cook": "🧑‍🍳", "worker": "👷",
    "school": "🏫", "home": "🏠", "house": "🏠", "park": "🌳", "hospital": "🏥",
    "zoo": "🦁", "shop": "🏪", "farm": "🚜", "garden": "🌷", "beach": "🏖️",
    "money": "💰", "music": "🎵", "song": "🎶", "game": "🎮", "ball": "⚽",
    "gift": "🎁", "phone": "📱", "computer": "💻", "TV": "📺", "photo": "📷",
    "spring": "🌱", "summer": "🏖️", "autumn": "🍂", "winter": "⛄",
    "morning": "🌅", "night": "🌃", "day": "☀️", "week": "📅", "year": "📅",
    "today": "📆", "tomorrow": "🔮", "yesterday": "📜",
}


def seed_emoji():
    """emoji 关联 v3：手工核心词典优先 + Unicode 官方名精确匹配 + 颜色/开合修饰后缀。
    跳过国旗（regional indicators）。自动模糊匹配全部废除——质量优先于覆盖。"""
    f = BASE.parent / "data" / "ext" / "emoji-test.txt"
    if not f.exists():
        print("缺 data/ext/emoji-test.txt"); return
    MODIFIERS = {"red", "green", "white", "black", "blue", "yellow", "brown", "pink",
                 "orange", "purple", "dark", "light", "open", "closed"}
    exact, mod = {}, {}
    for line in f.read_text(encoding="utf-8").splitlines():
        if "; fully-qualified" not in line or "#" not in line:
            continue
        cp = line.split(";")[0].split()
        try:
            if any(0x1F1E6 <= int(c, 16) <= 0x1F1FF for c in cp):   # 国旗跳过
                continue
        except ValueError:
            continue
        m = re.search(r"# (\S+) E[\d.]+\s+(.+?)\s*$", line)
        if not m:
            continue
        emoji_ch, name = m.group(1), m.group(2)
        exact.setdefault(name, emoji_ch)
        parts = name.split()
        if len(parts) == 2 and parts[0] in MODIFIERS:
            mod.setdefault(parts[1], emoji_ch)
    init_db()
    db = get_db()
    db.execute("UPDATE words SET emoji=NULL")
    n = 0
    manual_hit = 0
    for row in db.execute("SELECT id, word FROM words").fetchall():
        w = row["word"]
        e = EMOJI_MANUAL.get(w)
        if e:
            manual_hit += 1
        e = e or exact.get(w) or mod.get(w)
        if not e and w.endswith("s") and len(w) > 3 and w not in ("news", "maths", "physics"):
            e = EMOJI_MANUAL.get(w[:-1]) or exact.get(w[:-1]) or mod.get(w[:-1])
        if e:
            db.execute("UPDATE words SET emoji=? WHERE id=?", (e, row["id"]))
            n += 1
    db.commit()
    db.close()
    print(f"emoji 关联: {n} 词（手工词典命中 {manual_hit}，质量优先）")


def seed_frq():
    """词频排名 → words.frq（池排序依据；csv 由宿主机 fetch_vocab.py frq 生成）"""
    f = BASE.parent / "data" / "parsed" / "frq.csv"
    if not f.exists():
        print("缺 data/parsed/frq.csv（宿主机先跑 fetch_vocab.py frq）"); return
    mapping = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        w, r = line.rsplit(",", 1)
        mapping[w] = int(r)
    init_db()
    db = get_db()
    for w, r in mapping.items():
        db.execute("UPDATE words SET frq=? WHERE word=?", (r, w))
    db.commit()
    db.close()
    print(f"词频更新: {len(mapping)} 词")


def seed_senses():
    """精修义项导入（v4.1/v4.2 内容管线产物）：data/parsed/word_senses.jsonl
    每行: {word, sense_order, pos, definition_zh, definition_en, example_en, example_zh[, confidence]}
    幂等：按词先删后插。低置信行的审校在 JSONL 层完成，重导即生效。"""
    import json
    f = BASE.parent / "data" / "parsed" / "word_senses.jsonl"
    if not f.exists():
        print("缺 data/parsed/word_senses.jsonl（先跑内容生成管线）"); return
    init_db()
    db = get_db()
    n_words, n_senses = 0, 0
    # 按词聚合后整体替换（逐行 DELETE+INSERT 会把同词前一义项删掉）
    by_word = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        by_word.setdefault(d["word"], []).append(d)
    for word, senses in by_word.items():
        wid = db.execute("SELECT id FROM words WHERE word=?", (word,)).fetchone()
        if not wid:
            print(f"  跳过（词表无此词）: {word}")
            continue
        db.execute("DELETE FROM word_senses WHERE word_id=?", (wid["id"],))
        for d in sorted(senses, key=lambda s: s["sense_order"]):
            db.execute("""INSERT INTO word_senses(word_id, sense_order, pos, definition_zh,
                          definition_en, example_en, example_zh, confidence) VALUES(?,?,?,?,?,?,?,?)""",
                       (wid["id"], d["sense_order"], d.get("pos"), d["definition_zh"],
                        d.get("definition_en"), d.get("example_en"), d.get("example_zh"),
                        d.get("confidence", "high")))
            n_senses += 1
        n_words += 1
    db.commit()
    db.close()
    print(f"精修义项: {n_words} 词 {n_senses} 义项")


def seed_quiz():
    """题库导入（v4.1）：data/parsed/word_quiz.jsonl
    每行: {word, quiz_type, stage, idx, stem, options:[...], answer}
    幂等：按 (word_id, quiz_type, stage, idx) UPSERT。"""
    import json
    f = BASE.parent / "data" / "parsed" / "word_quiz.jsonl"
    if not f.exists():
        print("缺 data/parsed/word_quiz.jsonl（先跑题目生成管线）"); return
    init_db()
    db = get_db()
    # 题库是纯生成物：全清重灌（UPSERT 会残留凑不齐干扰而消失的旧题）
    db.execute("DELETE FROM word_quiz")
    db.execute("DELETE FROM user_quiz_used")
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        wid = db.execute("SELECT id FROM words WHERE word=?", (d["word"],)).fetchone()
        if not wid:
            print(f"  跳过（词表无此词）: {d['word']}")
            continue
        db.execute("""INSERT INTO word_quiz(word_id, quiz_type, stage, idx, stem, options, answer)
                      VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(word_id, quiz_type, stage, idx) DO UPDATE SET
                      stem=excluded.stem, options=excluded.options, answer=excluded.answer""",
                   (wid["id"], d["quiz_type"], d["stage"], d.get("idx", 1),
                    d.get("stem", ""), json.dumps(d.get("options", []), ensure_ascii=False),
                    d["answer"]))
        n += 1
    db.commit()
    db.close()
    print(f"题库: {n} 题")


def seed_forms():
    """词形变化导入（v4.2）：data/parsed/word_forms.jsonl
    每行: {word, forms:{p:过去式,...}}——按词整体覆盖，无 forms 的词清空。"""
    import json
    f = BASE.parent / "data" / "parsed" / "word_forms.jsonl"
    if not f.exists():
        print("缺 data/parsed/word_forms.jsonl（先跑 gen_forms.py）"); return
    init_db()
    db = get_db()
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        wid = db.execute("SELECT id FROM words WHERE word=?", (d["word"],)).fetchone()
        if not wid:
            continue
        db.execute("UPDATE words SET forms=? WHERE id=?",
                   (json.dumps(d["forms"], ensure_ascii=False), wid["id"]))
        n += 1
    db.commit()
    db.close()
    print(f"词形: {n} 词")


def seed_phonetic():
    """音标补丁导入（ECDICT 等）：data/parsed/phonetic_patch.jsonl
    每行: {word, phonetic_uk, phonetic_us}——只覆盖空缺，不覆盖已有值。"""
    import json
    f = BASE.parent / "data" / "parsed" / "phonetic_patch.jsonl"
    if not f.exists():
        print("缺 data/parsed/phonetic_patch.jsonl"); return
    init_db()
    db = get_db()
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        uk, us = d.get("phonetic_uk"), d.get("phonetic_us")
        row = db.execute("SELECT id, phonetic_uk, phonetic_us FROM words WHERE word=?", (d["word"],)).fetchone()
        if not row:
            continue
        nuk = row["phonetic_uk"] or uk
        nus = row["phonetic_us"] or us
        if nuk != row["phonetic_uk"] or nus != row["phonetic_us"]:
            db.execute("UPDATE words SET phonetic_uk=?, phonetic_us=? WHERE id=?",
                       (nuk, nus, row["id"]))
            n += 1
    db.commit()
    db.close()
    print(f"音标补齐: {n} 词")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "words":
        seed_words()
    elif cmd == "users":
        seed_users()
    elif cmd == "audio":
        seed_audio()
    elif cmd == "emoji":
        seed_emoji()
    elif cmd == "frq":
        seed_frq()
    elif cmd == "senses":
        seed_senses()
    elif cmd == "quiz":
        seed_quiz()
    elif cmd == "phonetic":
        seed_phonetic()
    elif cmd == "forms":
        seed_forms()
    else:
        print("用法: python seed.py words|users|audio|emoji|frq|senses|quiz|forms|phonetic")
        sys.exit(1)

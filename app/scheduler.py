"""调度核心 v4.2：计划生成（全局词频序）、每日队列（热身轮+新词4题+波次复习+巩固轮供题）、
答题状态机、表格确认流程（取代旧 spot 抽查+全量摸底）。

状态机（不变）: 首学 stage=1,next=D+2 → 复习答对 stage=2,next=复习日+5
→ 答对 stage=3,next=复习日+12 → 答对 mastered（全对时即首学后第 2/7/19 天）。
复习答错: stage 重置为 1, next=D+2 完整回炉；当日重现由前端推回队尾。
掌握词每日抽检（v2.6 方案A）：audit_schedule 当日随机抽 AUDIT_N 个已掌握词混入复习队列，
答对无事、答错打回 D+2 重走——掌握后仍有留存防线，且产出真实留存率数据。

v4.2 新词首学 4 题（多角度）：word_zh(形→义) + listen_zh(音→义) + zh_word(义→形)
+ spell(音→形·当天听写)；junior 为 word_zh + listen_zh + emoji(听音选词，无 emoji
词退化 zh_word)。巩固轮（前端在主循环后编排）：每词 1 题从 d5/d2 池借用
（mark_used=False 不消耗正式复习配额），不进调度。

v4.1 摸底 = 表格确认三步：速选（认识的标 verify）→ 验证轮（1 题四选一，对→skipped
错→回候选池即删状态）→ 建计划时按全局词频序续补至目标词量。
旧 spot/screening 的两连对验证废弃（verify_count 字段 legacy 保留不再使用）。
"""
import json
import math
import random
import re
from datetime import date, timedelta
from database import get_db

INTERVALS = [2, 5, 12]           # stage-1 → 间隔天数（从每次复习当天起算；扩张比≈2.5）
REST_DAYS = 12                   # 计划尾部停投新词的天数（保证最后的词走完复习周期）
REVIEW_CAP = 60                  # 单日复习上限（积压分日消化，不改动调度状态；稳态需求≈3×daily_new）
NEW_THROTTLE_AT = 80             # 到期复习积压超过此值 → 当日新词减半（先还债再扩新）
AUDIT_N = 5                      # 掌握词每日抽检数
POS_RE = re.compile(r"^((?:n|v|adj|adv|prep|conj|pron|num|art|int|vt|vi|aux|abbr|modal)\.)")

STAGE_BY_REVIEW = {1: "d2", 2: "d5", 3: "d12"}   # 复习 stage → 题库阶段
JUNIOR_TYPES = ("word_zh", "listen_zh", "zh_word")   # junior 可用题型（无拼写/挖空；zh_word 为再认型可选）


# ---------- 出题引擎 ----------
def _gen_fallback_quiz(db, word, definition_zh, stage, quiz_type="word_zh"):
    """题库缺题时的兜底：现算看词选义四选一（同词性干扰优先）。
    cloze/spell 等题型无法现算，统一降级为 word_zh。"""
    pos = POS_RE.match(definition_zh or "")
    short = (definition_zh or "").split(" | ")[0].strip()
    if not short:
        return None
    distract = []
    if pos:
        distract = [p["definition_zh"].split(" | ")[0].strip() for p in db.execute("""
            SELECT definition_zh FROM words
            WHERE definition_zh LIKE ? AND definition_zh != ? AND definition_zh IS NOT NULL
            ORDER BY RANDOM() LIMIT 3""", (pos.group(1) + "%", definition_zh))]
    if len(distract) < 3:
        extra = [p["definition_zh"].split(" | ")[0].strip() for p in db.execute("""
            SELECT definition_zh FROM words
            WHERE definition_zh IS NOT NULL AND definition_zh != '' AND definition_zh != ?
            ORDER BY RANDOM() LIMIT ?""", (definition_zh, 3 - len(distract)))]
        distract += extra
    opts = list(dict.fromkeys(distract[:3] + [short]))
    if len(opts) < 4:                      # 干扰枯竭，凑不齐四选一
        return None
    random.shuffle(opts)
    return {"quiz_id": None, "quiz_type": quiz_type, "stage": stage,
            "stem": word, "options": opts, "answer": short}


def get_quiz(db, word_id, word, definition_zh, stage, user_id=None, junior=False, mark_used=True):
    """取该词该阶段一道题：预生成题库优先（排除该用户已用），耗尽允许复用旧题
    （复习场景间隔≥3天重复出题可接受），无题库则现算兜底。
    mark_used=False 供巩固轮借用 d5/d2 题而不消耗正式复习的防重复配额。"""
    rows = db.execute("""SELECT q.id, q.quiz_type, q.stem, q.options, q.answer FROM word_quiz q
                         WHERE q.word_id=? AND q.stage=?""", (word_id, stage)).fetchall()
    if junior:
        rows = [r for r in rows if r["quiz_type"] in JUNIOR_TYPES]
    if rows:
        used = set()
        if user_id:
            used = {r["quiz_id"] for r in db.execute(
                "SELECT quiz_id FROM user_quiz_used WHERE user_id=?", (user_id,))}
        fresh = [r for r in rows if r["id"] not in used]
        pool = fresh or rows
        r = random.choice(pool)
        if user_id and mark_used:
            db.execute("INSERT OR IGNORE INTO user_quiz_used(user_id, quiz_id) VALUES(?,?)",
                       (user_id, r["id"]))
        return {"quiz_id": r["id"], "quiz_type": r["quiz_type"], "stage": stage,
                "stem": r["stem"], "options": json.loads(r["options"]), "answer": r["answer"]}
    return _gen_fallback_quiz(db, word, definition_zh, stage)


def _quiz_by_type(db, word_id, stage, quiz_type, user_id=None, mark_used=True):
    """按题型精确取题（learn 题型序列用）：排除已用，耗尽复用，题库缺该题型返回 None。
    mark_used=False 供用词关借用 cloze 题而不消耗正式 d5 复习的防重复配额。"""
    rows = db.execute("""SELECT id, quiz_type, stem, options, answer FROM word_quiz
                         WHERE word_id=? AND stage=? AND quiz_type=?""",
                      (word_id, stage, quiz_type)).fetchall()
    if not rows:
        return None
    used = set()
    if user_id:
        used = {r["quiz_id"] for r in db.execute(
            "SELECT quiz_id FROM user_quiz_used WHERE user_id=?", (user_id,))}
    pool = [r for r in rows if r["id"] not in used] or rows
    r = random.choice(pool)
    if user_id and mark_used:
        db.execute("INSERT OR IGNORE INTO user_quiz_used(user_id, quiz_id) VALUES(?,?)",
                   (user_id, r["id"]))
    return {"quiz_id": r["id"], "quiz_type": r["quiz_type"], "stage": stage,
            "stem": r["stem"], "options": json.loads(r["options"]), "answer": r["answer"]}


def get_learn_quizzes(db, user_id, word_id, word, definition_zh, junior, emoji=None):
    """新词首学题（v4.2 多角度 4 题，全部不计调度）：
    standard: word_zh(形→义) + listen_zh(音→义) + zh_word(义→形) + spell(音→形·产出)
    junior:   word_zh + listen_zh + emoji(听音选词·视觉)；无 emoji 词退化 zh_word。
    题库缺某题型则跳过；全缺时兜底现算 1 题。"""
    seq = ["word_zh", "listen_zh", "zh_word", "spell"] if not junior else \
          ["word_zh", "listen_zh", "emoji" if emoji else "zh_word"]
    out = []
    for t in seq:
        q = _quiz_by_type(db, word_id, "learn", t, user_id)
        if q:
            out.append(q)
    if not out:
        q = _gen_fallback_quiz(db, word, definition_zh, "learn")
        return [q] if q else []
    return out


def _new_word_item(db, r, user_id, junior):
    """新词条目装配：词卡信息 + 首学题 + 用词关题（new 与 learned_today 共用）。"""
    item = _word_brief(db, r, user_id, junior)
    item["quizzes"] = get_learn_quizzes(db, user_id, r["id"], r["word"],
                                        r["definition_zh"], junior, r["emoji"])
    # 用词关题：组词（cloze 挖空）优先——当天学的词当天练用词；无 cloze 的词退化 d5/d2。
    # 借题不记 user_quiz_used，不消耗正式复习的防重复配额。
    item["reinforce"] = (
        (None if junior else _quiz_by_type(db, r["id"], "d5", "cloze", user_id, mark_used=False))
        or get_quiz(db, r["id"], r["word"], r["definition_zh"], "d5",
                    user_id, junior, mark_used=False)
        or get_quiz(db, r["id"], r["word"], r["definition_zh"], "d2",
                    user_id, junior, mark_used=False))
    return item


# ---------- 表格确认（v4.1 摸底）----------
BASIC_FRQ = 500        # 超高频基础词阈值：这些词默认已会，走快测抽查而非表格逐词确认
BASIC_QUIZ_N = 15      # 快测题数
BASIC_PASS = 0.93      # 快测通过阈值（15 题容错 1）


def need_confirm(db, user):
    """standard 且 3 年级及以上且未完成表格确认 → True。
    junior（1-2年级）与 adult（家长体验）不做确认。"""
    g = user["grade"] if user else None
    return bool(user and user["mode"] == "standard" and g
                and g not in ("1A", "1B", "2A", "2B", "adult")
                and not user["confirm_done"])


def _basic_pool_ids(db, user_id, grade):
    """基础词段：年级候选段内 frq≤BASIC_FRQ 且无状态的词"""
    grades = _confirm_grades(grade)
    return [r["id"] for r in db.execute("""
        SELECT w.id FROM words w
        JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN (%s)
        WHERE COALESCE(w.frq, 999999) <= ?
              AND NOT EXISTS (SELECT 1 FROM user_word_state s
                              WHERE s.user_id=? AND s.word_id=w.id)
        GROUP BY w.id""" % ",".join("?" * len(grades)),
        (*grades, BASIC_FRQ, user_id)).fetchall()]


def confirm_basic_quiz(user_id):
    """基础词快测：从超高频段随机抽 15 题（题库现成 word_zh）。"""
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["basic_quiz_done"]:
            return []
        ids = _basic_pool_ids(db, user_id, user["grade"])
        random.shuffle(ids)
        quiz = []
        for wid in ids[:BASIC_QUIZ_N]:
            w = db.execute("SELECT word, phonetic_uk, definition_zh FROM words WHERE id=?", (wid,)).fetchone()
            q = get_quiz(db, wid, w["word"], w["definition_zh"], "confirm")
            if q is None:
                continue
            quiz.append({**q, "word_id": wid, "word": w["word"],
                         "phonetic_uk": w["phonetic_uk"]})
        return quiz
    finally:
        db.close()


def confirm_basic_result(user_id, answers):
    """快测判定：{word_id: 对错}。≥BASIC_PASS → 整段 skipped（答错词删状态捞回表格）；
    未达标 → 不写状态，基础词并入表格逐词确认。"""
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["basic_quiz_done"]:
            raise ValueError("快测已提交，请勿重复提交")
        if not answers or len(answers) > BASIC_QUIZ_N:
            raise ValueError("答案数量非法")
        correct = sum(1 for v in answers.values() if v)
        passed = len(answers) > 0 and correct / len(answers) >= BASIC_PASS
        wrong_ids = [int(k) for k, v in answers.items() if not v]
        if passed:
            grades = _confirm_grades(user["grade"])
            db.execute("""INSERT INTO user_word_state(user_id, word_id, state)
                SELECT ?, w.id, 'skipped' FROM words w
                JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN (%s)
                WHERE COALESCE(w.frq, 999999) <= ?
                      AND NOT EXISTS (SELECT 1 FROM user_word_state s
                                      WHERE s.user_id=? AND s.word_id=w.id)
                GROUP BY w.id""" % ",".join("?" * len(grades)),
                (user_id, *grades, BASIC_FRQ, user_id))
            # 答错词捞回：删状态 → 回候选池（表格里排最前，词频最高）
            for wid in wrong_ids:
                db.execute("DELETE FROM user_word_state WHERE user_id=? AND word_id=?",
                           (user_id, wid))
        db.execute("UPDATE users SET basic_quiz_done=1 WHERE id=?", (user_id,))
        db.commit()
        skipped = db.execute("SELECT COUNT(*) c FROM user_word_state WHERE user_id=? AND state='skipped'",
                             (user_id,)).fetchone()["c"]
        return {"passed": passed, "correct": correct, "total": len(answers),
                "wrong_back": len(wrong_ids), "skipped_total": skipped}
    finally:
        db.close()


def _confirm_grades(grade):
    """年级先验：低段默认已会不进表格。3-5年级 → P45+JUNIOR；6年级及以上 → JUNIOR。"""
    if grade and grade[0].isdigit() and 3 <= int(grade[0]) <= 5:
        return ("P45", "JUNIOR")
    return ("JUNIOR",)


def confirm_pool(user_id, size):
    """表格候选：年级段 ∪ 之后段，排除已有状态词（mily 保留的旧摸底 skipped 生效），
    全局词频序取前 size（= 目标词量 × 1.3，由前端传入）。
    GROUP BY w.id：同一词可双归属（P45∩JUNIOR 交集），必须去重。"""
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or not need_confirm(db, user):
            return []
        rows = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.definition_zh FROM words w
            JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN (%s)
            WHERE NOT EXISTS (SELECT 1 FROM user_word_state s
                              WHERE s.user_id=? AND s.word_id=w.id)
            GROUP BY w.id
            ORDER BY COALESCE(w.frq, 999999), w.id LIMIT ?""" % ",".join("?" * 2),
            (*_confirm_grades(user["grade"]), user_id, size)).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


def confirm_submit(user_id, known_ids):
    """速选提交（v4.1.2 免验证版）：点「认识」的词直接 skipped 排除——
    用户决策砍掉验证轮（时间成本>假熟漏学风险；假熟多为超高频词阅读自巩固）。"""
    db = get_db()
    try:
        for wid in known_ids:
            wid = int(wid)
            db.execute("""INSERT INTO user_word_state(user_id, word_id, state) VALUES(?,?, 'skipped')
                          ON CONFLICT(user_id, word_id) DO UPDATE SET state='skipped', verify_count=0""",
                       (user_id, wid))
        db.commit()
    finally:
        db.close()


def confirm_quiz(user_id, size=10):
    """验证轮：为速选标认识的词取 1 题四选一（看词选义）。"""
    db = get_db()
    try:
        rows = db.execute("""SELECT w.id, w.word, w.phonetic_uk, w.definition_zh
                             FROM user_word_state s JOIN words w ON w.id=s.word_id
                             WHERE s.user_id=? AND s.state='verify' ORDER BY w.id LIMIT ?""",
                          (user_id, size)).fetchall()
        quiz = []
        for r in rows:
            q = get_quiz(db, r["id"], r["word"], r["definition_zh"], "confirm")
            if q is None:                      # 无释义无法出题 → 视为不认识，回候选池
                db.execute("DELETE FROM user_word_state WHERE user_id=? AND word_id=?",
                           (user_id, r["id"]))
                db.commit()
                continue
            quiz.append({**q, "word_id": r["id"], "word": r["word"], "phonetic_uk": r["phonetic_uk"]})
        return quiz
    finally:
        db.close()


def confirm_verify(user_id, word_id, correct):
    """验证提交：对 → skipped（真认识，排除出学习池）；错 → 删状态回候选池（会学的词）。"""
    db = get_db()
    try:
        if correct:
            db.execute("""UPDATE user_word_state SET state='skipped', verify_count=0
                          WHERE user_id=? AND word_id=?""", (user_id, word_id))
        else:
            db.execute("DELETE FROM user_word_state WHERE user_id=? AND word_id=?",
                       (user_id, word_id))
        db.commit()
    finally:
        db.close()


def confirm_status(user_id):
    db = get_db()
    try:
        verify = db.execute("""SELECT COUNT(*) c FROM user_word_state
                               WHERE user_id=? AND state='verify'""", (user_id,)).fetchone()["c"]
        skipped = db.execute("""SELECT COUNT(*) c FROM user_word_state
                                WHERE user_id=? AND state='skipped'""", (user_id,)).fetchone()["c"]
        return {"verify": verify, "skipped": skipped}
    finally:
        db.close()


def confirm_finish(user_id):
    """表格流程完成标记（速选提交后由前端调用）。
    兼容：旧验证流程遗留的 verify 词按免验证语义直接转 skipped。"""
    db = get_db()
    try:
        db.execute("""UPDATE user_word_state SET state='skipped'
                      WHERE user_id=? AND state='verify'""", (user_id,))
        db.execute("UPDATE users SET confirm_done=1 WHERE id=?", (user_id,))
        db.commit()
    finally:
        db.close()


# ---------- 计划 ----------
def _today() -> date:
    return date.today()


def _pool_grades(grade):
    """计划池的年级段集合：junior=P13；3-5年级=本段+后续全部；6年级及以上=JUNIOR。
    本段未 skip 的词（摸底确认不会的）自然在池内，skipped/人名（无段归属）出局。"""
    if not grade or grade in ("1A", "1B", "2A", "2B"):
        return ("P13",)
    if grade[0].isdigit() and 3 <= int(grade[0]) <= 5:
        return ("P13", "P45", "JUNIOR")
    return ("JUNIOR",)


def create_plan(user_id, target_words, days):
    """v4.1：池 = 年级段内候选（无状态或 new/verify 遗留）按全局词频序取 target。
    段序（GRADE_ORDER）废弃——严格从易到难。摸底产物（skipped）与无归属词（人名）出局。"""
    db = get_db()
    try:
        row = db.execute("SELECT id FROM study_plans WHERE user_id=? AND status='active'", (user_id,)).fetchone()
        if row:
            return None, "已有进行中的计划"
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if need_confirm(db, user):
            return None, "请先完成词表确认"
        grades = _pool_grades(user["grade"] if user else None)
        pool = [r["id"] for r in db.execute("""
            SELECT w.id FROM words w
            JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN (%s)
            WHERE NOT EXISTS (SELECT 1 FROM user_word_state s
                              WHERE s.user_id=? AND s.word_id=w.id
                                AND s.state IN ('learning','mastered','skipped'))
            GROUP BY w.id
            ORDER BY COALESCE(w.frq, 999999), w.id""" % ",".join("?" * len(grades)),
            (*grades, user_id)).fetchall()]
        target = min(target_words, len(pool))
        if target <= 0:
            return None, "没有可学的词"
        daily_new = max(1, math.ceil(target / max(1, days - REST_DAYS)))
        start, end = _today(), _today() + timedelta(days=days - 1)
        cur = db.execute("""INSERT INTO study_plans(user_id, target_words, start_date, end_date, daily_new)
                            VALUES(?,?,?,?,?)""",
                         (user_id, target, start.isoformat(), end.isoformat(), daily_new))
        plan_id = cur.lastrowid
        db.executemany("INSERT INTO plan_words(plan_id, word_id, seq) VALUES(?,?,?)",
                       [(plan_id, wid, i + 1) for i, wid in enumerate(pool[:target])])
        db.commit()
        return {"plan_id": plan_id, "target": target, "daily_new": daily_new,
                "start": start.isoformat(), "end": end.isoformat()}, None
    finally:
        db.close()


def get_active_plan(db, user_id):
    return db.execute("SELECT * FROM study_plans WHERE user_id=? AND status='active'",
                      (user_id,)).fetchone()


# ---------- 每日队列 ----------
def _word_brief(db, r, user_id, junior, with_senses=True):
    """词基础信息 + 义项列表（新卡片展示用）。r 需含 words 表各列。"""
    item = {"id": r["id"], "word": r["word"], "phonetic_uk": r["phonetic_uk"],
            "phonetic_us": r["phonetic_us"], "definition_zh": r["definition_zh"],
            "mp3": r["mp3"], "emoji": r["emoji"]}
    if with_senses:
        senses = db.execute("""SELECT sense_order, pos, definition_zh, definition_en,
                                      example_en, example_zh FROM word_senses
                               WHERE word_id=? ORDER BY sense_order""", (r["id"],)).fetchall()
        item["senses"] = [dict(s) for s in senses] if senses else None
    return item


def get_today_queue(user_id):
    """v4.1 队列：warmup（昨日词快闪）+ new（每词带 2 道首学题）+ review（每词带阶段题）。
    波次交错由前端编排（每 5 新词插一组复习）；后端只供原料。"""
    db = get_db()
    try:
        plan = get_active_plan(db, user_id)
        if not plan:
            return {"plan": None, "warmup": [], "new": [], "review": []}
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        junior = bool(user and user["mode"] == "junior")
        t = _today()
        t_str = t.isoformat()
        cutoff = (date.fromisoformat(plan["end_date"]) - timedelta(days=REST_DAYS)).isoformat()
        new_allowance = 0
        if t_str <= cutoff:
            log = db.execute("SELECT new_done FROM daily_logs WHERE user_id=? AND date=?",
                             (user_id, t_str)).fetchone()
            done = log["new_done"] if log else 0
            new_allowance = max(0, plan["daily_new"] - done)
        # 积压调节：到期复习过多时当日新词减半（先还债再扩新，防缺勤后滚雪球）
        due_n = db.execute("""SELECT COUNT(*) c FROM user_word_state
                              WHERE user_id=? AND state='learning' AND next_review_date<=?""",
                           (user_id, t_str)).fetchone()["c"]
        if due_n > NEW_THROTTLE_AT:
            new_allowance //= 2
        # 热身轮：昨天首学的词（快闪巩固，不提交不调度）
        yesterday = (t - timedelta(days=1)).isoformat()
        warmup = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji
            FROM user_word_state s JOIN words w ON w.id=s.word_id
            WHERE s.user_id=? AND s.first_learned_date=?
            ORDER BY w.id""", (user_id, yesterday)).fetchall()
        warmup_items = []
        for r in warmup:
            item = _word_brief(db, r, user_id, junior, with_senses=False)
            item["quiz"] = get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                    "confirm", user_id, junior) or None
            warmup_items.append(item)
        new_words = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji
            FROM plan_words p JOIN words w ON w.id=p.word_id
            WHERE p.plan_id=? AND NOT EXISTS (
                SELECT 1 FROM user_word_state s WHERE s.user_id=? AND s.word_id=w.id
                  AND s.first_learned_date IS NOT NULL)
            ORDER BY p.seq LIMIT ?""", (plan["id"], user_id, new_allowance)).fetchall()
        new_items = []
        for r in new_words:
            new_items.append(_new_word_item(db, r, user_id, junior))
        # 今日已学词（first_learned_date=今天）：前端刷新后据此重建关卡（中途退出不丢测验）
        learned_words = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji
            FROM plan_words p JOIN words w ON w.id=p.word_id
            JOIN user_word_state s ON s.user_id=? AND s.word_id=w.id
            WHERE p.plan_id=? AND s.first_learned_date=?
            ORDER BY p.seq""", (user_id, plan["id"], t_str)).fetchall()
        learned_items = [_new_word_item(db, r, user_id, junior) for r in learned_words]
        review = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji, s.stage
            FROM user_word_state s JOIN words w ON w.id=s.word_id
            WHERE s.user_id=? AND s.state='learning' AND s.next_review_date<=?
            ORDER BY s.next_review_date, w.id LIMIT ?""", (user_id, t_str, REVIEW_CAP)).fetchall()
        review_items = []
        for r in review:
            item = _word_brief(db, r, user_id, junior, with_senses=False)
            stage_key = STAGE_BY_REVIEW.get(r["stage"], "d2")
            item["quiz"] = get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                    stage_key, user_id, junior) or None
            review_items.append(item)
        # 掌握词每日抽检（方案A）：当日排样一次，done 标记防重出；答错由 submit_answer 打回 D+2
        db.execute("DELETE FROM audit_schedule WHERE user_id=? AND date<?", (user_id, t_str))
        audit_rows = db.execute("""SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji
                                   FROM audit_schedule a JOIN words w ON w.id=a.word_id
                                   WHERE a.user_id=? AND a.date=? AND a.done=0 ORDER BY a.rowid""",
                                 (user_id, t_str)).fetchall()
        if not audit_rows:
            has_today = db.execute("SELECT COUNT(*) c FROM audit_schedule WHERE user_id=? AND date=?",
                                   (user_id, t_str)).fetchone()["c"]
            if not has_today:   # 首次加载才抽样（全部完成后重开不重抽）
                audit_rows = db.execute("""SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us,
                                                  w.definition_zh, w.mp3, w.emoji
                                           FROM user_word_state s JOIN words w ON w.id=s.word_id
                                           WHERE s.user_id=? AND s.state='mastered'
                                           ORDER BY RANDOM() LIMIT ?""",
                                         (user_id, AUDIT_N)).fetchall()
                db.executemany("INSERT OR IGNORE INTO audit_schedule(user_id, date, word_id) VALUES(?,?,?)",
                               [(user_id, t_str, r["id"]) for r in audit_rows])
        for r in audit_rows:
            item = _word_brief(db, r, user_id, junior, with_senses=False)
            item["quiz"] = get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                    random.choice(("d2", "d5", "d12")), user_id, junior) or None
            item["audit"] = True
            review_items.append(item)
        db.commit()   # get_quiz 的 user_quiz_used 标记与抽检排样都要落盘（v2.6 修复：此前只读路径不 commit 全部回滚）
        return {"plan": dict(plan), "warmup": warmup_items, "new": new_items,
                "learned_today": learned_items, "review": review_items}
    finally:
        db.close()


def _bump_log(db, user_id, field):
    t = _today().isoformat()
    if field == "new":
        db.execute("""INSERT INTO daily_logs(user_id, date, new_done) VALUES(?,?,1)
                      ON CONFLICT(user_id, date) DO UPDATE SET new_done=new_done+1""", (user_id, t))
    else:
        db.execute("""INSERT INTO daily_logs(user_id, date, review_done) VALUES(?,?,1)
                      ON CONFLICT(user_id, date) DO UPDATE SET review_done=review_done+1""", (user_id, t))


# ---------- 答题状态机 ----------
def submit_answer(user_id, word_id, phase, correct, quiz_type=""):
    """phase: 'learn'=新词首学, 'review'=复习测试, 'audit'=掌握词抽检。
    首学 2 题 / 热身轮 / 当日重现轮均不调用此接口——只有「进入下一个新词」与复习首答才提交。
    同时写 quiz_log（学习日报数据源；quiz_type 由前端带回）。"""
    db = get_db()
    try:
        t = _today()
        db.execute("""INSERT INTO quiz_log(user_id, date, phase, word_id, quiz_type, correct)
                      VALUES(?,?,?,?,?,?)""",
                   (user_id, t.isoformat(), phase, word_id, quiz_type or "", 1 if correct else 0))
        if phase == "audit":
            db.execute("UPDATE audit_schedule SET done=1 WHERE user_id=? AND word_id=? AND date=?",
                       (user_id, word_id, t.isoformat()))
        if phase == "learn":
            # 防重复首学双计数：仅首次落 first_learned_date 时记 daily_logs
            existing = db.execute("""SELECT first_learned_date FROM user_word_state
                                     WHERE user_id=? AND word_id=?""", (user_id, word_id)).fetchone()
            is_first = not (existing and existing["first_learned_date"])
            next_date = (t + timedelta(days=INTERVALS[0])).isoformat()
            db.execute("""INSERT INTO user_word_state(user_id, word_id, state, stage,
                          first_learned_date, next_review_date)
                          VALUES(?,?, 'learning', 1, ?, ?)
                          ON CONFLICT(user_id, word_id) DO UPDATE SET
                          state='learning', stage=1,
                          first_learned_date=excluded.first_learned_date,
                          next_review_date=excluded.next_review_date""",
                       (user_id, word_id, t.isoformat(), next_date))
            if is_first:
                _bump_log(db, user_id, "new")
        else:
            row = db.execute("""SELECT stage FROM user_word_state
                                WHERE user_id=? AND word_id=?""", (user_id, word_id)).fetchone()
            if not row:
                raise ValueError("该词尚未学习")
            if correct:
                old = row["stage"]
                if old >= 3:
                    db.execute("""UPDATE user_word_state SET state='mastered', next_review_date=NULL
                                  WHERE user_id=? AND word_id=?""", (user_id, word_id))
                else:
                    next_date = (t + timedelta(days=INTERVALS[old])).isoformat()
                    db.execute("""UPDATE user_word_state SET stage=?, next_review_date=?
                                  WHERE user_id=? AND word_id=?""",
                               (old + 1, next_date, user_id, word_id))
            else:
                # 答错：stage 重置为 1，从最短间隔完整回炉（state 回 learning——
                # 掌握词抽检答错时原 state='mastered'，不改回则永远进不了复习管道）
                next_date = (t + timedelta(days=INTERVALS[0])).isoformat()
                db.execute("""UPDATE user_word_state SET state='learning', stage=1, next_review_date=?,
                              wrong_count=wrong_count+1
                              WHERE user_id=? AND word_id=?""",
                           (next_date, user_id, word_id))
            _bump_log(db, user_id, "review")
        db.commit()
    finally:
        db.close()


# ---------- 日历与进度 ----------
def get_calendar(user_id, month):
    db = get_db()
    try:
        plan = get_active_plan(db, user_id)
        logs = {r["date"]: dict(r) for r in db.execute(
            "SELECT * FROM daily_logs WHERE user_id=? AND date LIKE ?",
            (user_id, f"{month}-%")).fetchall()}
        days = []
        if plan:
            start = date.fromisoformat(plan["start_date"])
            end = date.fromisoformat(plan["end_date"])
            cutoff = end - timedelta(days=REST_DAYS)
            d = date.fromisoformat(f"{month}-01")
            while d.strftime("%Y-%m") == month:
                log = logs.get(d.isoformat(), {})
                target = plan["daily_new"] if start <= d <= cutoff else 0
                days.append({"date": d.isoformat(),
                             "new_done": log.get("new_done", 0),
                             "review_done": log.get("review_done", 0),
                             "target_new": target,
                             "in_plan": start <= d <= end})
                d += timedelta(days=1)
        else:
            d = date.fromisoformat(f"{month}-01")
            while d.strftime("%Y-%m") == month:
                log = logs.get(d.isoformat(), {})
                days.append({"date": d.isoformat(), "new_done": log.get("new_done", 0),
                             "review_done": log.get("review_done", 0),
                             "target_new": 0, "in_plan": False})
                d += timedelta(days=1)
        return {"days": days, "plan": dict(plan) if plan else None}
    finally:
        db.close()


def get_progress(user_id):
    db = get_db()
    try:
        plan = get_active_plan(db, user_id)
        if not plan:
            return {"plan": None}
        dist = {"new": 0, "learning": 0, "mastered": 0, "skipped": 0}
        for r in db.execute("""SELECT COALESCE(s.state,'new') st, COUNT(*) c
                               FROM plan_words p
                               LEFT JOIN user_word_state s ON s.word_id=p.word_id AND s.user_id=?
                               WHERE p.plan_id=? GROUP BY st""", (user_id, plan["id"])):
            dist[r["st"]] = r["c"]
        streak = 0
        d = _today()
        while True:
            log = db.execute("SELECT new_done, review_done FROM daily_logs WHERE user_id=? AND date=?",
                             (user_id, d.isoformat())).fetchone()
            # 纯复习日（计划收尾期/新词配额用尽）也算打卡——streak 不因"今天没新词"而断
            if log and (log["new_done"] > 0 or (log["review_done"] or 0) > 0):
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        return {"plan": dict(plan), "dist": dist, "streak": streak,
                "learned_total": dist["mastered"] + dist["learning"]}
    finally:
        db.close()

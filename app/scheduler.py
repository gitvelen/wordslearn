"""调度核心 v4.2：计划生成（全局词频序）、每日队列（热身轮+新词4题+复习轮+巩固轮供题）、
答题状态机、表格确认流程（取代旧 spot 抽查+全量摸底）。

状态机: 首学 stage=1,next=D+2 → 复习答对 stage=2,next=复习日+5
→ 答对 stage=3,next=复习日+12 → 答对 mastered（全对时即首学后第 2/7/19 天）。
复习答错: 部分重置（v3.2）——stage-3 降一级回 stage-2，stage-1/2 回 stage-1 完整回炉，
next 均 D+2；audit 失败仍完整回炉（长间隔遗忘是强证据）。当日重现由前端推回队尾。
晋级证据阶梯（v3.2，判卷与练习分离）：stage-1 用 d2 zh_word（再认）、stage-2 用 d5 cloze
（语境·独占 fresh——D+0 用词关不再消耗，旧实现每词仅 1 道 cloze 被 D+0/D+2 两次借用，
100% 同题且实测判据与两天前同题表现零相关）、stage-3 用 d12 spell（自由拼写·0 猜测底噪）。
junior 不变（再认管线，无 cloze/spell）。
掌握词每日抽检（v2.6 方案A）：audit_schedule 当日随机抽 AUDIT_N 个已掌握词混入复习队列，
答对无事、答错打回 D+2 重走——掌握后仍有留存防线，且产出真实留存率数据。

v4.2 新词首学 4 题（多角度）：word_zh(形→义) + listen_zh(音→义) + zh_word(义→形)
+ spell(音→形·当天听写)；junior 为 word_zh + listen_zh + emoji(听音选词，无 emoji
词退化 zh_word)。巩固轮（前端在主循环后编排）：每词 1 题从 d5 借 zh_word（v3.2 起
d5 cloze 收归 stage-2 评测独占），mark_used=False 不消耗正式复习配额，不进调度。

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
REST_DAYS = 12                   # 计划尾部停投新词的天数（最后一批词的 D+12 复习会落到计划期之后，
                                 # 由复习管道继续消化——复习查询不筛 plan，不随计划结束停止）
REVIEW_CAP = 60                  # 单日复习上限（积压分日消化，不改动调度状态；稳态需求≈3×daily_new）
NEW_THROTTLE_AT = 110            # 到期复习积压超过此值 → 当日新词按比例放缓（先还债再扩新）。
                                 # v3.3.6 定 110=稳态义务(3×19=57)+尖峰余量：间隔链[2,5,12]周期
                                 # 对齐的单日尖峰(实测104)不误伤，满额19；缺勤2天(≈140+)才触发缓降
AUDIT_N = 5                      # 掌握词每日抽检数
POS_RE = re.compile(r"^((?:n|v|adj|adv|prep|conj|pron|num|art|int|vt|vi|aux|abbr|modal)\.)")

STAGE_BY_REVIEW = {1: "d2", 2: "d5", 3: "d12"}   # 复习 stage → 题库阶段
JUNIOR_TYPES = ("word_zh", "listen_zh", "zh_word")   # junior 可用题型（无拼写/挖空；zh_word 为再认型可选）


# ---------- 出题引擎 ----------
def _gen_fallback_quiz(db, word, definition_zh, stage, quiz_type="word_zh"):
    """题库缺题时的兜底现算（v3.1 全题型）：word_zh/listen_zh 看词听音选义、
    zh_word 选词、spell 拼写（前端拼写格）。干扰项同词性优先+词频邻近。"""
    pos = POS_RE.match(definition_zh or "")
    short = (definition_zh or "").split(" | ")[0].strip()
    if not short:
        return None
    if quiz_type == "spell":
        return {"quiz_id": None, "quiz_type": "spell", "stage": stage,
                "stem": word, "options": [], "answer": word}
    if quiz_type == "zh_word":
        pool = []
        if pos:
            pool = [p["word"] for p in db.execute("""
                SELECT word, frq FROM words WHERE definition_zh LIKE ? AND word != ?
                ORDER BY ABS(COALESCE(frq,999999) - COALESCE((SELECT frq FROM words WHERE word=?), 999999))
                LIMIT 40""", (pos.group(1) + "%", word, word))]
        pool = [p for p in pool if abs(len(p) - len(word)) <= 6][:3]
        if len(pool) < 3:
            extra = [p["word"] for p in db.execute("""
                SELECT word FROM words WHERE definition_zh IS NOT NULL AND word != ?
                ORDER BY RANDOM() LIMIT ?""", (word, 3 - len(pool)))]
            pool += extra
        opts = list(dict.fromkeys(pool[:3] + [word]))
        if len(opts) < 4:
            return None
        import random as _r
        _r.shuffle(opts)
        return {"quiz_id": None, "quiz_type": "zh_word", "stage": stage,
                "stem": short, "options": opts, "answer": word}
    # word_zh / listen_zh / confirm 同构：释义四选一
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
    import random as _r
    _r.shuffle(opts)
    return {"quiz_id": None, "quiz_type": quiz_type, "stage": stage,
            "stem": word, "options": opts, "answer": short}


def get_quiz(db, word_id, word, definition_zh, stage, user_id=None, junior=False, mark_used=True, used=None):
    """取该词该阶段一道题：预生成题库优先（排除该用户已用），耗尽允许复用旧题
    （复习场景间隔≥3天重复出题可接受），无题库则现算兜底。
    mark_used=False 供巩固轮借用 d5/d2 题而不消耗正式复习的防重复配额。
    used：调用方（get_today_queue）预取的该用户已用题集——一次请求内多次取题
    不再各自全量拉 user_quiz_used（19 新词+60 复习的队列构建 = 170+ 次全表扫，
    11-16 实测占 /api/today 300ms 中的 230ms，且随历史线性恶化）。"""
    rows = db.execute("""SELECT q.id, q.quiz_type, q.stem, q.options, q.answer FROM word_quiz q
                         WHERE q.word_id=? AND q.stage=?""", (word_id, stage)).fetchall()
    if junior:
        rows = [r for r in rows if r["quiz_type"] in JUNIOR_TYPES]
    if rows:
        if used is None:
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


def _quiz_by_type(db, word_id, stage, quiz_type, user_id=None, mark_used=True,
                  fresh_only=False, idx=None, used=None):
    """按题型精确取题：排除已用，耗尽默认复用，题库缺该题型返回 None。
    mark_used=False 供借题（用词关）而不消耗正式复习的防重复配额。
    fresh_only=True（v3.2 评测抽题）：耗尽不复用旧题，返回 None 交上层降级——
    判卷的题绝不能是刚练过的题。
    idx（listen_def 专用）：精确取某释义号的题（idx=sense_order 原值）。
    used：调用方预取的已用题集（语义同 get_quiz，防同请求重复全量拉取）。"""
    rows = db.execute("""SELECT id, quiz_type, idx, stem, options, answer FROM word_quiz
                         WHERE word_id=? AND stage=? AND quiz_type=?""",
                      (word_id, stage, quiz_type)).fetchall()
    if idx is not None:
        rows = [r for r in rows if r["idx"] == idx]
    if not rows:
        return None
    if used is None:
        used = set()
        if user_id:
            used = {r["quiz_id"] for r in db.execute(
                "SELECT quiz_id FROM user_quiz_used WHERE user_id=?", (user_id,))}
    pool = [r for r in rows if r["id"] not in used]
    if fresh_only and not pool:
        return None
    pool = pool or rows
    r = random.choice(pool)
    if user_id and mark_used:
        db.execute("INSERT OR IGNORE INTO user_quiz_used(user_id, quiz_id) VALUES(?,?)",
                   (user_id, r["id"]))
    return {"quiz_id": r["id"], "quiz_type": r["quiz_type"], "stage": stage,
            "idx": r["idx"], "stem": r["stem"],
            "options": json.loads(r["options"]), "answer": r["answer"]}


def get_learn_quizzes(db, user_id, word_id, word, definition_zh, junior, emoji=None, used=None):
    """新词首学题（v4.2 多角度 4 题，全部不计调度）：
    standard: word_zh(形→义) + listen_zh(音→义) + zh_word(义→形) + spell(音→形·产出)
              + listen_def(听英文释义选词·义1，第 6 关用；v3.4)
    junior:   word_zh + listen_zh + emoji(听音选词·视觉)；无 emoji 词退化 zh_word。
    题库缺某题型则跳过；全缺时兜底现算 1 题。"""
    seq = ["word_zh", "listen_zh", "zh_word", "spell"] if not junior else \
          ["word_zh", "listen_zh", "emoji" if emoji else "zh_word"]
    out = []
    for t in seq:
        q = _quiz_by_type(db, word_id, "learn", t, user_id, used=used)
        if q:
            out.append(q)
        elif t != "emoji":                 # emoji 无现算干扰源；其余缺题按题型现算（v3.1）
            q = _gen_fallback_quiz(db, word, definition_zh, "learn", t)
            if q:
                out.append(q)
    if not junior:
        # listen_def·义1（第 6 关）。绝不走 fallback 现算：兜底会把 stem 生成单词本身，
        # playDef 朗读=直接报答案（假掌握，静默伤害）——缺题词（全污染 13 词/干扰不足）
        # 第 6 关自动跳过（前端 buildQueue 无题词跳关已有机制）
        q = _quiz_by_type(db, word_id, "learn", "listen_def", user_id, idx=1, used=used)
        if q:
            out.append(q)
    if not out:
        q = _gen_fallback_quiz(db, word, definition_zh, "learn")
        return [q] if q else []
    return out


def _new_word_item(db, r, user_id, junior, used=None):
    """新词条目装配：词卡信息 + 首学题 + 用词关题（new 与 learned_today 共用）。"""
    item = _word_brief(db, r, user_id, junior)
    item["quizzes"] = get_learn_quizzes(db, user_id, r["id"], r["word"],
                                        r["definition_zh"], junior, r["emoji"], used=used)
    # 用词关题（v3.2）：只借 d5 zh_word（义→形，不耗配额）。不再借 cloze——d5 每词仅 1 道
    # cloze 收归 stage-2(D+7) 正式评测独占（旧实现 D+0/D+2 100% 同题，晋级判据被污染）。
    # 无 d5 zh_word 的词该关自动跳过（前端 buildQueue 对 reinforce=None 已兼容）。
    item["reinforce"] = (
        None if junior else _quiz_by_type(db, r["id"], "d5", "zh_word", user_id,
                                          mark_used=False, used=used))
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
        grades = _confirm_grades(user["grade"])
        rows = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.definition_zh FROM words w
            JOIN word_levels l ON l.word_id=w.id AND l.grade_code IN (%s)
            WHERE NOT EXISTS (SELECT 1 FROM user_word_state s
                              WHERE s.user_id=? AND s.word_id=w.id)
            GROUP BY w.id
            ORDER BY COALESCE(w.frq, 999999), w.id LIMIT ?""" % ",".join("?" * len(grades)),
            (*grades, user_id, size)).fetchall()
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


# ---------- 自选词库（v3.0）----------
def _levenshtein(a, b):
    """编辑距离（lookup 近似候选用；词表 2.4k 规模全扫可接受）"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[lb]


def _is_zh(q):
    return any("一" <= ch <= "鿿" for ch in q)


def _phono_keys(s):
    """音标归一化：剥重音符/长音符/括号，多音标（'fɔr, for'）按逗号拆成多键。
    phonetic_us 源自 CMUdict 同一转换器，同发音归一后必相等（two='tu'/too='ˈtuː'→'tu'）。"""
    if not s:
        return set()
    return {"".join(ch for ch in part.lower() if ch not in "ˈˌː(). ")
            for part in s.split(",")}


def library_lookup(q):
    """语音/手工录入的词消歧（三级）：
    ① 英文精确命中 → match（若库内有同音词附 alternatives 供家长换选）
    ② 未命中 → 编辑距离近似候选（含中文释义反查：家长念中文）
    ③ 全落空 → not_found（前端标"待生成"，内容管线补齐）"""
    q = " ".join((q or "").strip().lower().split())
    if not q:
        return {"q": q, "candidates": []}
    db = get_db()
    try:
        if _is_zh(q):
            rows = db.execute("""SELECT id, word, phonetic_uk, phonetic_us, definition_zh, emoji
                                 FROM words WHERE definition_zh LIKE ?
                                 ORDER BY COALESCE(frq, 999999), id LIMIT 8""", (f"%{q}%",)).fetchall()
            return {"q": q, "zh": True,
                    "candidates": [dict(r) for r in rows]}
        exact = db.execute("""SELECT id, word, phonetic_uk, phonetic_us, definition_zh, emoji
                              FROM words WHERE lower(word)=lower(?)
                              LIMIT 1""", (q,)).fetchone()
        if exact:
            keys = _phono_keys(exact["phonetic_us"])
            alternatives = []
            if keys:
                for r in db.execute("""SELECT word, phonetic_us, definition_zh, emoji, frq FROM words
                                       WHERE phonetic_us IS NOT NULL AND word != ?""",
                                     (q,)).fetchall():
                    if _phono_keys(r["phonetic_us"]) & keys:
                        alternatives.append(r)
                alternatives.sort(key=lambda r: r["frq"] if r["frq"] else 999999)
                alternatives = alternatives[:5]
            out = {"q": q, "match": {k: exact[k] for k in
                                     ("id", "word", "phonetic_uk", "phonetic_us", "definition_zh", "emoji")}}
            if alternatives:
                out["alternatives"] = [dict(r) for r in alternatives]
            return out
        # 近似候选：编辑距离 ≤（词长≤4 为1，否则2），按距离+词频排
        cands = []
        for r in db.execute("""SELECT id, word, phonetic_uk, phonetic_us, definition_zh, emoji, frq
                               FROM words WHERE length(word) BETWEEN ? AND ?""",
                            (max(2, len(q) - 2), len(q) + 2)).fetchall():
            d = _levenshtein(q, r["word"])
            if d <= (1 if len(q) <= 4 else 2):
                cands.append((d, r["frq"] if r["frq"] else 999999, r))
        cands.sort(key=lambda x: (x[0], x[1]))
        return {"q": q, "candidates": [dict(r) for _, _, r in cands[:8]]}
    finally:
        db.close()


def create_library(user_id, name, entries):
    """建词库：entries = [{word_id}|{word}]（word=库外词文本）。库外词即时 ECDICT 富化
    （释义/音标/词频，毫秒级）→ 库即 usable 可建计划；LLM 精修由后台管线异步补齐。"""
    if not name or not entries:
        raise ValueError("词库名和词都不能为空")
    if len(entries) > 500:
        raise ValueError("单个词库最多 500 词")
    db = get_db()
    try:
        seen, resolved, new_words = set(), [], []
        for e in entries:
            if "word_id" in e:
                wid, w = int(e["word_id"]), None
                row = db.execute("SELECT word FROM words WHERE id=?", (wid,)).fetchone()
                if not row:
                    raise ValueError(f"word_id {wid} 不存在")
            else:
                w = " ".join(str(e.get("word", "")).strip().lower().split())
                if not w:
                    continue
                row = db.execute("SELECT id FROM words WHERE word=?", (w,)).fetchone()
                if row:
                    wid = row["id"]
                else:                       # 库外词：占位入库，下方即时富化
                    cur = db.execute("INSERT INTO words(word) VALUES(?)", (w,))
                    wid = cur.lastrowid
                    new_words.append(w)
            if wid not in seen:
                seen.add(wid)
                resolved.append(wid)
        if not resolved:
            raise ValueError("没有有效词")
        cur = db.execute("""INSERT INTO word_libraries(user_id, name, source_type)
                            VALUES(?,?,'custom')""", (user_id, name))
        lib_id = cur.lastrowid
        db.executemany("INSERT INTO library_words(library_id, word_id, seq) VALUES(?,?,?)",
                       [(lib_id, wid, i + 1) for i, wid in enumerate(resolved)])
        db.commit()
        pending_senses = 0
        if new_words:                       # 库外词 ECDICT 即时富化（同步，词表即可用）
            import pipeline
            pipeline.enrich_words(new_words)
        pending_senses = db.execute("""SELECT COUNT(*) c FROM library_words lw
                                       JOIN words w ON w.id=lw.word_id
                                       WHERE lw.library_id=? AND w.definition_zh IS NULL""",
                                    (lib_id,)).fetchone()["c"]
        return {"library_id": lib_id, "total": len(resolved),
                "pending": pending_senses, "new_words": len(new_words)}
    finally:
        db.close()


def library_status(db, lib_id):
    """库状态（动态计算）：usable=全词有释义（可建计划，词典版）；
    ready=全词有精修义项（完整版）。pend_senses=待精修词数（前端进度显示）。"""
    r = db.execute("""SELECT
        SUM(CASE WHEN w.definition_zh IS NULL OR w.definition_zh='' THEN 1 ELSE 0 END) no_def,
        SUM(CASE WHEN NOT EXISTS (SELECT 1 FROM word_senses s WHERE s.word_id=lw.word_id)
                 THEN 1 ELSE 0 END) pend
        FROM library_words lw JOIN words w ON w.id=lw.word_id WHERE lw.library_id=?""",
                   (lib_id,)).fetchone()
    return {"usable": (r["no_def"] or 0) == 0, "ready": (r["pend"] or 0) == 0,
            "pend_senses": r["pend"] or 0}


def add_words_to_library(user_id, lib_id, entries):
    """库编辑：追加词（尾部，seq 续接）。返回新增数与新增的库外词数。"""
    db = get_db()
    try:
        lib = db.execute("SELECT * FROM word_libraries WHERE id=?", (lib_id,)).fetchone()
        if not lib or (lib["user_id"] and lib["user_id"] != user_id):
            raise ValueError("词库不存在")
        have = {r["word_id"] for r in db.execute(
            "SELECT word_id FROM library_words WHERE library_id=?", (lib_id,))}
        max_seq = db.execute("SELECT COALESCE(MAX(seq),0) m FROM library_words WHERE library_id=?",
                             (lib_id,)).fetchone()["m"]
        added, new_words = 0, []
        for e in entries:
            if "word_id" in e:
                wid = int(e["word_id"])
            else:
                w = " ".join(str(e.get("word", "")).strip().lower().split())
                if not w:
                    continue
                row = db.execute("SELECT id FROM words WHERE word=?", (w,)).fetchone()
                if row:
                    wid = row["id"]
                else:
                    wid = db.execute("INSERT INTO words(word) VALUES(?)", (w,)).lastrowid
                    new_words.append(w)
            if wid in have:
                continue
            have.add(wid)
            max_seq += 1
            db.execute("INSERT INTO library_words(library_id, word_id, seq) VALUES(?,?,?)",
                       (lib_id, wid, max_seq))
            added += 1
        db.commit()
        if new_words:
            import pipeline
            pipeline.enrich_words(new_words)
        return {"added": added, "new_words": len(new_words)}
    finally:
        db.close()


def remove_word_from_library(user_id, lib_id, word_id):
    """库编辑：删词（仅未被 active 计划引用的库开放——在用计划以 plan_words 快照为准）。"""
    db = get_db()
    try:
        lib = db.execute("SELECT * FROM word_libraries WHERE id=?", (lib_id,)).fetchone()
        if not lib or (lib["user_id"] and lib["user_id"] != user_id):
            raise ValueError("词库不存在")
        used = db.execute("""SELECT 1 FROM study_plans WHERE library_id=? AND status='active' LIMIT 1""",
                          (lib_id,)).fetchone()
        if used:
            raise ValueError("计划进行中的词库不能删词（可继续加词）；请先完成或放弃计划")
        db.execute("DELETE FROM library_words WHERE library_id=? AND word_id=?", (lib_id, word_id))
        db.commit()
    finally:
        db.close()


def list_libraries(user_id):
    """可选词库：预置教材库（user_id IS NULL）+ 本用户自建库。"""
    db = get_db()
    try:
        out = []
        for r in db.execute("""SELECT l.*, COUNT(lw.word_id) n FROM word_libraries l
                               LEFT JOIN library_words lw ON lw.library_id=l.id
                               WHERE l.user_id IS NULL OR l.user_id=?
                               GROUP BY l.id ORDER BY l.user_id IS NOT NULL, l.id""",
                            (user_id,)).fetchall():
            d = dict(r)
            d.update(library_status(db, d["id"]))
            out.append(d)
        return out
    finally:
        db.close()


def get_library(user_id, lib_id, with_words=True):
    db = get_db()
    try:
        r = db.execute("SELECT * FROM word_libraries WHERE id=?", (lib_id,)).fetchone()
        if not r or (r["user_id"] and r["user_id"] != user_id):
            return None
        d = dict(r)
        d.update(library_status(db, lib_id))
        d["in_use"] = bool(db.execute(
            "SELECT 1 FROM study_plans WHERE library_id=? AND status='active' LIMIT 1",
            (lib_id,)).fetchone())
        if with_words:
            d["words"] = [dict(w) for w in db.execute("""
                SELECT w.id, w.word, w.phonetic_uk, w.definition_zh, w.frq,
                       CASE WHEN EXISTS (SELECT 1 FROM word_senses s WHERE s.word_id=w.id)
                            THEN 0 ELSE 1 END AS pending
                FROM library_words lw JOIN words w ON w.id=lw.word_id
                WHERE lw.library_id=? ORDER BY lw.seq""", (lib_id,)).fetchall()]
        return d
    finally:
        db.close()


def delete_library(user_id, lib_id):
    """仅自建库可删；被 active 计划引用的不可删。"""
    db = get_db()
    try:
        r = db.execute("SELECT user_id FROM word_libraries WHERE id=?", (lib_id,)).fetchone()
        if not r or r["user_id"] != user_id:
            raise ValueError("词库不存在")
        used = db.execute("""SELECT 1 FROM study_plans WHERE library_id=? AND status='active' LIMIT 1""",
                          (lib_id,)).fetchone()
        if used:
            raise ValueError("词库正在被进行中的计划使用")
        db.execute("DELETE FROM library_words WHERE library_id=?", (lib_id,))
        db.execute("DELETE FROM word_libraries WHERE id=?", (lib_id,))
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


def create_plan(user_id, target_words, days, library_id=None):
    """v4.1：池 = 年级段内候选（无状态或 new/verify 遗留）按全局词频序取 target。
    v3.0：指定 library_id → 池 = 库内词按录入序（教材单元序），不改词频序语义。
    续订继承：到期接续且未显式传库时，沿用旧计划的库。"""
    db = get_db()
    try:
        row = db.execute("SELECT id, end_date, library_id FROM study_plans WHERE user_id=? AND status='active'",
                         (user_id,)).fetchone()
        if row:
            if row["end_date"] >= _today().isoformat():
                return None, "已有进行中的计划"
            # 已到期计划自动完结并接续新计划（同事务完成，不存在"无 active 计划"的中间态）；
            # 旧计划未走完复习周期的 learning/mastered 词状态保留——复习查询不筛 plan，
            # 尾词在新计划下照常服务，新池自动排除已学词
            db.execute("UPDATE study_plans SET status='completed' WHERE id=?", (row["id"],))
            if library_id is None:
                library_id = row["library_id"]
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if need_confirm(db, user):
            return None, "请先完成词表确认"
        if library_id:
            lib = db.execute("SELECT * FROM word_libraries WHERE id=?", (library_id,)).fetchone()
            if not lib or (lib["user_id"] and lib["user_id"] != user_id):
                return None, "词库不存在"
            if not library_status(db, library_id)["usable"]:
                return None, "词库还有词没准备好（稍等几秒重试）"
            pool = [r["id"] for r in db.execute("""
                SELECT w.id FROM library_words lw JOIN words w ON w.id=lw.word_id
                WHERE lw.library_id=?
                  AND NOT EXISTS (SELECT 1 FROM user_word_state s
                                  WHERE s.user_id=? AND s.word_id=w.id
                                    AND s.state IN ('learning','mastered','skipped'))
                ORDER BY lw.seq""", (library_id, user_id)).fetchall()]
        else:
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
        cur = db.execute("""INSERT INTO study_plans(user_id, target_words, start_date, end_date, daily_new, library_id)
                            VALUES(?,?,?,?,?,?)""",
                         (user_id, target, start.isoformat(), end.isoformat(), daily_new, library_id))
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
            "mp3": r["mp3"], "emoji": r["emoji"],
            # v4.3 新字段用 .get()：SELECT 漏列时降级为"不展示"而非 /api/today 500
            "syllables": dict(r).get("syllables"),
            "collocations": dict(r).get("collocations")}
    if with_senses:
        senses = db.execute("""SELECT sense_order, pos, definition_zh, definition_en,
                                      example_en, example_zh FROM word_senses
                               WHERE word_id=? ORDER BY sense_order""", (r["id"],)).fetchall()
        item["senses"] = [dict(s) for s in senses] if senses else None
    return item


def get_today_queue(user_id):
    """v4.1 队列：warmup（昨日词快闪）+ new（每词带 2 道首学题）+ review（每词带阶段题）。
    顺序由前端 buildQueue 整块编排（热身→新词卡连播→复习→分关测验，关内独立乱序）；后端只供原料。"""
    db = get_db()
    try:
        plan = get_active_plan(db, user_id)
        if not plan:
            return {"plan": None, "warmup": [], "new": [], "review": []}
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        junior = bool(user and user["mode"] == "junior")
        t = _today()
        t_str = t.isoformat()
        # 当日完成态（完成事件已结算/已发报）：客户端据此直呈完成屏，防同日重开/隔夜复活重放队列
        dl = db.execute("""SELECT new_done, review_done, score, report_sent FROM daily_logs
                           WHERE user_id=? AND date=?""", (user_id, t_str)).fetchone()
        done = bool(dl and (dl["score"] is not None or dl["report_sent"]))
        if done:
            # 已完成：不构建队列（省 get_quiz 配额消耗与抽检排样），客户端直呈完成屏
            return {"plan": dict(plan), "date": t_str, "done": True,
                    "done_new": dl["new_done"], "done_review": dl["review_done"],
                    "warmup": [], "new": [], "learned_today": [], "review": []}
        cutoff = (date.fromisoformat(plan["end_date"]) - timedelta(days=REST_DAYS)).isoformat()
        new_allowance = 0
        if t_str <= cutoff:
            # 勿复用 done：它已表示"当日完成态"(bool)，此处仅是已学新词数。
            # 复用会让响应返回 done=19 之类 truthy，前端误判"当日已完成"→补发完成事件
            # →误结算积分+误发日报（11-03 mily 中途退出重开即踩此雷）
            learned_new = dl["new_done"] if dl else 0
            new_allowance = max(0, plan["daily_new"] - learned_new)
        # 积压调节：到期复习过多时当日新词按比例放缓（先还债再扩新，防缺勤后滚雪球）。
        # v3.3.6 软着陆替代减半：还债速度=REVIEW_CAP-复习义务(3×new)，按比例放缓
        # 以最小丢词维持同样还债速度（减半 19→9 与 19→15 同样 3 天还清，丢词 30 vs 4，
        # 11-14 mily 9词事故）。ceil 保证渐进：due 略超线时无损，随积压加深线性收缩。
        due_n = db.execute("""SELECT COUNT(*) c FROM user_word_state
                              WHERE user_id=? AND state='learning' AND next_review_date<=?""",
                           (user_id, t_str)).fetchone()["c"]
        if due_n > NEW_THROTTLE_AT:
            new_allowance = min(new_allowance,
                                math.ceil(new_allowance * NEW_THROTTLE_AT / due_n))
        # 进关后重开不补位（11-12 mily 事故）：当日已有 gate 埋点=已进入关卡测验，
        # REVIEW_CAP 截断的尾巴 due 词不再补进队列——否则阶段倒退（退出在 gate4、
        # 重开被拉回复习做当日没见过的词，观感"从头开始"）。尾巴词如数排明天。
        # 掌握词抽检同理跳过（当日样本作废，明日首拉重新抽样，无调度损害）。
        gate_started = db.execute(
            "SELECT 1 FROM quiz_log WHERE user_id=? AND date=? AND phase='gate' LIMIT 1",
            (user_id, t_str)).fetchone() is not None
        # 热身轮：昨天首学的词（快闪巩固，不提交不调度）。
        # v3.2.1 只在当日首段会话出：当日已有学习提交（new/review）后重开不再重放热身——
        # 中途退出重开的孩子不应把 19 题热身再做一遍。
        # v3.4 补 warmup 哨兵：孩子只做完热身就退出时 daily_logs 无记录，重开整段重放
        # （多题化后 ~27 题）；当日 quiz_log 已有 warmup 作答埋点即视为已出过
        yesterday = (t - timedelta(days=1)).isoformat()
        warm_done = db.execute(
            "SELECT 1 FROM quiz_log WHERE user_id=? AND date=? AND phase='warmup' LIMIT 1",
            (user_id, t_str)).fetchone() is not None
        warmup_items = []
        # 该用户已用题集一次预取（下方全部取题共用——原先逐词全量拉，队列构建
        # O(词数×已用题数)，11-16 实测 mily 2640 已用 × 174 次取题 = 230ms 纯浪费）
        used = {r[0] for r in db.execute(
            "SELECT quiz_id FROM user_quiz_used WHERE user_id=?", (user_id,))}
        if not (warm_done or (dl and (dl["new_done"] > 0 or (dl["review_done"] or 0) > 0))):
            warmup = db.execute("""
                SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji,
                       w.syllables, w.collocations
                FROM user_word_state s JOIN words w ON w.id=s.word_id
                WHERE s.user_id=? AND s.first_learned_date=?
                ORDER BY w.id""", (user_id, yesterday)).fetchall()
            for r in warmup:
                item = _word_brief(db, r, user_id, junior, with_senses=False)
                # v3.4 热身多题：standard 多义词先出全部剩余释义的 listen_def（义2..n，
                # 按 sense_order；再巩固窗口换义听），单义词/缺题词维持 confirm word_zh 快闪。
                # 按 idx>=2 实际存在行取（污染义跳过生成后 idx 有洞，不能按连续整数推）。
                # 练习不记账（与判卷/练习分离一致）——直接 SELECT 构造，不走 _quiz_by_type。
                quizzes = []
                if not junior:
                    for q in db.execute("""SELECT id, idx, stem, options, answer FROM word_quiz
                                           WHERE word_id=? AND quiz_type='listen_def'
                                             AND stage='learn' AND idx>=2 ORDER BY idx""",
                                        (r["id"],)).fetchall():
                        quizzes.append({"quiz_id": q["id"], "quiz_type": "listen_def",
                                        "stage": "learn", "idx": q["idx"], "stem": q["stem"],
                                        "options": json.loads(q["options"]), "answer": q["answer"]})
                if not quizzes:
                    cq = get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                  "confirm", user_id, junior, used=used)
                    quizzes = [cq] if cq else []
                item["quizzes"] = quizzes
                warmup_items.append(item)
        new_words = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji,
                   w.syllables, w.collocations
            FROM plan_words p JOIN words w ON w.id=p.word_id
            WHERE p.plan_id=? AND NOT EXISTS (
                SELECT 1 FROM user_word_state s WHERE s.user_id=? AND s.word_id=w.id
                  AND s.first_learned_date IS NOT NULL)
            ORDER BY p.seq LIMIT ?""", (plan["id"], user_id, new_allowance)).fetchall()
        new_items = []
        for r in new_words:
            new_items.append(_new_word_item(db, r, user_id, junior, used=used))
        # 今日已学词（first_learned_date=今天）：前端刷新后据此重建关卡（中途退出不丢测验）
        learned_words = db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji,
                   w.syllables, w.collocations
            FROM plan_words p JOIN words w ON w.id=p.word_id
            JOIN user_word_state s ON s.user_id=? AND s.word_id=w.id
            WHERE p.plan_id=? AND s.first_learned_date=?
            ORDER BY p.seq""", (user_id, plan["id"], t_str)).fetchall()
        learned_items = [_new_word_item(db, r, user_id, junior, used=used) for r in learned_words]
        review = [] if gate_started else db.execute("""
            SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji,
                   w.syllables, w.collocations, s.stage
            FROM user_word_state s JOIN words w ON w.id=s.word_id
            WHERE s.user_id=? AND s.state='learning' AND s.next_review_date<=?
            ORDER BY s.next_review_date, w.id LIMIT ?""", (user_id, t_str, REVIEW_CAP)).fetchall()
        review_items = []
        for r in review:
            item = _word_brief(db, r, user_id, junior, with_senses=False)
            stage_key = STAGE_BY_REVIEW.get(r["stage"], "d2")
            if junior:
                item["quiz"] = get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                        stage_key, user_id, junior, used=used) or None
            else:
                # v3.2 晋级证据阶梯：每级用该间隔下最诚实（猜测底噪最低）且未练过的题。
                # stage-1(D+2) d2 zh_word 再认；stage-2(D+7) d5 cloze 语境（独占 fresh，
                # 无 cloze 词降级 d5 通用）；stage-3(D+19→mastered) d12 spell 自由拼写
                # （题库全覆盖；缺题降级 d12 通用/兜底现算）。
                # v3.2.1 评测主取题 mark_used=False——改作答时记账（submit_answer 收 quiz_id）：
                # 取题即标会在"作答前刷新/重开"时烧掉唯一 fresh 题，毕业门静默降级 MCQ
                if r["stage"] == 1:
                    item["quiz"] = (
                        _quiz_by_type(db, r["id"], "d2", "zh_word", user_id,
                                      mark_used=False, used=used)
                        or get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                    "d2", user_id, junior, used=used)) or None
                elif r["stage"] == 2:
                    item["quiz"] = (
                        _quiz_by_type(db, r["id"], "d5", "cloze", user_id,
                                      fresh_only=True, mark_used=False, used=used)
                        or get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                    "d5", user_id, junior, used=used)) or None
                else:
                    item["quiz"] = (
                        _quiz_by_type(db, r["id"], "d12", "spell", user_id,
                                      fresh_only=True, mark_used=False, used=used)
                        or get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                    "d12", user_id, junior, used=used)) or None
            review_items.append(item)
        # 掌握词每日抽检（方案A）：当日排样一次，done 标记防重出；答错由 submit_answer 打回 D+2。
        # 进关后重开（gate_started）整段跳过：抽检词混进队列同样是阶段倒退；当日样本作废
        db.execute("DELETE FROM audit_schedule WHERE user_id=? AND date<?", (user_id, t_str))
        audit_rows = []
        if not gate_started:
            audit_rows = db.execute("""SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.definition_zh, w.mp3, w.emoji,
                                              w.syllables, w.collocations
                                       FROM audit_schedule a JOIN words w ON w.id=a.word_id
                                       WHERE a.user_id=? AND a.date=? AND a.done=0 ORDER BY a.rowid""",
                                     (user_id, t_str)).fetchall()
            if not audit_rows:
                has_today = db.execute("SELECT COUNT(*) c FROM audit_schedule WHERE user_id=? AND date=?",
                                       (user_id, t_str)).fetchone()["c"]
                if not has_today:   # 首次加载才抽样（全部完成后重开不重抽）
                    audit_rows = db.execute("""SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us,
                                                      w.definition_zh, w.mp3, w.emoji,
                                                      w.syllables, w.collocations
                                               FROM user_word_state s JOIN words w ON w.id=s.word_id
                                               WHERE s.user_id=? AND s.state='mastered'
                                               ORDER BY RANDOM() LIMIT ?""",
                                             (user_id, AUDIT_N)).fetchall()
                    db.executemany("INSERT OR IGNORE INTO audit_schedule(user_id, date, word_id) VALUES(?,?,?)",
                                   [(user_id, t_str, r["id"]) for r in audit_rows])
            for r in audit_rows:
                item = _word_brief(db, r, user_id, junior, with_senses=False)
                item["quiz"] = get_quiz(db, r["id"], r["word"], r["definition_zh"],
                                        random.choice(("d2", "d5", "d12")), user_id,
                                        junior, used=used) or None
                item["audit"] = True
                review_items.append(item)
        db.commit()   # get_quiz 的 user_quiz_used 标记与抽检排样都要落盘（v2.6 修复：此前只读路径不 commit 全部回滚）
        # 进关后重开：当日仍 due 的全部排明天（含 CAP 尾巴）；否则只有 CAP 外的存量
        due_remaining = due_n if gate_started else max(0, due_n - REVIEW_CAP)
        # 当日已完成题数基数（v3.3.4 进度条防回卷）：quiz_log 行数单调只增，
        # 前端 todayPct = (baseDone+qIdx)/(baseDone+total)——重开不再从 0% 起跳
        done_items = db.execute("SELECT COUNT(*) c FROM quiz_log WHERE user_id=? AND date=?",
                                (user_id, t_str)).fetchone()["c"]
        return {"plan": dict(plan), "date": t_str, "done": done,
                "done_new": dl["new_done"] if dl else 0,
                "done_review": dl["review_done"] if dl else 0,
                "due_remaining": due_remaining,   # v3.2.1 完成屏"还有N个排明天"提示
                "done_items": done_items,
                "warmup": warmup_items, "new": new_items,
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
def submit_answer(user_id, word_id, phase, correct, quiz_type="", quiz_id=None, tele=None):
    """phase: 'learn'=新词首学, 'review'=复习测试, 'audit'=掌握词抽检。
    首学 2 题 / 热身轮 / 当日重现轮均不调用此接口——只有「进入下一个新词」与复习首答才提交。
    同时写 quiz_log（学习日报数据源；quiz_type 由前端带回）。
    quiz_id（v3.2.1）：评测题作答时记账——取题不标（防作答前刷新/重开烧掉唯一 fresh 题，
    spell 毕业门静默降级 MCQ），首答提交时才落 user_quiz_used。旧客户端不带则不标（退化无害）。
    tele（v5 过程埋点）：{latency_ms, audio_plays, typed, backspaces, empty_submits,
    blocked_taps}，仅落 quiz_log 列，不参与任何调度判定。"""
    db = get_db()
    try:
        t = _today()
        tele = tele or {}
        db.execute("""INSERT INTO quiz_log(user_id, date, phase, word_id, quiz_type, correct,
                      latency_ms, audio_plays, typed, backspaces, empty_submits, blocked_taps)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (user_id, t.isoformat(), phase, word_id, quiz_type or "", 1 if correct else 0,
                    tele.get("latency_ms"), tele.get("audio_plays"), tele.get("typed"),
                    tele.get("backspaces"), tele.get("empty_submits"), tele.get("blocked_taps")))
        if quiz_id:
            db.execute("INSERT OR IGNORE INTO user_quiz_used(user_id, quiz_id) VALUES(?,?)",
                       (user_id, quiz_id))
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
                # 答错：部分重置（v3.2）——stage-3 失败降一级回 stage-2（防 D+2~D+19 共 31 天
                # 完整回炉）；stage-1/2 失败回 stage-1 完整回炉；audit 失败仍满重置 stage-1
                # （长间隔后的遗忘是强证据，与轨迹内失败性质不同）。
                # state 回 learning——掌握词抽检答错时原 state='mastered'，不改回则永远进不了复习管道
                new_stage = 1 if (phase == "audit" or row["stage"] <= 2) else 2
                next_date = (t + timedelta(days=INTERVALS[0])).isoformat()
                db.execute("""UPDATE user_word_state SET state='learning', stage=?, next_review_date=?,
                              wrong_count=wrong_count+1
                              WHERE user_id=? AND word_id=?""",
                           (new_stage, next_date, user_id, word_id))
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
        # 复习冲刺线（v3.2.1）：learning 词的 stage 分布——进度页呈现复习里程碑，
        # 治"266 词一整坨复习中、掌握恒 0"两周看不到推进的家长观感
        stages = [0, 0, 0]
        for r in db.execute("""SELECT stage, COUNT(*) c FROM user_word_state
                               WHERE user_id=? AND state='learning' GROUP BY stage""", (user_id,)):
            if 1 <= r["stage"] <= 3:
                stages[r["stage"] - 1] = r["c"]
        streak = 0
        d = _today()
        # 今天未学不断签：无今日日志时从昨天起算（否则当天一打开 app 永远显示 0，挫伤积极性）
        today_log = db.execute("SELECT new_done, review_done FROM daily_logs WHERE user_id=? AND date=?",
                               (user_id, d.isoformat())).fetchone()
        if not (today_log and (today_log["new_done"] > 0 or (today_log["review_done"] or 0) > 0)):
            d -= timedelta(days=1)
        while True:
            log = db.execute("SELECT new_done, review_done FROM daily_logs WHERE user_id=? AND date=?",
                             (user_id, d.isoformat())).fetchone()
            # 纯复习日（计划收尾期/新词配额用尽）也算打卡——streak 不因"今天没新词"而断
            if log and (log["new_done"] > 0 or (log["review_done"] or 0) > 0):
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        p = dict(plan)
        p["expired"] = plan["end_date"] < _today().isoformat()   # 到期可续订（前端续订入口依据）
        return {"plan": p, "dist": dist, "stages": stages, "streak": streak,
                "learned_total": dist["mastered"] + dist["learning"]}
    finally:
        db.close()

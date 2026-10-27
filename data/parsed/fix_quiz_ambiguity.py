"""歧义题修复（2026-10-27 专项审查）：两步走。

1. 修 words.definition_zh 污染释义（8 条：ski/tooth/four/two/push/species 去错误义项，
   for/to/from 补全空释义）——词卡展示层，senses 出题源本就干净。
2. 为歧义题换干扰项：以 word_senses 全义项为基准，重选与题面词义项零交集、
   同词性、frq 邻近、非同族的干扰项（复用 gen_quiz.py 修正后的选词约束）。

用法: python3 -I fix_quiz_ambiguity.py [--apply]   （默认 dry-run 打印建议）
"""
import json
import random
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"
SEED = 20261027

# 人工覆盖：cloze 语境中脚本候选词仍成立（引入新歧义）的替换，人工指定
OVERRIDES = {
    1450: {"when": "apple"},      # if/or/so/because 等连词全部语境成立，只能用名词荒谬项
    14353: {"hey": "please"},     # "Oh! Nice to meet you." 自然英语
    18086: {"anyone": "herself"}, # "Is everybody at home?" 成立
    3402: {"although": "unless"}, # "While it was late, we kept working" 成立
}

# 检测自动命中之外人工追加的真实语言歧义题（cloze 语境双答案等）
MANUAL_ADD = [748, 10655, 22240, 24985, 878]
# 检测命中但人工豁免的题（语境可排除 / senses 干净按所学可区分）
MANUAL_SKIP = [22785]

# 词库（words.definition_zh）污染修复：word -> 新释义
WORDS_FIX = {
    "ski": "n. 滑雪板；滑水橇 | v. 滑雪",                       # 去"滑冰"（滑冰=skate）
    "tooth": "n. 牙齿；齿状物",                                  # 去"爱好"（sweet tooth 误拆）
    "four": "num. 四；四个 | n. 四个一组之物",                   # 去"第四"（=fourth）
    "two": "num. 二；两个 | adj. 两个的 | n. 一对",              # 去"第二"（=second）与乱码"我同"
    "push": "vt. 推 | n. 推；推动",                              # 去"攻击"等边缘义
    "species": "n. 物种；种类",                                  # 去"外形"（古义）
    "for": "prep. 为；给；因为；由于；长达（时间） | conj. 因为，由于",   # 补全空介词义
    "to": "prep. 到；向；给；对 | aux. （不定式符号）",           # 补全
    "from": "prep. 从；来自",                                    # 补全
}


def same_family(a, b):
    """与 gen_quiz.py 保持一致的同族判定（含屈折变形），另加本题库常见同根对。"""
    FAMILIES = [
        {"can", "could", "will", "would", "shall", "should", "may", "might", "must"},
        {"use", "used", "used to"}, {"do", "does", "did"}, {"go", "went", "gone", "goes"},
        {"be", "am", "is", "are", "was", "were", "been"}, {"have", "has", "had"},
        {"this", "that", "these", "those"},
        {"he", "she", "it", "they", "we", "you", "i", "me", "him", "her", "them", "us"},
        {"come", "came"}, {"see", "saw", "seen"}, {"take", "took", "taken"},
        {"get", "got", "gotten"}, {"give", "gave", "given"}, {"make", "made"},
        {"good", "better", "best"}, {"many", "much", "more", "most"},
        {"little", "less", "least"}, {"bad", "worse", "worst"},
        {"far", "farther", "further"}, {"old", "older", "elder"},
        # 本库高频互混对补充（防换汤）
        {"also", "too", "as well", "either"}, {"yes", "yeah", "yep"},
        {"hello", "hey", "hi"}, {"big", "large", "huge", "enormous"},
        {"fast", "quick", "quickly"}, {"anybody", "anyone", "anyone"},
        {"nobody", "none", "no one"}, {"though", "although"},
        {"movie", "film"}, {"private", "personal"}, {"mostly", "mainly"},
        {"organize", "arrange"}, {"entirely", "totally", "altogether", "completely"},
        {"obviously", "apparently"}, {"immediately", "at once", "right now"},
        {"when", "while"}, {"softly", "gently", "lightly"},
        {"store", "shop"}, {"website", "site"}, {"hope", "wish"},
        {"every", "each"}, {"college", "university"}, {"major", "main"},
        {"mission", "task"}, {"news", "information"}, {"wait", "wait for"},
        {"rely", "rely on", "depend", "depend on"},
        {"look after", "care for", "take care of"},
        {"take part in", "go in for", "join", "enter for"},
        {"in a minute", "immediately", "directly", "at once"},
        {"ok", "all right"}, {"elementary", "primary"}, {"reason", "cause"},
        {"quite", "rather"}, {"whether", "if"}, {"yet", "still"},
        {"again", "once more"}, {"very", "much", "quite"},
        {"state", "case", "condition"}, {"bathroom", "bath", "toilet"},
        {"through", "by", "via"}, {"in order to", "so", "so that"},
        {"because of", "as", "for", "since", "because"},
        {"people", "person", "man", "being", "human"},
        {"next to", "by", "beside", "near"},
        {"crazy", "stupid", "mad"}, {"pleasant", "enjoyable", "nice"},
        {"question", "matter", "problem"}, {"about", "on"},
        {"increase", "add"}, {"probably", "maybe", "perhaps"},
        {"especially", "above all"}, {"expect", "look forward to"},
        {"ahead", "forward", "onward"}, {"college", "university"},
    ]
    if a == b:
        return True
    al, bl = a.lower().strip(), b.lower().strip()
    for f in FAMILIES:
        if al in f and bl in f:
            return True
    for x, y in ((al, bl), (bl, al)):
        if len(x) > 3 and (y == x + "s" or y == x + "es" or y == x + "ing"
                           or y == x + "d" or y == x + "ed"
                           or (x.endswith("e") and y == x[:-1] + "ing")
                           or (x.endswith("y") and y == x[:-1] + "ied")):
            return True
    return False


def norm(s):
    return re.sub(r"\s+", "", s)


def split_zh(zh):
    zh = re.sub(r"^[a-zA-Z.]+\.?", "", (zh or "").strip())
    return {norm(p) for p in re.split(r"[；;，,、/]", zh) if norm(p)}


def main():
    apply = "--apply" in sys.argv
    random.seed(SEED)
    db = sqlite3.connect(f"file:{DB}?mode=ro" if not apply else str(DB), uri=not apply)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # ---- 载入 senses / 词频 / 词性池 ----
    word_id = {}                       # word -> id
    senses = defaultdict(list)         # word -> [(pos, zh)]
    for r in cur.execute("""SELECT wd.id, wd.word, wd.frq, s.pos, s.definition_zh
                            FROM word_senses s JOIN words wd ON s.word_id=wd.id
                            ORDER BY s.sense_order"""):
        word_id[r["word"]] = r["id"]
        senses[r["word"]].append((r["pos"], r["definition_zh"]))
    frq = {r["word"]: r["frq"] or 999999
           for r in cur.execute("SELECT word, frq FROM words")}

    def all_senses(w):
        out = set()
        for _, zh in senses.get(w, []):
            out |= split_zh(zh)
        return out

    pool = defaultdict(list)           # pos -> [(zh1, word)]，只取核心义
    for w, ss in senses.items():
        if ss:
            pool[ss[0][0] or "?"].append((ss[0][1], w))
    for k in pool:
        pool[k].sort(key=lambda t: frq.get(t[1], 999999))

    # ---- 检测待修题（与审计脚本同一逻辑）----
    bad = defaultdict(dict)            # quiz_id -> {bad_option: True}
    for r in cur.execute("""SELECT q.id, q.quiz_type, q.stem, q.options, q.answer, wd.word AS wword
                            FROM word_quiz q JOIN words wd ON wd.id=q.word_id
                            WHERE q.quiz_type IN ('word_zh','listen_zh','zh_word','emoji','cloze')"""):
        opts = json.loads(r["options"])
        a = r["answer"].strip().lower()
        if r["quiz_type"] in ("word_zh", "listen_zh"):
            a_s = split_zh(r["answer"])
            for o in opts:
                if o.strip().lower() == a:
                    continue
                if a_s & split_zh(o):
                    bad[r["id"]][o] = True
        else:
            stem_s = split_zh(r["stem"]) if r["quiz_type"] in ("zh_word", "emoji") else all_senses(r["answer"])
            for o in opts:
                if o.strip().lower() == a:
                    continue
                if stem_s & all_senses(o.strip().lower()):
                    bad[r["id"]][o] = True
    for qid in MANUAL_SKIP:
        bad.pop(qid, None)
    for qid in MANUAL_ADD:
        r = cur.execute("SELECT options, answer FROM word_quiz WHERE id=?", (qid,)).fetchone()
        if r:
            # cloze 追加题指定坏干扰（语境判定，检测器算不出）
            extra = {748: "find out", 10655: "none", 22240: "introduce",
                     24985: "disturb", 878: "feel"}[qid]
            bad.setdefault(qid, {})
            bad[qid][extra] = True

    # ---- 选替换干扰 ----
    def pick_word_distract(w, exclude):
        """英文干扰：同词性、frq 邻近、全义项零交集、非同族。"""
        pos = (senses.get(w) or [("?","")])[0][0] or "?"
        base_s = all_senses(w)
        f = frq.get(w, 999999)
        cand = [c for _, c in pool.get(pos, [])
                if c != w and c not in exclude and not same_family(w, c)
                and not (base_s & all_senses(c))]
        for window in (200, 800, 3000, 10**9):
            near = [c for c in cand if abs(frq.get(c, 999999) - f) <= window]
            if near:
                return random.choice(near)
        return None

    def pick_zh_distract(w, answer_zh, exclude):
        """中文干扰：核心义与答案义项零交集的词的核心义。"""
        a_s = split_zh(answer_zh)
        pos = (senses.get(w) or [("?","")])[0][0] or "?"
        f = frq.get(w, 999999)
        cand = [(zh, c) for zh, c in pool.get(pos, [])
                if c != w and zh not in exclude and not same_family(w, c)
                and not (a_s & split_zh(zh)) and not (all_senses(w) & all_senses(c))]
        for window in (200, 800, 3000, 10**9):
            near = [(zh, c) for zh, c in cand if abs(frq.get(c, 999999) - f) <= window]
            if near:
                return random.choice(near)
        return None

    plans = []                         # (qid, desc, old_opts, new_opts, changes)
    for qid in sorted(bad):
        r = cur.execute("""SELECT q.*, wd.word AS wword FROM word_quiz q
                           JOIN words wd ON wd.id=q.word_id WHERE q.id=?""", (qid,)).fetchone()
        opts = json.loads(r["options"])
        new_opts = opts[:]
        changes = []
        bad_opts = set(bad[qid])
        used = {o.strip().lower() for o in opts}
        for i, o in enumerate(opts):
            if o not in bad_opts:
                continue
            if o in OVERRIDES.get(qid, {}):
                new_opts[i] = OVERRIDES[qid][o]
                used.add(OVERRIDES[qid][o].strip().lower())
                changes.append((o, OVERRIDES[qid][o]))
                continue
            exclude = used | {r["answer"].strip().lower()}
            if r["quiz_type"] in ("zh_word", "emoji", "cloze"):
                repl = pick_word_distract(r["wword"], exclude)
                if repl is None:
                    changes.append((o, "<无候选!>")); continue
            else:
                got = pick_zh_distract(r["wword"], r["answer"], exclude)
                if got is None:
                    changes.append((o, "<无候选!>")); continue
                repl = got[0]
            new_opts[i] = repl
            used.add(repl.strip().lower())
            changes.append((o, repl))
        plans.append((r, opts, new_opts, changes))

    # ---- 输出 ----
    print(f"待修题 {len(plans)} 道（词库 {len(WORDS_FIX)} 条另改）\n")
    fail = 0
    for r, opts, new_opts, changes in plans:
        ok = all(nw != "<无候选!>" for _, nw in changes)
        if not ok:
            fail += 1
        mark = "OK " if ok else "!! "
        print(f"{mark}#{r['id']} [{r['quiz_type']}/{r['stage']}] {r['wword']}")
        print(f"    stem: {r['stem']!r}  answer: {r['answer']!r}")
        print(f"    old: {opts}")
        print(f"    new: {new_opts}   ({'; '.join(f'{a!r}->{b!r}' for a,b in changes)})")
    if fail:
        print(f"\n!! {fail} 题无候选干扰，需人工指定")
        return

    if not apply:
        print("\n(dry-run。加 --apply 写库)")
        return

    # ---- 应用：词库 + 题目 ----
    db.isolation_level = None
    cur.execute("BEGIN")
    for w, zh in WORDS_FIX.items():
        cur.execute("UPDATE words SET definition_zh=? WHERE word=?", (zh, w))
    for r, opts, new_opts, changes in plans:
        assert r["answer"] in new_opts and len(set(map(str, new_opts))) == 4
        cur.execute("UPDATE word_quiz SET options=? WHERE id=?",
                    (json.dumps(new_opts, ensure_ascii=False), r["id"]))
    cur.execute("COMMIT")
    print(f"\n已写库：words {len(WORDS_FIX)} 条，word_quiz {len(plans)} 道")


if __name__ == "__main__":
    main()

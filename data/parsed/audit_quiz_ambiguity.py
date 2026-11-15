"""题库歧义审计（2026-10-27 专项审查沉淀）：以 word_senses（孩子所学释义）为基准。

检查两类问题：
1. 结构：answer 不在选项 / 选项重复 / 少于 4 项 / cloze 无空格
2. 义项歧义：答案与干扰项的中文义项有交集（按 ；，、/ 拆分后集合相交）——
   孩子按所学释义选干扰项同样正确，判错即冤枉。

listen_def（听英文释义选单词，2026-11-15 新增）六规则：
  ① 题干含目标词形（听音直接报答案）  ② 干扰词任一义 def_en 归一化==题干（双正确）
  ③ 干扰词任一义 def_en Jaccard≥0.6（剥停用词）  ④ 选项词（含变形）出现在题干
  ⑤ 结构（answer∈options/4 项唯一，走通用检查）  ⑥ idx 必须等于实际 sense_order 且
  stem==对应义 def_en 原文（热身按 idx>=2 取题依赖此语义）

注意：cloze 的"语境歧义"（干扰释义与答案不同，但填入句子后自然成立，如
"Look at that ____ dog" small/white）检测器算不出，需人工审；2026-10-27 已
人工全审一轮（形容词位 135 / 句首位 146 / 副词与宾语位 83），新生成的题库
若 cloze 例句有变需重审。listen_def 的语义近义（hot/warm 类互译）同样算不出，
由 LLM judge 全量兜底（gen_out/def_review.csv）。

用法: python3 -I audit_quiz_ambiguity.py            （在 data/parsed 目录；exit 1 = 有问题）
      python3 -I audit_quiz_ambiguity.py --selftest （构造 6 类坏题断言审计器全抓）
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

# 已知豁免：语境可排除 / senses 干净按所学可区分（2026-10-27 人工判定）
KNOWN_OK = {22785}   # "We obey the stars" 语义荒谬，语境排除

STOP_TOKENS = {"a", "an", "the", "of", "to", "in", "on", "for", "with", "and", "or",
               "is", "are", "be", "am", "was", "were", "that", "this", "it", "its",
               "as", "at", "by", "from", "you", "your", "not", "no", "very", "can",
               "will", "do", "does", "did", "have", "has", "had", "one", "some",
               "something", "someone", "who", "what", "when", "where", "there"}

_EXC = re.compile(r"[.,!?;:\"()]")


def norm(s):
    return re.sub(r"\s+", "", s)


def split_zh(zh):
    zh = re.sub(r"^[a-zA-Z.]+\.?", "", (zh or "").strip())
    return {norm(p) for p in re.split(r"[；;，,、/]", zh) if norm(p)}


# ---------- listen_def 专项 ----------
def _nw(w):
    return _EXC.sub("", w.lower().strip())


def _lemma_forms(w):
    base = _nw(w)
    forms = {base}
    if base.endswith("y"):
        forms |= {base[:-1] + "ies", base[:-1] + "ier"}
    if base.endswith("e"):
        forms |= {base + "d", base + "r", base[:-1] + "ing"}
    else:
        forms |= {base + "s", base + "es", base + "ing", base + "ed", base + "er",
                  base + "est", base[:-1] + "ing", base[:-1] + "ed", base + "ly"}
    return base, forms


_pat_cache = {}


def _word_pattern(w):
    if w not in _pat_cache:
        base = _nw(w)
        if " " in base:
            parts = base.split()
            alts = [re.escape(base), re.escape(base.replace(" ", "")),
                    r"\s+".join(re.escape(p) + r"\w{0,3}" for p in parts[:-1]) + r"\s+" + re.escape(parts[-1])]
        else:
            _, forms = _lemma_forms(w)
            alts = [re.escape(f) for f in forms]
        _pat_cache[w] = re.compile(r"\b(?:" + "|".join(sorted(alts, key=len, reverse=True)) + r")\b", re.I)
    return _pat_cache[w]


def _def_norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", (s or "").casefold())).strip()


def _def_tokens(s):
    return {t for t in re.findall(r"[a-z']+", (s or "").casefold()) if t not in STOP_TOKENS and len(t) > 1}


def check_listen_def(word, idx, stem, options, answer, def_by_order, def_norms, def_tokens_by_sense):
    """listen_def 六规则（⑤结构走通用检查）。返回问题字符串列表。"""
    problems = []
    if _word_pattern(word).search(stem):
        problems.append(f"[{word}] 义{idx} 题干含目标词形（听音报答案）: {stem!r}")
    stem_norm, stem_tokens = _def_norm(stem), _def_tokens(stem)
    for o in options:
        if o == answer:
            continue
        on = _nw(o)
        if stem_norm in def_norms.get(on, set()):
            problems.append(f"[{word}] 义{idx} 干扰 {o!r} 与题干释义完全相同（双正确）")
        if any(len(t & stem_tokens) / max(1, len(t | stem_tokens)) >= 0.6
               for t in def_tokens_by_sense.get(on, [])):
            problems.append(f"[{word}] 义{idx} 干扰 {o!r} 释义与题干 Jaccard≥0.6")
        if _single_forms(on) & {t for t in re.findall(r"[a-z']+", stem.casefold())}:
            problems.append(f"[{word}] 义{idx} 干扰 {o!r}（含变形）出现在题干")
    legal = def_by_order.get(_nw(word), {})
    if idx not in legal:
        problems.append(f"[{word}] idx={idx} 不在实际 sense_order {sorted(legal)}")
    elif legal[idx] != stem:
        problems.append(f"[{word}] idx={idx} stem 与该义 def_en 原文不一致")
    return problems


def _single_forms(w):
    _, forms = _lemma_forms(w)
    return {f for f in forms if " " not in f}


def selftest():
    """构造 6 类坏题 + 1 道好题：坏题每类必须抓到、好题必须零报。"""
    def_by_order = {"trip": {1: "going to a place and coming back",
                             2: "to hit your foot and almost fall"}}
    def_norms = {"look": {_def_norm("the act of looking at something")}}
    def_tokens_by_sense = {"watch": [_def_tokens("to look at something for a time")]}
    good = dict(word="trip", idx=1, stem="going to a place and coming back",
                options=["trip", "look", "watch", "book"], answer="trip")
    bads = [
        ("①题干含目标词", dict(good, stem="a trip to a place and back")),
        ("②干扰同释义", dict(good, stem=_def_norm("the act of looking at something"))),
        ("③Jaccard高", dict(good, stem="to look at something for a time")),
        ("④干扰在题干", dict(good, stem="going to a place and coming back by boat")),
        ("⑥idx非法", dict(good, idx=3)),
        ("⑥stem不一致", dict(good, stem="going to a place")),
    ]
    fails = 0
    for name, b in bads:
        found = check_listen_def(b["word"], b["idx"], b["stem"], b["options"], b["answer"],
                                 def_by_order, def_norms, def_tokens_by_sense)
        tag = "OK" if found else "MISSED"
        fails += 0 if found else 1
        print(f"  selftest {name}: {tag}")
    ok_good = check_listen_def(good["word"], good["idx"], good["stem"], good["options"],
                               good["answer"], def_by_order, def_norms, def_tokens_by_sense)
    print(f"  selftest 好题零报: {'OK' if not ok_good else 'FALSE-POSITIVE ' + str(ok_good)}")
    if fails or ok_good:
        sys.exit(1)
    print("selftest 全部通过（6 类坏题全抓、好题零误报）")



def main():
    if "--selftest" in sys.argv[1:]:
        selftest()
        return
    db_path = DB
    if "--db" in sys.argv[1:]:          # 审计指定库（如 8091 测试副本）
        db_path = Path(sys.argv[sys.argv.index("--db") + 1])
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    senses = {}
    for r in cur.execute("""SELECT wd.word, s.definition_zh FROM word_senses s
                            JOIN words wd ON s.word_id=wd.id"""):
        senses.setdefault(r["word"], set()).update(split_zh(r["definition_zh"]))

    # listen_def 预载：词 -> {sense_order: def_en 原文}；干扰词 -> def 归一化/词元
    def_by_order, def_norms, def_tokens_by_sense = {}, {}, {}
    for r in cur.execute("""SELECT wd.word, s.sense_order, s.definition_en FROM word_senses s
                            JOIN words wd ON s.word_id=wd.id WHERE s.definition_en IS NOT NULL"""):
        def_by_order.setdefault(_nw(r["word"]), {})[r["sense_order"]] = r["definition_en"]
        def_norms.setdefault(_nw(r["word"]), set()).add(_def_norm(r["definition_en"]))
        def_tokens_by_sense.setdefault(_nw(r["word"]), []).append(_def_tokens(r["definition_en"]))

    problems = []
    for r in cur.execute("""SELECT q.id, q.quiz_type, q.stage, q.idx, q.stem, q.options, q.answer, wd.word AS wword
                            FROM word_quiz q JOIN words wd ON wd.id=q.word_id
                            WHERE q.quiz_type != 'spell'"""):
        opts = json.loads(r["options"])
        a = r["answer"].strip().lower()
        # 结构
        if a not in [o.strip().lower() for o in opts] and r["quiz_type"] != "spell":
            problems.append(f"#{r['id']} answer 不在选项: {opts}")
        if len(opts) < 4 or len({o.strip() for o in opts}) != 4:
            problems.append(f"#{r['id']} 选项数量/重复异常: {opts}")
        if r["quiz_type"] == "cloze" and not re.search(r"_+", r["stem"]):
            problems.append(f"#{r['id']} cloze 无空格: {r['stem']!r}")
        if r["quiz_type"] == "listen_def":
            problems += [f"#{r['id']} {p}" for p in check_listen_def(
                r["wword"], r["idx"], r["stem"] or "", opts, r["answer"],
                def_by_order, def_norms, def_tokens_by_sense)]
            continue
        if r["id"] in KNOWN_OK:
            continue
        # 义项歧义
        if r["quiz_type"] in ("word_zh", "listen_zh"):
            a_s = split_zh(r["answer"])
            for o in opts:
                if o.strip().lower() != a and a_s & split_zh(o):
                    problems.append(f"#{r['id']} [{r['wword']}] answer={r['answer']!r} 与干扰 {o!r} 义项交集")
        else:
            stem_s = split_zh(r["stem"]) if r["quiz_type"] in ("zh_word", "emoji") else senses.get(r["answer"].strip().lower(), set())
            for o in opts:
                if o.strip().lower() == a:
                    continue
                inter = stem_s & senses.get(o.strip().lower(), set())
                if inter:
                    problems.append(f"#{r['id']} [{r['wword']}] 题面 {r['stem']!r} 与干扰 {o!r} 义项交集 {sorted(inter)}")

    if problems:
        print(f"发现 {len(problems)} 处问题：")
        for p in problems:
            print(" ", p)
        sys.exit(1)
    print("题库审计通过：结构与义项歧义零问题。")


if __name__ == "__main__":
    main()

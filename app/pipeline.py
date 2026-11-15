"""v3.1 全自动内容管线：库外词 ECDICT 即时富化 → LLM 义项精修（校验+重试+兜底）
→ 按词确定性生成题库 → edge-tts 单词/例句音频。
入口 enrich_words()（建库/加词同步调用，毫秒级）与 run_library_pipeline()（后台任务，秒~分钟级）。
幂等：任何阶段重跑只补缺失，不重复生成。"""
import asyncio
import json
import re
import sqlite3
import threading
import urllib.request
from pathlib import Path

from database import get_db

BASE = Path(__file__).parent
DATA = BASE.parent / "data"
EC_DICT_CSV = DATA / "ext" / "ecdict" / "ecdict.csv"      # 容器内 /data/ext/...（ro 挂载）
MINI_DB = BASE / "data" / "ecdict_mini.db"                 # 转换缓存（bind mount 持久，首建后复用）

POS_OK = {"n.", "v.", "vt.", "vi.", "adj.", "adv.", "prep.", "conj.", "pron.",
          "num.", "art.", "int.", "aux.", "modal.", "phr."}
BANNED = ("wine", "beer", "alcohol", "casino", "gambl", "drug", "cocaine", "heroin",
          "weapon", "gun", "rifle", "kill", "murder", "execut", "sex", "porn",
          "politic", "election", "communist", "capitalist")


# ---------- ECDICT 富化 ----------
_mini_lock = threading.Lock()
_mini_ready = None


def _build_mini():
    """csv → 精简 SQLite（word, translation, phonetic, frq）。34 万行约 30s，一次性。"""
    import csv
    import time
    t0 = time.time()
    tmp = MINI_DB.with_suffix(".tmp")
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE ecd(word TEXT PRIMARY KEY, tr TEXT, ph TEXT, frq INTEGER)")
    rows = []
    if EC_DICT_CSV.exists():
        with open(EC_DICT_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                w = (row.get("word") or "").strip().lower()
                if not w or len(w) > 40:
                    continue
                tr = (row.get("translation") or "").split("\n")[0].strip()
                rows.append((w, tr, (row.get("phonetic") or "").strip(),
                             int(row.get("frq") or 0) or None))
        conn.executemany("INSERT OR IGNORE INTO ecd VALUES(?,?,?,?)", rows)
    conn.commit()
    conn.close()
    tmp.replace(MINI_DB)
    print(f"[pipeline] ECDICT mini 就绪: {len(rows)} 词 ({time.time()-t0:.0f}s)")


def ensure_mini(background=False):
    """保证 mini 库可用（懒建；转换期间调用方走 csv 流扫降级）。"""
    global _mini_ready
    if MINI_DB.exists():
        _mini_ready = True
        return
    if background:
        threading.Thread(target=_build_and_flag, daemon=True).start()
        return
    with _mini_lock:
        if not MINI_DB.exists():
            _build_mini()
    _mini_ready = True


def _build_and_flag():
    global _mini_ready
    with _mini_lock:
        if not MINI_DB.exists():
            try:
                _build_mini()
            except Exception as e:
                print(f"[pipeline] ECDICT mini 构建失败: {e}")
                return
    _mini_ready = True


def _ecd_lookup_stream(words):
    """mini 未就绪时的降级：csv 单遍流扫目标词。"""
    import csv
    words = {w.lower() for w in words}
    out = {}
    if not EC_DICT_CSV.exists():
        return out
    with open(EC_DICT_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            w = (row.get("word") or "").strip().lower()
            if w in words:
                out[w] = ((row.get("translation") or "").split("\n")[0].strip(),
                          (row.get("phonetic") or "").strip(),
                          int(row.get("frq") or 0) or None)
                words.discard(w)
                if not words:
                    break
    return out


def _clean_zh(tr):
    if not tr:
        return None
    tr = re.sub(r"\[[^\]]{1,6}\]", "", tr)          # [化][医][口] 标注
    tr = re.sub(r"\s{2,}", " ", tr).strip(" ;；,，")
    return tr or None


def ecd_lookup(words):
    if not words:
        return {}
    if _mini_ready or MINI_DB.exists():
        ensure_mini()
        if _mini_ready:
            conn = sqlite3.connect(MINI_DB)
            conn.row_factory = sqlite3.Row
            try:
                out = {}
                for w in words:
                    r = conn.execute("SELECT tr, ph, frq FROM ecd WHERE word=?", (w.lower(),)).fetchone()
                    if r:
                        out[w.lower()] = (r["tr"], r["ph"], r["frq"])
                return out
            finally:
                conn.close()
    return _ecd_lookup_stream(words)


def enrich_words(words):
    """库外词即时富化（同步，建库路径调用）：ECDICT 释义/英式音标/词频 → words 表补空缺。
    返回 (富化数, 仍无释义词表)。"""
    ensure_mini(background=True)                     # 首次触发后台构建，本次走流扫
    db = get_db()
    try:
        todo = [r["word"] for r in db.execute(
            """SELECT word FROM words WHERE word IN (%s)
               AND (definition_zh IS NULL OR definition_zh='' OR phonetic_uk IS NULL)"""
            % ",".join("?" * len(words)), tuple(words))]
        rows = ecd_lookup(todo)
        n = 0
        for w in todo:
            tr, ph, frq = rows.get(w.lower(), (None, None, None))
            zh = _clean_zh(tr) or "词组"
            phn = _phono_uk(ph)
            if not phn:                       # ECDICT 无音标（短语等）→ cmudict 拼读
                phn = _cmu_ipa(w)
            db.execute("""UPDATE words SET definition_zh=COALESCE(NULLIF(definition_zh,''), ?),
                          phonetic_uk=COALESCE(phonetic_uk, ?), phonetic_us=COALESCE(phonetic_us, ?),
                          frq=COALESCE(frq, ?)
                          WHERE word=?""", (zh, phn, _cmu_ipa(w), frq, w))
            n += 1
        db.commit()
        still = [w for w in todo if not rows.get(w.lower(), (None,))[0]]
        return n, still
    finally:
        db.close()


def _phono_uk(p):
    """ECDICT DJ 音标粗归一（西里尔/ASCII 重音符）；深度归一由精修后的 fix 管线负责。"""
    if not p:
        return None
    for k, v in {"ә": "ə", "є": "e", "а": "a"}.items():
        p = p.replace(k, v)
    return p.replace("'", "ˈ").replace("`", "ˌ") or None


ARP_IPA = {"AA": "ɑː", "AE": "æ", "AH": "ə", "AO": "ɔː", "AW": "aʊ", "AY": "aɪ",
           "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "EH": "e", "ER": "ɚ", "EY": "eɪ",
           "F": "f", "G": "ɡ", "HH": "h", "IH": "ɪ", "IY": "iː", "JH": "dʒ", "K": "k",
           "L": "l", "M": "m", "N": "n", "NG": "ŋ", "OW": "oʊ", "OY": "ɔɪ", "P": "p",
           "R": "r", "S": "s", "SH": "ʃ", "T": "t", "TH": "θ", "UH": "ʊ", "UW": "uː",
           "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ"}
ARP_VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY",
              "OW", "OY", "UH", "UW"}


def _cmu_ipa(word):
    """cmudict 单词→美式 IPA；多音节标主重音。短语逐词拼接。失败返回 None。"""
    try:
        import cmudict
    except ImportError:
        return None
    cmu = cmudict.dict()

    def one(tok):
        phones = cmu.get(tok)
        if not phones:
            return None
        toks = [(ARP_IPA.get(p.rstrip("012"), p.lower()),
                 p[-1] if p[-1].isdigit() else "", p.rstrip("012") in ARP_VOWELS)
                for p in phones[0]]
        if sum(1 for t in toks if t[2]) <= 1:
            return "".join(t[0] for t in toks)
        main = next((i for i, t in enumerate(toks) if t[2] and t[1] == "1"), None)
        if main is None:
            return "".join(t[0] for t in toks)
        s = main
        while s > 0 and not toks[s - 1][2]:
            s -= 1
        return "".join(t[0] for t in toks[:s]) + "ˈ" + "".join(t[0] for t in toks[s:])

    parts = [one(t) for t in re.split(r"[\s-]+", word.lower()) if t]
    if not parts or any(p is None for p in parts):
        return None
    return " ".join(parts)


# ---------- LLM 义项精修 ----------
SENSE_PROMPT = """你是儿童英语词典编辑，为 6-9 岁中国孩子（上海牛津小学低年级）的单词应用编写义项。

对下面每个词产出 1-3 个义项（单一概念的词只给 1 个，不要硬凑），JSON 数组输出，每个元素：
{{"word":"...","senses":[{{"pos":"n.|v.|adj.|adv.|prep.|conj.|pron.|num.|art.|int.|aux.|phr.","definition_zh":"核心义中文≤12字","definition_en":"儿童友好英文释义≤14词","example_en":"例句≤10个英文词且必须包含该词","example_zh":"例句中文翻译"}}]}}

硬约束：义项按儿童教材常用度降序排列（第 1 个=最常用义，孩子首学只看它）；
例句用小学常用词；不收酒/武器/赌博/毒品/暴力/政治义项；definition_zh 彼此有区分度；
只输出 JSON 数组，不要其他文字。词典参考释义（可能有噪音，仅参考）：{refs}"""

_EXC = re.compile(r"[.,!?;:\"()]")


def _norm_word(w):
    return _EXC.sub("", w.lower().strip())


def _lemma_forms(w):
    """词形归一：原形+规则变形（例句含目标词校验用）。"""
    base = _norm_word(w)
    forms = {base, base.replace(" ", "")}
    if " " in base:                                    # 短语：允许成分间插入（do my homework）
        parts = base.split()
        forms.add(r"\s+".join(re.escape(p) + r"\w{0,3}" for p in parts[:-1]) + r"\s+" + re.escape(parts[-1]))
    else:
        if base.endswith("y"):
            forms |= {base[:-1] + "ies", base[:-1] + "ier"}
        if base.endswith("e"):
            forms |= {base + "d", base + "r", base[:-1] + "ing"}
        else:
            forms |= {base + "s", base + "es", base + "ing", base + "ed", base + "d",
                      base + "er", base + "est", base[:-1] + "ing", base[:-1] + "ed",
                      base + "ly"}
    return base, forms


def _validate(word, senses):
    """简化校验器：结构/长度/例句含词/词数/语域。返回 (ok, 清洗后的 senses)。"""
    if not isinstance(senses, list) or not senses:
        return False, None
    seen_zh, out = set(), []
    _, forms = _lemma_forms(word)
    pat = re.compile(r"\b(?:" + "|".join(sorted((re.escape(f) for f in forms if not f.startswith("\\")),
                                                key=len, reverse=True)) + r")\b", re.I) \
        if any(not f.startswith("\\") for f in forms) else None
    for i, s in enumerate(senses[:3], start=1):
        zh = (s.get("definition_zh") or "").strip()
        pos = (s.get("pos") or "").strip()
        ex = (s.get("example_en") or "").strip()
        if not zh or len(zh) > 14 or pos not in POS_OK or zh in seen_zh:
            continue
        if ex:
            words_n = len(ex.split())
            if words_n > 11:
                continue
            hit = (_norm_word(word) in {(_norm_word(t)) for t in ex.split()}
                   or (pat and pat.search(ex)))
            if not hit:
                continue
        seen_zh.add(zh)
        out.append({"sense_order": i, "pos": pos, "definition_zh": zh,
                    "definition_en": (s.get("definition_en") or "").strip()[:80] or None,
                    "example_en": ex or None,
                    "example_zh": (s.get("example_zh") or "").strip() or None,
                    "confidence": "low"})           # 自动生成标 low（离线校对台可复核）
    return (len(out) > 0), out


def _parse_llm_json(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except Exception:
        return []


def gen_senses_llm(items, retries=1):
    """items: [{word, definition_zh}]。返回 {word: senses}；LLM 失败/校验不过的词给 ECDICT 兜底义。"""
    import llm
    out, failed = {}, []
    refs = {d["word"]: d.get("definition_zh") or "" for d in items}
    try:
        text = llm.chat(SENSE_PROMPT.format(refs=json.dumps(refs, ensure_ascii=False)),
                        max_tokens=4000)
        for d in _parse_llm_json(text):
            w = _norm_word(d.get("word") or "")
            ok, ss = _validate(w, d.get("senses") or [])
            if ok:
                out[w] = ss
    except Exception as e:
        print(f"[pipeline] LLM 批量失败: {e}")
    for it in items:
        w = _norm_word(it["word"])
        if w not in out:
            failed.append(it)
    for it in failed[:retries]:                      # 失败词单独重试一次（更小上下文）
        try:
            text = llm.chat(SENSE_PROMPT.format(refs=json.dumps({it["word"]: it.get("definition_zh") or ""},
                                                                ensure_ascii=False)), max_tokens=800)
            for d in _parse_llm_json(text):
                w = _norm_word(d.get("word") or "")
                ok, ss = _validate(w, d.get("senses") or [])
                if ok:
                    out[w] = ss
        except Exception:
            pass
    for it in failed:                                # 仍失败：ECDICT 释义兜底（无例句）
        w = _norm_word(it["word"])
        if w not in out:
            zh = (it.get("definition_zh") or "").strip()
            pos = zh.split(".")[0] + "." if "." in zh[:6] else "n."
            out[w] = [{"sense_order": 1, "pos": pos if pos in POS_OK else "n.",
                       "definition_zh": zh or w, "definition_en": None,
                       "example_en": None, "example_zh": None, "confidence": "low"}]
    return out


# ---------- 题库生成（确定性，移植 gen_quiz 核心规则） ----------
def _same_family(a, b):
    la, lb = _norm_word(a), _norm_word(b)
    if la == lb:
        return True
    return la.startswith(lb) or lb.startswith(la) if abs(len(la) - len(lb)) <= 3 else False


def _zh_senses(zh):
    return {s.strip() for s in re.split(r"[;；/]", zh or "") if s.strip()}


# listen_def 干扰排除用：def_en token 剥停用词（与 gen_def_quiz.py 同口径）
DEF_STOP = {"a", "an", "the", "of", "to", "in", "on", "for", "with", "and", "or",
            "is", "are", "be", "am", "was", "were", "that", "this", "it", "its",
            "as", "at", "by", "from", "you", "your", "not", "no", "very", "can",
            "will", "do", "does", "did", "have", "has", "had", "one", "some",
            "something", "someone", "who", "what", "when", "where", "there"}


def _def_norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", (s or "").casefold())).strip()


def _def_tokens(s):
    return {t for t in re.findall(r"[a-z']+", (s or "").casefold())
            if t not in DEF_STOP and len(t) > 1}


def _gen_listen_def(db, word_row, senses):
    """listen_def（听英文释义选单词）在线生成：每干净义一题，idx=sense_order 原值。
    def 维度排除完整实现（同释义/Jaccard≥0.6 剥停用词/干扰变形在题干/反向污染）；
    zh 义项交集与同族用在线简化口径，全库由 audit_quiz_ambiguity 兜底。
    释义含目标词形（污染义）跳过——听音直接报答案（离线 gen_def_quiz 同规则）。
    返回 emit 同款 6 元组；无干净义/干扰不足返回 []。"""
    wid, w = word_row["id"], word_row["word"]
    frq = word_row["frq"] or 999999
    _, tforms = _lemma_forms(w)
    talts = [re.escape(f) for f in tforms if not f.startswith("\\")]
    base = _norm_word(w)
    if " " in base:
        parts = base.split()
        talts.append(r"\s+".join(re.escape(p) + r"\w{0,3}" for p in parts[:-1])
                     + r"\s+" + re.escape(parts[-1]))
    tpat = re.compile(r"\b(?:" + "|".join(sorted(talts, key=len, reverse=True)) + r")\b", re.I)
    t_plain = {f for f in tforms if not f.startswith("\\") and " " not in f}
    my_zh = set().union(*(_zh_senses(s["definition_zh"]) for s in senses)) if senses else set()

    out = []
    for s in senses:
        den = (s["definition_en"] or "").strip()
        if not den or tpat.search(den):
            continue
        stem_tokens = {t for t in re.findall(r"[a-z']+", den.casefold())}
        tgt_norm, tgt_tokens = _def_norm(den), _def_tokens(den)
        # 候选池：同词性、frq 邻近，一次取全部义供 def 维度检查
        cands = {}
        for r in db.execute("""SELECT DISTINCT wd.id, wd.word, wd.frq FROM word_senses ss
                               JOIN words wd ON wd.id=ss.word_id
                               WHERE ss.pos=? AND wd.word != ?
                               ORDER BY ABS(COALESCE(wd.frq,999999)-?) LIMIT 80""",
                            (s["pos"] or "n.", w, frq)):
            cands[r["id"]] = {"word": r["word"], "frq": r["frq"], "zh": set(), "dens": [], "toks": []}
        if cands:
            for r in db.execute(f"""SELECT word_id, definition_zh, definition_en FROM word_senses
                                    WHERE word_id IN ({','.join('?' * len(cands))})""",
                                tuple(cands)):
                c = cands[r["word_id"]]
                c["zh"] |= _zh_senses(r["definition_zh"])
                if r["definition_en"]:
                    c["dens"].append(r["definition_en"])
                    c["toks"].append(_def_tokens(r["definition_en"]))
        ok_cands = []
        for c in cands.values():
            if _same_family(w, c["word"]) or (my_zh & c["zh"]):
                continue                                    # 同族 / zh 义项交集
            if tgt_norm in {_def_norm(d) for d in c["dens"]}:
                continue                                    # 同释义（双正确）
            if any(len(t & tgt_tokens) / max(1, len(t | tgt_tokens)) >= 0.6 for t in c["toks"]):
                continue                                    # Jaccard≥0.6
            _, cforms = _lemma_forms(c["word"])
            if {f for f in cforms if " " not in f} & stem_tokens:
                continue                                    # 干扰（含变形）在题干
            if any(tpat.search(d) for d in c["dens"]):
                continue                                    # 反向污染
            ok_cands.append(c["word"])
        if len(set(ok_cands)) < 3:
            continue                                        # 干扰枯竭宁缺毋滥（缺题关跳过）
        import random as _r
        _r.seed(hash(f"{w}|{s['sense_order']}") & 0xFFFFFFFF)
        opts = _r.sample(ok_cands, 3) + [w]
        _r.shuffle(opts)
        out.append(("listen_def", "learn", s["sense_order"], den,
                    json.dumps(opts, ensure_ascii=False), w))
    return out


def gen_quizzes_for_word(db, word_row):
    """按词生成全部题型题。已有题库题（主库 v2.8 修正版）绝不覆盖；
    已有题但缺 listen_def 且有干净义时仅补该题型（防部署窗口期录的词永不获 def 题，
    对抗审查 P1-4）。"""
    wid, w = word_row["id"], word_row["word"]
    senses = db.execute("""SELECT sense_order, pos, definition_zh, definition_en, example_en
                           FROM word_senses WHERE word_id=? ORDER BY sense_order""", (wid,)).fetchall()
    if not senses:
        return 0
    has = db.execute("SELECT 1 FROM word_quiz WHERE word_id=? LIMIT 1", (wid,)).fetchone()
    has_def = db.execute("""SELECT 1 FROM word_quiz WHERE word_id=? AND quiz_type='listen_def'
                            LIMIT 1""", (wid,)).fetchone()
    if has:
        if has_def:
            return 0
        rows = _gen_listen_def(db, word_row, senses)     # 补缺模式：只补 listen_def
        if not rows:
            return 0
        db.executemany("""INSERT OR IGNORE INTO word_quiz(word_id, quiz_type, stage, idx, stem, options, answer)
                          VALUES(?,?,?,?,?,?,?)""",
                       [(wid, *r) for r in rows])
        db.commit()
        return len(rows)
    zh1, pos1, ex1 = senses[0]["definition_zh"], senses[0]["pos"] or "n.", senses[0]["example_en"]
    frq = word_row["frq"] or 999999

    def distract_zh(n=3, exclude=()):
        pool = db.execute("""SELECT s.definition_zh zh, s.pos pos, wd.frq frq FROM word_senses s
                             JOIN words wd ON wd.id=s.word_id
                             WHERE s.sense_order=1 AND s.definition_zh IS NOT NULL
                               AND wd.word != ? AND s.definition_zh != ?
                             ORDER BY ABS(COALESCE(wd.frq,999999) - ?) LIMIT 60""",
                          (w, zh1, frq)).fetchall()
        my = _zh_senses(zh1)
        out = []
        for p in pool:
            if p["zh"] in exclude or p["zh"] == zh1:
                continue
            if _zh_senses(p["zh"]) & my:
                continue
            if _same_family(w, p["zh"]):
                pass
            out.append(p["zh"])
            if len(out) >= n:
                break
        return out

    def distract_word(n=3):
        pool = db.execute("""SELECT wd.word, wd.frq FROM word_senses s
                             JOIN words wd ON wd.id=s.word_id
                             WHERE s.sense_order=1 AND s.pos=? AND wd.word != ?
                             ORDER BY ABS(COALESCE(wd.frq,999999) - ?) LIMIT 60""",
                          (pos1, w, frq)).fetchall()
        out = [p["word"] for p in pool if not _same_family(w, p["word"])][:n]
        if len(out) < n:                    # 同词性池枯竭 → 随机词兜底（宁可异词性不可缺题）
            for p in db.execute("""SELECT word FROM words WHERE word != ?
                                   AND definition_zh IS NOT NULL ORDER BY RANDOM() LIMIT ?""",
                                (w, (n - len(out)) * 3)):
                if not _same_family(w, p["word"]) and p["word"] not in out:
                    out.append(p["word"])
                    if len(out) >= n:
                        break
        return out

    import random
    random.seed(hash(w) & 0xFFFFFFFF)                # 每词稳定种子（可复现）
    quizzes = []
    _idx = {}

    def emit(qt, stage, stem, options, answer):
        k = (qt, stage)
        _idx[k] = _idx.get(k, 0) + 1
        quizzes.append((qt, stage, _idx[k], stem, json.dumps(options, ensure_ascii=False), answer))

    dz = lambda: distract_zh()
    dw = lambda: distract_word()
    # learn 链
    opts = dz() + [zh1]
    if len(opts) >= 4:
        random.shuffle(opts)
        emit("word_zh", "learn", w, opts, zh1)
        emit("listen_zh", "learn", w, opts[:], zh1)
    opts = dw() + [w]
    if len(opts) >= 4:
        random.shuffle(opts)
        emit("zh_word", "learn", zh1, opts, w)
    emit("spell", "learn", w, [], w)
    if word_row["emoji"]:
        emit("emoji", "learn", word_row["emoji"], dw() + [w] if len(dw() + [w]) >= 4 else [], w)
    # confirm
    opts = dz() + [zh1]
    if len(opts) >= 4:
        random.shuffle(opts)
        emit("word_zh", "confirm", w, opts, zh1)
    # d2: zh_word×2（干扰必须是英文词：dz() 是中文义池，误用会让 answer 不在选项——
    # 2026-11-15 审计暴露 have lunch/do homework 短语词存量 8 题全错，此处修源头）
    for _ in range(2):
        opts = dw() + [w]
        if len(opts) >= 4:
            random.shuffle(opts)
            emit("zh_word", "d2", zh1, opts, w)
    # d5: cloze（有例句）+ zh_word
    if ex1:
        base, forms = _lemma_forms(w)
        plain = {base, base.replace(" ", "")}
        hit = any(_norm_word(t) in plain for t in ex1.split())
        pat = None
        if not hit and " " in base:         # 短语：成分逐词（允许中间夹形容词/代词）
            parts = [re.escape(p) for p in base.split()]
            pat = re.compile(r"\b" + r"(?:\s+\w{0,3})?\s+".join(parts) + r"\b", re.I)
            hit = bool(pat.search(ex1))
        elif not hit:
            pat = re.compile(r"\b(?:" + "|".join(sorted((re.escape(f) for f in forms
                         if not f.startswith("\\")), key=len, reverse=True)) + r")\b", re.I)
            hit = bool(pat.search(ex1))
        if hit:
            if pat is None:
                pat = re.compile(r"\b" + re.escape(w) + r"\w{0,2}\b", re.I)
            stem = pat.sub("____", ex1, count=1) if pat.pattern else ex1.replace(w, "____", 1)
            opts = dw() + [w]
            if len(opts) >= 4:
                random.shuffle(opts)
                emit("cloze", "d5", stem, opts, w)
    opts = dw() + [w]
    if len(opts) >= 4:
        random.shuffle(opts)
        emit("zh_word", "d5", zh1, opts, w)
    # d12: spell + zh_word
    emit("spell", "d12", w, [], w)
    opts = dw() + [w]
    if len(opts) >= 4:
        random.shuffle(opts)
        emit("zh_word", "d12", zh1, opts, w)
    # listen_def（第 6 关·听释义；无干净义词整组跳过，该关自动缺题跳关）
    quizzes.extend(_gen_listen_def(db, word_row, senses))

    db.execute("DELETE FROM word_quiz WHERE word_id=?", (wid,))
    db.execute("DELETE FROM user_quiz_used WHERE quiz_id NOT IN (SELECT id FROM word_quiz)")
    for qt, stage, idx, stem, options, answer in quizzes:
        db.execute("""INSERT INTO word_quiz(word_id, quiz_type, stage, idx, stem, options, answer)
                      VALUES(?,?,?,?,?,?,?)""",
                   (wid, qt, stage, idx, stem, options, answer))
    return len(quizzes)


# ---------- TTS ----------
VOICE = "en-US-AriaNeural"


async def _tts_one(text, dest, rate=None):
    for attempt in range(3):
        try:
            import edge_tts
            kw = {"rate": rate} if rate else {}
            c = edge_tts.Communicate(text, VOICE, **kw)
            await c.save(str(dest))
            return dest.stat().st_size > 800
        except Exception:
            await asyncio.sleep(1 + attempt)
    return False


def gen_tts_for_word(word, example=None, defs=None):
    """单词+例句+释义音频（幂等，缺啥补啥）。defs: [definition_en,...] 全部义。
    返回 (word_ok, sent_ok)。def 音失败不阻塞（前端 speakTTS(stem) 回退）。"""
    AUDIO = BASE / "data" / "audio"
    (AUDIO / "sent").mkdir(parents=True, exist_ok=True)
    (AUDIO / "def").mkdir(parents=True, exist_ok=True)

    async def run():
        w_ok = True
        f = AUDIO / f"{word}.mp3"
        if not f.exists() or f.stat().st_size < 800:
            w_ok = await _tts_one(word, f)
        s_ok = True
        if example:
            sfile = AUDIO / "sent" / f"{word}__1.mp3"
            if not sfile.exists() or sfile.stat().st_size < 800:
                s_ok = await _tts_one(example, sfile, rate="-8%")
        for i, text in enumerate(defs or [], start=1):
            dfile = AUDIO / "def" / f"{word}__{i}.mp3"
            if not dfile.exists() or dfile.stat().st_size < 800:
                await _tts_one(text, dfile, rate="-8%")
        return w_ok, s_ok

    try:
        return asyncio.run(run())
    except Exception as e:
        print(f"[pipeline] TTS {word} 失败: {e}")
        return False, False


# ---------- 编排 ----------
def run_library_pipeline(lib_id):
    """后台任务：无 senses 词 → LLM 精修 → 入库 → 生成题库 → TTS。幂等可重跑。"""
    import llm
    if llm.conf() is None:
        print("[pipeline] LLM 未配置，跳过精修（词典版仍可用）")
        return
    db = get_db()
    try:
        pend = db.execute("""SELECT w.id, w.word, w.definition_zh, w.frq, w.emoji FROM library_words lw
                             JOIN words w ON w.id=lw.word_id
                             WHERE lw.library_id=? AND NOT EXISTS
                               (SELECT 1 FROM word_senses s WHERE s.word_id=lw.word_id)
                             ORDER BY lw.seq""", (lib_id,)).fetchall()
        if not pend:
            print(f"[pipeline] 库 {lib_id} 无待精修词")
        # 分批 LLM（10 词/批）
        for i in range(0, len(pend), 10):
            batch = pend[i:i + 10]
            out = gen_senses_llm([{"word": r["word"], "definition_zh": r["definition_zh"]}
                                  for r in batch])
            for r in batch:
                ss = out.get(_norm_word(r["word"]))
                if not ss:
                    continue
                for s in ss:
                    db.execute("""INSERT INTO word_senses(word_id, sense_order, pos, definition_zh,
                                  definition_en, example_en, example_zh, confidence)
                                  VALUES(?,?,?,?,?,?,?,?)""",
                               (r["id"], s["sense_order"], s["pos"], s["definition_zh"],
                                s["definition_en"], s["example_en"], s["example_zh"],
                                s["confidence"]))
            db.commit()
            print(f"[pipeline] 库 {lib_id}: 批 {i//10+1} 精修 {len(out)}/{len(batch)} 词")
        # 词义首义回填 words.definition_zh（ECDICT 缺失/'词组'占位时 LLM 义补位）
        for r in db.execute("""SELECT w.id, w.definition_zh, s.definition_zh s1, s.pos FROM words w
                               JOIN library_words lw ON lw.word_id=w.id AND lw.library_id=?
                               JOIN word_senses s ON s.word_id=w.id AND s.sense_order=1
                               WHERE w.definition_zh IS NULL OR w.definition_zh=''
                                     OR w.definition_zh='词组'""",
                            (lib_id,)).fetchall():
            zh = (f"{r['pos']} {r['s1']}" if r["pos"] and r["pos"] != "phr." else r["s1"])
            db.execute("UPDATE words SET definition_zh=? WHERE id=?", (zh, r["id"]))
        # 题库 + TTS
        for r in db.execute("""SELECT w.id, w.word, w.definition_zh, w.frq, w.emoji FROM library_words lw
                               JOIN words w ON w.id=lw.word_id WHERE lw.library_id=?""", (lib_id,)).fetchall():
            n = gen_quizzes_for_word(db, r)
            ss = db.execute("""SELECT example_en, definition_en FROM word_senses WHERE word_id=?
                               ORDER BY sense_order""", (r["id"],)).fetchall()
            gen_tts_for_word(r["word"],
                             ss[0]["example_en"] if ss else None,
                             [x["definition_en"] for x in ss if x["definition_en"]])
            if n:
                print(f"[pipeline] {r['word']}: {n} 题 + 音频")
        db.commit()
        print(f"[pipeline] 库 {lib_id} 管线完成")
    finally:
        db.close()

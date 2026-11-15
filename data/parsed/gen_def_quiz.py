#!/usr/bin/env python3
"""listen_def（听英文释义选单词）题生成器：独立脚本，零扰动存量题库。

设计约束（对抗审查 P1-1：gen_quiz.py 全局随机流扰动 → UPSERT 静默覆盖存量题）：
- 本脚本不重跑、不覆盖 word_quiz.jsonl 任何存量行——只产出新行到 gen_out/def_quiz.jsonl，
  复核后追加到 data/parsed/word_quiz.jsonl 尾部（jsonl 追加语义安全）。
- 独立随机流 random.Random(SEED_DEF)，与 gen_quiz.py 互不干扰。
- 数据源为 DB word_senses（与运行时调度、审计同源）；idx=sense_order 原值，
  污染义（释义含目标词形）跳过不重编号——热身按 idx>=2 取题依赖此语义。

每词每干净义一题：stem=definition_en（TTS 播报与浏览器 TTS 回退源，渲染层不显示）、
options=4 个英文单词、answer=目标词。stage='learn'。

干扰项七层排除（前 3 层与 gen_quiz.py 同口径）：
  1 核心义同义（并入第 2 层集合交集判定）
  2 senses_overlap：两词全部中文义项拆分后集合相交（双答案）
  3 same_family+FAMILIES+屈折前缀（gen_quiz.py 同款）
  4 干扰词任一义 def_en 归一化 == 目标 def_en（69 组跨词重复）
  5 干扰词任一义 def_en token Jaccard>=0.6（token 先剥停用词）
  6 干扰词（含屈折变形）整词出现在目标 def_en（泄题干）
  7 干扰词任一义 def_en 含目标词形（反向污染）

用法: python3 -I gen_def_quiz.py   （data/parsed 目录；输出 gen_out/def_quiz.jsonl + 统计）
"""
import json
import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"
SEED_DEF = 20261115          # 独立种子，与 gen_quiz.py SEED 无关

# L2 人工复核回注的歧义排除对（frozenset 双向）；复核清单产出后回填再重跑。
# 持久化在 gen_out/def_exclude.json（[[word, wrong_option], ...]，双向生效），
# 由 judge 疑点回注脚本维护——LLM 判 SUSPECT/UNSTABLE 的题整组干扰换新
_DEF_EXCLUDE_FILE = BASE / "gen_out" / "def_exclude.json"
DEF_EXCLUDE_PAIRS = set()
if _DEF_EXCLUDE_FILE.exists():
    DEF_EXCLUDE_PAIRS = {tuple(sorted(p)) for p in
                         json.loads(_DEF_EXCLUDE_FILE.read_text(encoding="utf-8"))}

STOP_TOKENS = {"a", "an", "the", "of", "to", "in", "on", "for", "with", "and", "or",
               "is", "are", "be", "am", "was", "were", "that", "this", "it", "its",
               "as", "at", "by", "from", "you", "your", "not", "no", "very", "can",
               "will", "do", "does", "did", "have", "has", "had", "one", "some",
               "something", "someone", "who", "what", "when", "where", "there"}

# 与 gen_quiz.py:50-61 同款同族黑名单（互为变形/近形态的词不得互为干扰）
FAMILIES = [
    {"can", "could", "will", "would", "shall", "should", "may", "might", "must"},
    {"use", "used", "used to"}, {"do", "does", "did"}, {"go", "went", "gone", "goes"},
    {"be", "am", "is", "are", "was", "were", "been"}, {"have", "has", "had"},
    {"this", "that", "these", "those"}, {"he", "she", "it", "they", "we", "you", "i", "me", "him", "her", "them", "us"},
    {"come", "came"}, {"see", "saw", "seen"}, {"take", "took", "taken"},
    {"get", "got", "gotten"}, {"give", "gave", "given"}, {"make", "made"},
    {"good", "better", "best"}, {"many", "much", "more", "most"},
    {"little", "less", "least"}, {"bad", "worse", "worst"},
    {"far", "farther", "further"}, {"old", "older", "elder"},
]

_EXC = re.compile(r"[.,!?;:\"()]")


def _norm_word(w):
    return _EXC.sub("", w.lower().strip())


def _lemma_forms(w):
    """词形归一（自 app/pipeline.py:228-244 移植）：(base, {原形+规则变形})。"""
    base = _norm_word(w)
    forms = {base, base.replace(" ", "")}
    if " " in base:
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


_pat_cache = {}


def _word_pattern(w):
    """整词边界正则（含变形；短语允许成分间插词）。缓存复用。"""
    if w not in _pat_cache:
        base = _norm_word(w)
        if " " in base:
            parts = base.split()
            alts = [re.escape(base), re.escape(base.replace(" ", "")),
                    r"\s+".join(re.escape(p) + r"\w{0,3}" for p in parts[:-1]) + r"\s+" + re.escape(parts[-1])]
        else:
            _, forms = _lemma_forms(w)
            alts = [re.escape(f) for f in forms]
        _pat_cache[w] = re.compile(r"\b(?:" + "|".join(sorted(alts, key=len, reverse=True)) + r")\b", re.I)
    return _pat_cache[w]


def _single_forms(w):
    """单词形式集合（第 6 层题干 token 比对用，不含正则片段）。"""
    _, forms = _lemma_forms(w)
    return {f for f in forms if not f.startswith("\\") and " " not in f}


def _def_norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", (s or "").casefold())).strip()


def _def_tokens(s):
    return {t for t in re.findall(r"[a-z']+", (s or "").casefold()) if t not in STOP_TOKENS and len(t) > 1}


def norm_zh(s):
    return re.sub(r"\s+", "", s)


def zh_senses(zh):
    zh = re.sub(r"^[a-zA-Z.]+\.?", "", (zh or "").strip())
    return {norm_zh(p) for p in re.split(r"[；;，,、/]", zh) if norm_zh(p)}


def same_family(a, b):
    if a == b:
        return True
    al, bl = a.lower(), b.lower()
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


def main():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    words = {r["id"]: dict(r) for r in db.execute(
        "SELECT id, word, frq FROM words ORDER BY id")}
    senses = defaultdict(list)          # word_id -> [ {sense_order,pos,zh,en} ]
    for r in db.execute("""SELECT word_id, sense_order, pos, definition_zh, definition_en
                           FROM word_senses ORDER BY word_id, sense_order"""):
        if r["definition_en"]:
            senses[r["word_id"]].append(dict(r))

    # 预计算（确定性：按 word_id/sense_order 排序遍历）
    zh_all = {}                         # word_id -> 中文义项并集（第 2 层）
    def_norms = {}                      # word_id -> {def_en 归一化}（第 4 层）
    def_tokens_by_sense = {}            # word_id -> [tokens per sense]（第 5 层）
    def_en_all = {}                     # word_id -> [全部义 def_en 原文]（第 7 层）
    def_pool = defaultdict(list)        # pos -> [(word_id, word, frq)]
    for wid, ss in senses.items():
        w = words[wid]["word"]
        z = set()
        for s in ss:
            z |= zh_senses(s["definition_zh"])
        zh_all[wid] = z
        def_norms[wid] = {_def_norm(s["definition_en"]) for s in ss}
        def_tokens_by_sense[wid] = [_def_tokens(s["definition_en"]) for s in ss]
        def_en_all[wid] = [s["definition_en"] for s in ss]
        seen_pos = set()
        for s in ss:
            p = s["pos"] or "?"
            if (wid, p) in seen_pos:      # 同词多义同词性只在池中出现一次（防 sample 抽重）
                continue
            seen_pos.add((wid, p))
            def_pool[p].append((wid, w, words[wid]["frq"] or 999999))
    for k in def_pool:
        def_pool[k].sort(key=lambda t: (t[2], t[0]))

    out, stats = [], defaultdict(int)
    excluded_pairs = {tuple(sorted(p)) for p in DEF_EXCLUDE_PAIRS}

    def contaminated(wid, s):
        """释义含目标词形（听音直接报答案）。"""
        return bool(_word_pattern(words[wid]["word"]).search(s["definition_en"]))

    def pick_distracts(wid, s, n=3):
        """七层排除 + frq 邻近窗口。每题独立随机流（word|idx 种子）：
        DEF_EXCLUDE_PAIRS 回注重生成时只影响涉案题本身，绝不移位其他题
        （共享流会因 sample 消费次数变化牵连全部已判 OK 的题）。"""
        w = words[wid]["word"]
        rng = random.Random(f"{SEED_DEF}|{w}|{s['sense_order']}")
        tgt_norm = _def_norm(s["definition_en"])
        tgt_tokens = _def_tokens(s["definition_en"])
        tgt_pat = _word_pattern(w)
        # 第 6 层 token 口径：题干全部 token（不剥停用词）与干扰词单词形交集
        stem_all_tokens = {t for t in re.findall(r"[a-z']+", s["definition_en"].casefold())}

        def ok(cand_wid):
            if cand_wid == wid:
                return False
            cw = words[cand_wid]["word"]
            if same_family(w, cw):
                return False                                        # 第 3 层
            if zh_all[wid] & zh_all[cand_wid]:
                return False                                        # 第 1+2 层
            if (w, cw) in excluded_pairs or (cw, w) in excluded_pairs:
                return False                                        # 人工回注
            if tgt_norm in def_norms[cand_wid]:
                return False                                        # 第 4 层
            if any(len(t & tgt_tokens) / max(1, len(t | tgt_tokens)) >= 0.6
                   for t in def_tokens_by_sense[cand_wid]):
                return False                                        # 第 5 层
            if _single_forms(cw) & stem_all_tokens:
                return False                                        # 第 6 层
            if any(tgt_pat.search(d) for d in def_en_all[cand_wid]):
                return False                                        # 第 7 层
            return True

        pos = s["pos"] or "?"
        frq = words[wid]["frq"] or 999999
        cand = [t for t in def_pool.get(pos, []) if ok(t[0])]
        for window in (200, 800, 3000, 10 ** 9):
            near = [t for t in cand if abs(t[2] - frq) <= window]
            if len(near) >= n:
                return [t[1] for t in rng.sample(near, n)]
        if len(cand) >= n:                      # 全库窗口仍不足则同池全量
            return [t[1] for t in cand[:n]]
        # 跨词性兜底（宁可异词性不可缺题）
        cand2 = [t for k in def_pool for t in def_pool[k]
                 if t[0] != wid and ok(t[0]) and t[1] not in {c[1] for c in cand}]
        merged = cand + cand2
        return [t[1] for t in merged[:n]] if len(merged) >= n else []

    for wid in sorted(senses):
        ss = senses[wid]
        w = words[wid]["word"]
        clean = [s for s in ss if not contaminated(wid, s)]
        stats["污染义跳过"] += len(ss) - len(clean)
        if not clean:
            stats["全污染词"] += 1
            continue
        for s in clean:
            ds = pick_distracts(wid, s)
            if len(set(ds + [w])) != 4:
                stats["干扰不足跳过"] += 1
                continue
            qrng = random.Random(f"{SEED_DEF}|SHUFFLE|{w}|{s['sense_order']}")
            opts = ds + [w]
            qrng.shuffle(opts)
            out.append({"word": w, "quiz_type": "listen_def", "stage": "learn",
                        "idx": s["sense_order"], "stem": s["definition_en"],
                        "options": opts, "answer": w})
            stats["learn/listen_def"] += 1

    dest = BASE / "gen_out" / "def_quiz.jsonl"
    with dest.open("w", encoding="utf-8") as f:
        for q in out:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    print(f"生成 listen_def {len(out)} 题 → {dest}")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")

    db.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""L2 疑点局部重生成（v3.4 全量走查第 2 轮）：judge 判 SUSPECT/UNSTABLE 的题，
在「原干扰 3 词全部排除」约束下重选干扰组（每题独立随机流），原地替换
gen_out/def_quiz.jsonl 对应行；干扰枯竭的题剔除（宁缺毋滥）。

不动其他题（3088 道 OK 题零扰动——共享随机流整体重跑会全部变脸）。
排除对存档 gen_out/def_exclude.json（gen_def_quiz.py 后续维护重跑时同样生效）。

用法: python3 -I regen_def_suspects.py   （data/parsed 目录）
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
SRC = BASE / "gen_out" / "def_quiz.jsonl"
JUDGE = BASE / "gen_out" / "def_judge.jsonl"
EXCL = BASE / "gen_out" / "def_exclude.json"
SEED2 = 20261116

sys.path.insert(0, str(BASE))
# 复用生成器的全部排除/归一工具（同口径）
from gen_def_quiz import (STOP_TOKENS, same_family, zh_senses, _def_norm,  # noqa: E402
                           _def_tokens, _word_pattern, _single_forms)


def main():
    verdict = {}
    for l in JUDGE.read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        verdict[d["key"]] = d["v"]
    suspects = {k for k, v in verdict.items() if v in ("SUSPECT", "UNSTABLE")}

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    words = {r["id"]: dict(r) for r in db.execute("SELECT id, word, frq FROM words ORDER BY id")}
    senses = defaultdict(list)
    for r in db.execute("""SELECT word_id, sense_order, pos, definition_zh, definition_en
                           FROM word_senses ORDER BY word_id, sense_order"""):
        if r["definition_en"]:
            senses[r["word_id"]].append(dict(r))
    zh_all, def_norms, def_tokens_by_sense, def_en_all = {}, {}, {}, {}
    def_pool = defaultdict(list)
    for wid, ss in senses.items():
        w = words[wid]["word"]
        zh_all[wid] = set().union(*(zh_senses(s["definition_zh"]) for s in ss))
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

    rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines()]
    wid_by_word = {words[i]["word"]: i for i in words}
    excl_pairs = set()
    if EXCL.exists():
        excl_pairs = {tuple(sorted(p)) for p in json.loads(EXCL.read_text(encoding="utf-8"))}
    regen, dropped = 0, 0

    for i, d in enumerate(rows):
        key = f"{d['word']}|{d['idx']}"
        if key not in suspects:
            continue
        wid = wid_by_word[d["word"]]
        s = next(x for x in senses[wid] if x["sense_order"] == d["idx"])
        w = d["word"]
        tgt_norm, tgt_tokens = _def_norm(d["stem"]), _def_tokens(d["stem"])
        tgt_pat = _word_pattern(w)
        stem_tokens = {t for t in re.findall(r"[a-z']+", d["stem"].casefold())}
        old_opts = {o for o in d["options"] if o != w}          # 原干扰全部排除（换全新干扰组）

        def ok(c):
            cw = words[c[0]]["word"]
            return (c[0] != wid and not same_family(w, cw)
                    and not (zh_all[wid] & zh_all[c[0]])
                    and (w, cw) not in excl_pairs and (cw, w) not in excl_pairs
                    and cw not in old_opts
                    and tgt_norm not in def_norms[c[0]]
                    and not any(len(t & tgt_tokens) / max(1, len(t | tgt_tokens)) >= 0.6
                                for t in def_tokens_by_sense[c[0]])
                    and not (_single_forms(cw) & stem_tokens)
                    and not any(tgt_pat.search(x) for x in def_en_all[c[0]]))

        pos = s["pos"] or "?"
        frq = words[wid]["frq"] or 999999
        cand = [t for t in def_pool.get(pos, []) if ok(t)]
        pick = None
        rng = random.Random(f"{SEED2}|{w}|{d['idx']}")
        for window in (200, 800, 3000, 10 ** 9):
            near = [t for t in cand if abs(t[2] - frq) <= window]
            if len(near) >= 3:
                pick = [t[1] for t in rng.sample(near, 3)]
                break
        if not pick:
            cand2 = [t for k in def_pool for t in def_pool[k] if ok(t)
                     and t[1] not in {c[1] for c in cand}]
            merged = cand + cand2
            if len(merged) >= 3:
                pick = [t[1] for t in merged[:3]]
        if not pick:
            rows[i] = None                                          # 剔除（宁缺毋滥）
            dropped += 1
            continue
        opts = pick + [w]
        rng2 = random.Random(f"{SEED2}|SHUFFLE|{w}|{d['idx']}")
        rng2.shuffle(opts)
        for o in old_opts:
            excl_pairs.add(tuple(sorted((w, o))))
        rows[i] = {**d, "options": opts}
        regen += 1

    rows = [r for r in rows if r]
    with SRC.open("w", encoding="utf-8") as f:
        for q in rows:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    EXCL.write_text(json.dumps([list(p) for p in sorted(excl_pairs) if p[0] != p[1]],
                               ensure_ascii=False), encoding="utf-8")
    print(f"疑点 {len(suspects)}：重生成 {regen}（原干扰全部入排除对），剔除 {dropped}，"
          f"总题数 {len(rows)}；排除对存档 {len(excl_pairs)} → {EXCL.name}")
    db.close()


if __name__ == "__main__":
    main()

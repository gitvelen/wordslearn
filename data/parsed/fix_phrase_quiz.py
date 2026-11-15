#!/usr/bin/env python3
"""修复在线管线时代（v3.0）短语词存量错题（2026-11-15 listen_def 审计扩展暴露）：

A. have lunch / do homework 的 zh_word×8（d2×2/d5/d12）：pipeline.py 旧 bug 把
   中文义池 dz() 误用作 zh_word 干扰 → 选项 3 个中文 1 个英文、answer 不在选项。
B. goodbye×3：干扰 bye 与 goodbye 义项全同（再见）——双正确题，孩子选 bye 被冤枉。

源头 pipeline.py 已同批修复（dz→dw）；本脚本只重造存量行（UPDATE，quiz_id 不变）。
干扰词确定性选择：短语词优先、常用词补充；过 senses_overlap/same_family 零交集校验。

用法: python3 -I fix_phrase_quiz.py           （dry-run，打印 diff）
      python3 -I fix_phrase_quiz.py --apply   （写库）
"""
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

FIX_IDS = [26628, 26629, 26631, 26633, 26639, 26640, 26642, 26644,   # A 组错乱
           26723, 26724, 26729]                                        # B 组 bye 歧义


def zh_set(zh):
    zh = re.sub(r"^[a-zA-Z.]+\.?", "", (zh or "").strip())
    n = lambda s: re.sub(r"\s+", "", s)
    return {n(p) for p in re.split(r"[；;，,、/]", zh) if n(p)}


def same_family(a, b):
    al, bl = a.lower(), b.lower()
    if al == bl:
        return True
    for x, y in ((al, bl), (bl, al)):
        if len(x) > 3 and (y == x + "s" or y == x + "es" or y == x + "ing"
                           or y == x + "ed" or y == x + "d"):
            return True
    return False


def main():
    apply = "--apply" in sys.argv[1:]
    db = sqlite3.connect(f"file:{DB}?mode=ro" if not apply else str(DB), uri=not apply)
    db.row_factory = sqlite3.Row

    # 候选干扰池：短语词优先，常用实词（frq 小=高频，排除冠词/代词/连词/介词/助动的虚词位）补充
    pool = []
    for r in db.execute("""SELECT wd.word, wd.frq, s.definition_zh, s.pos FROM words wd
                           JOIN word_senses s ON s.word_id=wd.id AND s.sense_order=1
                           WHERE wd.frq < 4000 AND s.pos NOT IN ('art.','pron.','conj.','prep.','aux.')
                           ORDER BY wd.frq, wd.word"""):
        pool.append((r["word"], zh_set(r["definition_zh"])))
    phr = [p for p in pool if " " in p[0]]
    common = [p for p in pool if " " not in p[0]]

    def pick(target_word, target_zh_union, exclude, n, prefer_phr=True):
        src = (phr + common) if prefer_phr else (common + phr)
        out = []
        for w, zs in src:
            if w == target_word or w in exclude or same_family(target_word, w):
                continue
            if zs & target_zh_union:
                continue
            out.append(w)
            if len(out) == n:
                break
        return out

    rows = {r["id"]: r for r in db.execute(
        f"""SELECT q.id, q.quiz_type, q.stage, q.idx, q.stem, q.options, q.answer, wd.word
            FROM word_quiz q JOIN words wd ON wd.id=q.word_id WHERE q.id IN ({','.join(map(str, FIX_IDS))})""")}

    updates = []          # (id, new_options_json)
    # A 组：每词 4 题各 3 个全新英文干扰
    for word in ("have lunch", "do homework"):
        zh_union = set()
        for r in db.execute("""SELECT s.definition_zh FROM word_senses s JOIN words wd ON wd.id=s.word_id
                               WHERE wd.word=?""", (word,)):
            zh_union |= zh_set(r["definition_zh"])
        ids = sorted(i for i, r in rows.items() if r["word"] == word)
        cands = pick(word, zh_union, {word}, 12)
        assert len(cands) >= 12, f"{word} 干扰不足: {len(cands)}"
        for k, qid in enumerate(ids):
            r = rows[qid]
            opts = cands[3 * k:3 * k + 3] + [word]
            random.Random(qid).shuffle(opts)          # 以 quiz_id 定种子，可复现
            updates.append((qid, word, r, opts))
    # B 组：goodbye 三题把 bye 换成语义零交集的感叹词（人工指定，含 cloze 语境审查：
    # "____, see you tomorrow!" 填 ok/thanks 均成句=双正确，只有 please 不成立）
    REPL = {26723: "thanks",   # zh_word d2/2（已有 yeah/hello）
            26724: "please",   # cloze d5/1（已有 yes/go ahead；"Please, see you tomorrow!" 不成句）
            26729: "ok"}       # zh_word learn/1（已有 hello/hey；题面"再见"无语境约束）
    for qid, r in sorted(rows.items()):
        if r["word"] != "goodbye":
            continue
        opts = [REPL[qid] if o == "bye" else o for o in json.loads(r["options"])]
        updates.append((qid, "goodbye", r, opts))

    print("== 修复预览（dry-run）==" if not apply else "== 写库 ==")
    for qid, word, r, opts in updates:
        print(f"#{qid} [{word}] {r['quiz_type']}/{r['stage']}/{r['idx']}")
        print(f"   旧: {json.loads(r['options'])}")
        print(f"   新: {opts}")
        assert r["answer"] in opts, f"#{qid} 修复后 answer 仍不在选项"
        assert len(set(opts)) == 4, f"#{qid} 选项重复"

    if apply:
        db2 = sqlite3.connect(DB)
        for qid, _, _, opts in updates:
            db2.execute("UPDATE word_quiz SET options=? WHERE id=?",
                        (json.dumps(opts, ensure_ascii=False), qid))
        db2.commit()
        # 复核：answer 必在选项内
        bad = []
        for qid, _, _, opts in updates:
            row = db2.execute("SELECT answer FROM word_quiz WHERE id=?", (qid,)).fetchone()
            if row and row[0] not in opts:
                bad.append(qid)
        print(f"已更新 {len(updates)} 行；answer 复核问题 {len(bad)} {bad}")
        db2.close()
    else:
        print("dry-run：加 --apply 写库")
    db.close()


if __name__ == "__main__":
    main()

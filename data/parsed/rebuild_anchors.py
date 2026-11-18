#!/usr/bin/env python3
"""v4.3 权威重构：以 LDOCE 切分为主锚重建 word_anchors.jsonl（搭配产物保持不动）。

优先级：LDOCE 切分（校验过）> LLM 双跑切分（标 LLM_FALLBACK）> 无。
SUSPECT = LDOCE 与 cmudict 音节数不一致或拼接不还原（不采用，进人工清单）。
用法: python3 -I rebuild_anchors.py
"""
import csv
import json
import random
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
FIN = BASE / "gen_out" / "word_anchors.jsonl"
RAW = BASE / "gen_out" / "ldoce_raw.jsonl"
OUT = BASE / "gen_out" / "word_anchors_v2.jsonl"
REVIEW = BASE / "gen_out" / "anchors_review_v2.csv"
STATS = BASE / "gen_out" / "anchors_stats_v2.json"
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

ARP_VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY",
              "OW", "OY", "UH", "UW"}


def restore_case(word, lower_parts):
    """LDOCE 块为小写；按块长从原词切片恢复大小写（拼接已验证一致）。"""
    out, i = [], 0
    for p in lower_parts:
        out.append(word[i:i + len(p)])
        i += len(p)
    return out if "".join(out) == word else lower_parts


_CMU = None


def cmu_n(word):
    global _CMU
    if _CMU is None:
        import cmudict
        _CMU = cmudict.dict()
    phones = _CMU.get(word.lower())
    if not phones:
        return None
    return sum(1 for p in phones[0] if p.rstrip("012") in ARP_VOWELS)


def main():
    ours = {}
    for l in FIN.read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        ours[d["word"]] = d
    ldoce = {}
    for l in RAW.read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        if d["syllables"]:
            ldoce[d["word"]] = [p.lower() for p in d["syllables"]]

    final, replaced, suspect, fallback = [], [], [], []
    n_ldoce = n_fb = n_sus = 0
    all_words = sorted(set(ours) | set(ldoce))
    for w in all_words:
        o, L = ours.get(w), ldoce.get(w)
        coll = (o or {}).get("collocations")
        syll, src = None, None
        if L:
            n = cmu_n(w)
            joined_ok = "".join(L) == w.lower()
            n_ok = (n is None) or (len(L) == n)      # cmu 缺词时不拦（留人工）
            if joined_ok and n_ok:
                syll, src = restore_case(w, L), "LDOCE"
                n_ldoce += 1
                # 分歧记录：LLM 原切分不同（供人工确认替换正确）
                if o and o["syllables"] and [p.lower() for p in o["syllables"]] != L:
                    replaced.append([w, "·".join(o["syllables"]), "·".join(L)])
            else:
                n_sus += 1
                suspect.append([w, "·".join(L), f"拼接{'ok' if joined_ok else '坏'} cmu_n={n} 块数={len(L)}"])
                # SUSPECT 不用 LDOCE，退回 LLM（若有且过校验）
                if o and o["syllables"]:
                    syll, src = [p.lower() for p in o["syllables"]], "LLM_FALLBACK(suspect)"
                    n_fb += 1
        elif o and o["syllables"]:
            syll, src = [p.lower() for p in o["syllables"]], "LLM_FALLBACK"
            n_fb += 1
            fallback.append([w, "·".join(syll), "LDOCE 无词条/无切分"])
        if syll or coll:
            final.append({"word": w, "syllables": syll, "collocations": coll,
                          "_src": src})

    OUT.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in final),
                   encoding="utf-8")
    stats = {"ldoce_primary": n_ldoce, "llm_fallback": n_fb, "suspect": n_sus,
             "replaced_diff": len(replaced), "total": len(final),
             "coll_kept": sum(1 for d in final if d["collocations"])}
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))

    with REVIEW.open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["word", "ours_was", "now/detail", "tag", "note"])
        for r in replaced:
            wr.writerow([r[0], r[1], r[2], "REPLACED_BY_LDOCE", "LLM→朗文替换，请确认"])
        for r in suspect:
            wr.writerow([r[0], "", r[1], "SUSPECT", r[2]])
        for r in fallback:
            wr.writerow([r[0], "", r[1], "LLM_FALLBACK", r[2]])
        done = {r[0] for r in replaced} | {r[0] for r in suspect} | {r[0] for r in fallback}
        pool = [d for d in final if d["_src"] == "LDOCE" and d["word"] not in done]
        for d in random.sample(pool, min(100, len(pool))):
            wr.writerow([d["word"], "", "·".join(d["syllables"]), "LDOCE_SAMPLE",
                         json.dumps(d["collocations"], ensure_ascii=False)])
    print(f"产物：{OUT.name} {len(final)} 行 / {REVIEW.name}")


if __name__ == "__main__":
    main()

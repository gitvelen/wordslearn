#!/usr/bin/env python3
"""v4.3 rebuild v3：拼写教学节（orthographic-for-spelling）为目标体系。

体系背景（2026-11-18 用户质疑 piano 驱动的排查结论）：
- 朗文在线 headword 切分 = 排版断词（hyphenation），与拼写教学节大量重合但有系统分歧
  （choco·late 断词合并 vs 教学 choc·o·late；piano 朗文 pi·an·o vs 教学 pi·a·no）
- 本应用场景是 spell 拼写编码辅助 → 目标体系=拼写教学节（每块一个拼写元音单元）
- 判据（③体系定义本身，非自拟）：块剔除词尾 silent e 后元音字母组 ≤1
  （announce→nounc 1组 ✓ / chocolate 的 choco→choco 2组 ✗ / table 的 ble→bl 0组 ✓）

双源策略：
- LLM 版（v1）与朗文版一致 → 采（双源互证）
- 分歧 → 朗文版过③判据 → 采朗文并进人工清单（REPLACED，家长终审）
        朗文版不过③ → LLM 版过③ → 采 LLM（TEACHING_FIX，如 chocolate）
        两版都不过③ → 置 NULL 进清单（UNRESOLVED）
- 仅朗文 → 过③采（LDOCE_ONLY），不过③丢弃（HYPHEN_DROP）
- 仅 LLM → 保留（LLM_FALLBACK）

用法: python3 -I rebuild_anchors_v3.py
"""
import csv
import json
import random
import re
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
V1 = BASE / "gen_out" / "word_anchors_v1_llm_backup.jsonl"
RAW = BASE / "gen_out" / "ldoce_raw.jsonl"
OUT = BASE / "gen_out" / "word_anchors_v3.jsonl"
REVIEW = BASE / "gen_out" / "anchors_review_v3.csv"
STATS = BASE / "gen_out" / "anchors_stats_v3.json"
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

ARP_VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY",
              "OW", "OY", "UH", "UW"}


def cmu_n(word):
    import cmudict
    phones = cmudict.dict().get(word.lower())
    if not phones:
        return None
    return sum(1 for p in phones[0] if p.rstrip("012") in ARP_VOWELS)


def spell_ok(parts):
    """拼写教学节判据：每块剔除词尾 silent e 后元音字母组 ≤1。"""
    for p in parts:
        b = p.lower()
        if len(b) >= 3 and b.endswith("e") and b[-2] not in "aeiouy":
            b = b[:-1]                      # 词尾 silent e（VCe/VCCe/VVCCe 一律剥）
        if len(re.findall(r"[aeiouy]+", b)) > 1:
            return False
    return True


def restore_case(word, lower_parts):
    out, i = [], 0
    for p in lower_parts:
        out.append(word[i:i + len(p)])
        i += len(p)
    return out if "".join(out) == word else lower_parts


def main():
    llm = {}
    for l in V1.read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        llm[d["word"]] = d
    ldoce = {}
    for l in RAW.read_text(encoding="utf-8").splitlines():
        d = json.loads(l)
        if d["syllables"]:
            ldoce[d["word"]] = [p.lower() for p in d["syllables"]]

    final, manual = [], []      # manual: (word, ldoce, llm, decision, tag)
    stat = {"agree": 0, "diff_ldoce_ok": 0, "diff_teaching_fix": 0,
            "diff_unresolved": 0, "ldoce_only_ok": 0, "hyphen_drop": 0,
            "llm_fallback": 0}

    def emit(word, syll, coll, tag=""):
        if syll or coll:
            final.append({"word": word, "syllables": syll, "collocations": coll})
        if tag:
            stat[tag] = stat.get(tag, 0) + 1

    all_words = sorted(set(llm) | set(ldoce))
    for w in all_words:
        lo, lm = ldoce.get(w), llm.get(w)
        coll = (lm or {}).get("collocations")
        lm_syl = [p.lower() for p in lm["syllables"]] if (lm and lm["syllables"]) else None
        if lo and lm_syl:
            if lo == lm_syl:
                if spell_ok(lo):
                    emit(w, restore_case(w, lo), coll, "agree")
                else:                      # 双源同错（断词型，如 chocolate 双源都给 choco·late）
                    emit(w, None, coll, "diff_unresolved")
                    manual.append([w, "·".join(lo), "(同朗文)", "(不展示，可手切)",
                                   "UNRESOLVED_agree"])
            elif spell_ok(lo):
                emit(w, restore_case(w, lo), coll, "diff_ldoce_ok")
                manual.append([w, "·".join(lo), "·".join(lm_syl),
                               "·".join(restore_case(w, lo)), "REPLACED_ldoce_ok"])
            elif spell_ok(lm_syl):
                emit(w, restore_case(w, lm_syl), coll, "diff_teaching_fix")
                manual.append([w, "·".join(lo), "·".join(lm_syl),
                               "·".join(restore_case(w, lm_syl)), "TEACHING_FIX"])
            else:
                emit(w, None, coll, "diff_unresolved")
                manual.append([w, "·".join(lo), "·".join(lm_syl), "(不展示)", "UNRESOLVED"])
        elif lo:
            if spell_ok(lo):
                emit(w, restore_case(w, lo), coll, "ldoce_only_ok")
            else:
                emit(w, None, coll, "hyphen_drop")
                manual.append([w, "·".join(lo), "", "(不展示)", "HYPHEN_DROP"])
        else:
            emit(w, lm_syl and restore_case(w, lm_syl), coll, "llm_fallback")

    OUT.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in final),
                   encoding="utf-8")
    stat["total_syll"] = sum(1 for d in final if d["syllables"])
    stat["total"] = len(final)
    stat["coll_kept"] = sum(1 for d in final if d["collocations"])
    STATS.write_text(json.dumps(stat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stat, ensure_ascii=False))

    with REVIEW.open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["word", "ldoce版", "llm版", "定稿", "tag"])
        for row in manual:
            wr.writerow(row)
        decided = {r[0] for r in manual}
        pool = [d for d in final if d["syllables"] and d["word"] not in decided]
        for d in random.sample(pool, min(100, len(pool))):
            wr.writerow([d["word"], "", "", "·".join(d["syllables"]), "SAMPLE"])
    print(f"产物：{OUT.name} / 人工清单 {len(manual)} 词 + 100 抽样 → {REVIEW.name}")


if __name__ == "__main__":
    main()

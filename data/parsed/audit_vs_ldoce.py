#!/usr/bin/env python3
"""v4.3 权威对照审计：gen_out/word_anchors.jsonl 的 LLM 切分 vs LDOCE 抓取结果。

产物三分类：
- MATCH  两源一致（可保留）
- DIFF   不一致 → 以 LDOCE 为准替换（权威主锚）
- LDOCE_ONLY / LLM_ONLY 单侧存在
另输出全库权威切分总表（含 MANUAL/DROPPED 词的 LDOCE 捞回）。
用法: python3 -I audit_vs_ldoce.py
"""
import json
from pathlib import Path

BASE = Path(__file__).parent
FIN = BASE / "gen_out" / "word_anchors.jsonl"
RAW = BASE / "gen_out" / "ldoce_raw.jsonl"


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

    both = {w for w in ours if ours[w]["syllables"]} & set(ldoce)
    ours_only = {w for w in ours if ours[w]["syllables"]} - set(ldoce)
    ldoce_only = set(ldoce) - {w for w in ours if ours[w]["syllables"]}
    match = {w for w in both if [p.lower() for p in ours[w]["syllables"]] == ldoce[w]}
    diff = both - match

    print(f"LDOCE 抓到切分 {len(ldoce)} 词；我们有切分且 LDOCE 有 {len(both)}"
          f"（一致 {len(match)} / 分歧 {len(diff)}）")
    print(f"仅我们有切分（LDOCE 无词条或无切分）{len(ours_only)}；"
          f"仅 LDOCE 有（含 MANUAL/DROPPED 捞回）{len(ldoce_only)}")
    print("\n== 分歧样例（前 25，ours vs ldoce）==")
    for w in sorted(diff)[:25]:
        print(f"  {w:18s} {'·'.join(ours[w]['syllables']):22s} | {'·'.join(ldoce[w])}")


if __name__ == "__main__":
    main()

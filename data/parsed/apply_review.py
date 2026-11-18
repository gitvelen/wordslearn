#!/usr/bin/env python3
"""v4 终版合成：v3 基线 + 用户走查定稿 + 三项规则裁决 + 错位修复。

用户裁决（2026-11-18 会话）：
1. 双写中切为标准（朗文27词一致）→ possibility 定稿改回 pos·si·bil·i·ty
2. 词尾 -io/-ior 合并推广（韦氏 ra·dio 背书）：radio/studio/senior/period
   （junior/video 用户走查已给 ju·nior/vi·deo；behavior 已是合并形态）
3. 复合词成分切统一：somebody/anybody/anyone/anything/anywhere
   （nobody/someone/something/myself 等已是成分切）
4. later 行走查串行（写成 ju·nior）→ 修为 la·ter
用法: python3 -I apply_review.py
"""
import csv
import json
from pathlib import Path

BASE = Path(__file__).parent
V3 = BASE / "gen_out" / "word_anchors_v3.jsonl"
REVIEW = BASE / "gen_out" / "anchors_review_v3.csv"
OUT = BASE / "gen_out" / "word_anchors_v4.jsonl"

# 规则修正（词 → 新切分；None=不展示）。大小写按原词 restore。
RULE_FIX = {
    "possibility": ["pos", "si", "bil", "i", "ty"],
    "radio": ["ra", "dio"],
    "studio": ["stu", "dio"],
    "senior": ["sen", "ior"],
    "period": ["pe", "riod"],
    "later": ["la", "ter"],                    # 修走查串行
    "somebody": ["some", "body"],
    "anybody": ["any", "body"],
    "anyone": ["any", "one"],
    "anything": ["any", "thing"],
    "anywhere": ["any", "where"],
}


def restore_case(word, parts):
    out, i = [], 0
    for p in parts:
        out.append(word[i:i + len(p)])
        i += len(p)
    return out if "".join(out) == word else parts


def main():
    fin = {d["word"]: d for d in
           (json.loads(l) for l in V3.read_text(encoding="utf-8").splitlines())}

    # 1) 用户走查定稿（REPLACED/TEACHING_FIX 区的定稿列）
    applied_user = {}
    rows = list(csv.reader(open(REVIEW, encoding="utf-8-sig"), delimiter="\t"))
    for r in rows[1:]:
        if len(r) >= 5 and r[4] in ("REPLACED_ldoce_ok", "TEACHING_FIX"):
            w, final = r[0], r[3].strip()
            if not final or "不展示" in final or "手切" in final:
                continue
            if w == "later":                   # 串行错位，按 RULE_FIX
                continue
            parts = final.split("·")
            if "".join(parts).lower() == w.lower():
                applied_user[w] = restore_case(w, parts)

    # 2) 应用：v3 基线 → 用户定稿 → 规则修正
    n_user = n_rule = 0
    for w, parts in applied_user.items():
        if w in fin and fin[w]["syllables"] != parts:
            fin[w]["syllables"] = parts
            n_user += 1
    for w, parts in RULE_FIX.items():
        if w in fin:
            fin[w]["syllables"] = restore_case(w, parts)
            n_rule += 1

    # 3) 硬校验终审：拼接还原/无空块
    bad = [w for w, d in fin.items() if d["syllables"]
           and ("".join(d["syllables"]) != w or any(not p for p in d["syllables"]))]
    assert not bad, f"拼接违例: {bad}"

    OUT.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n"
                           for d in fin.values()), encoding="utf-8")
    stat = {"total": len(fin),
            "syll": sum(1 for d in fin.values() if d["syllables"]),
            "coll": sum(1 for d in fin.values() if d["collocations"]),
            "user_applied": n_user, "rule_fixed": n_rule}
    print(json.dumps(stat, ensure_ascii=False))
    print("终版变更示例：")
    for w in ("piano", "possibility", "radio", "junior", "senior", "studio",
              "period", "video", "nobody", "somebody", "anybody", "later",
              "operate", "operation", "ruler", "cinema", "England"):
        d = fin.get(w)
        print(f"  {w:14s}", "·".join(d["syllables"]) if d and d["syllables"] else "(无切分)")


if __name__ == "__main__":
    main()

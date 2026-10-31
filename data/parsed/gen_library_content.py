#!/usr/bin/env python3
"""v3.0 词库内容管线（增量）：教材库库外词 → ECDICT 补 frq/英式音标 + cmudict 补美式音标 +
v2_shard_13 义项展平并入 word_senses.jsonl + gen_input.jsonl 追加。
之后照常：gen_quiz.py 全量重跑（确定性）→ seed senses/quiz → gen_tts_audio/gen_sentence_tts。
用法: python3 -I gen_library_content.py   （在 data/parsed 目录）
"""
import csv
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from fix_uk_phonetic import fix_uk  # noqa: E402

# cmudict 缺失/短语的词手工音标（uk, us）
HAND_PHONETIC = {
    "mooncake": ("ˈmuːnkeɪk", "ˈmuːnkeɪk"),
    "hot dog": ("ˈhɒt dɒɡ", "ˈhɑːt dɔːɡ"),
    "x-ray": ("ˈeksreɪ", "ˈeksreɪ"),
    "tongue-twister": ("ˈtʌŋ ˌtwɪstə", "ˈtʌŋ ˌtwɪstər"),
    "taro": ("ˈtɑːrəʊ", "ˈtɑːroʊ"),       # cmudict 给了 EH 变体，取标准 AA
}

# ARPAbet → 美式 IPA（多音节词在主重音音节首辅音簇前标 ˈ，单音节不标）
ARP = {"AA": "ɑː", "AE": "æ", "AH": "ə", "AO": "ɔː", "AW": "aʊ", "AY": "aɪ",
       "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "EH": "e", "ER": "ɚ", "EY": "eɪ",
       "F": "f", "G": "ɡ", "HH": "h", "IH": "ɪ", "IY": "iː", "JH": "dʒ", "K": "k",
       "L": "l", "M": "m", "N": "n", "NG": "ŋ", "OW": "oʊ", "OY": "ɔɪ", "P": "p",
       "R": "r", "S": "s", "SH": "ʃ", "T": "t", "TH": "θ", "UH": "ʊ", "UW": "uː",
       "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ"}
VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY",
          "OW", "OY", "UH", "UW"}

def arpabet_to_ipa(phones):
    toks = []
    for p in phones:
        stress = p[-1] if p[-1].isdigit() else ""
        base = p.rstrip("012")
        toks.append((ARP.get(base, p.lower()), stress, base in VOWELS))
    if sum(1 for t in toks if t[2]) <= 1:
        return "".join(t[0] for t in toks)
    main = next((i for i, t in enumerate(toks) if t[2] and t[1] == "1"), None)
    if main is None:
        return "".join(t[0] for t in toks)
    s = main
    while s > 0 and not toks[s - 1][2]:
        s -= 1
    return "".join(t[0] for t in toks[:s]) + "ˈ" + "".join(t[0] for t in toks[s:])


def main():
    import cmudict
    cmu = cmudict.dict()

    # 1) 目标词 = 词库分册表与主词表求差（shanghai→Shanghai 同 import_textbooks 约定）
    sems = json.loads((BASE / "oxford_sh_semesters.json").read_text())
    targets = {w for ws in sems.values() for w in ws}
    # words 表词集从 gen_input + DB 展平既有词；这里直接读 word_senses.jsonl 的词集判定"已有内容"
    have = {json.loads(l)["word"] for l in (BASE / "word_senses.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = sorted(targets - have)
    todo = ["Shanghai" if w == "shanghai" else w for w in todo]
    todo = sorted(set(todo))
    print(f"词库待补内容: {len(todo)} 词")

    # 3) 产出：gen_input 追加行 + words 富化补丁（给 import/seed 用，直接产 UPDATE 用的 jsonl）
    shard = BASE / "gen_out/v2_shard_13.jsonl"
    senses = {json.loads(l)["word"]: json.loads(l)["senses"]
              for l in shard.read_text(encoding="utf-8").splitlines() if l.strip()}
    for w in todo:
        assert w in senses, f"v2_shard_13 缺 {w} 的义项"

    # 2) ECDICT 行（词源同 shard，不依赖 todo）
    ecd = {}
    need = {w.lower() for w in senses}
    with open(BASE.parent / "ext/ecdict/ecdict.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row["word"] or "").lower() in need:
                ecd[row["word"].lower()] = row
                need.discard(row["word"].lower())
                if not need:
                    break

    gi_lines = [l for l in (BASE / "gen_input.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    max_i = max(json.loads(l)["i"] for l in gi_lines)
    gi_have = {json.loads(l)["w"] for l in gi_lines}   # 幂等：已追加过的词跳过

    gi = open(BASE / "gen_input.jsonl", "a", encoding="utf-8")
    enrich = open(BASE / "library_enrich.jsonl", "w", encoding="utf-8")
    # 词源 = 分片本身（todo 会因 word_senses 已并入而空集，enrich 须全量可重放）
    all_new = sorted(senses)
    for i, w in enumerate(all_new, start=max_i + 1):
        row = ecd.get(w.lower(), {})
        uk_raw = HAND_PHONETIC.get(w, (row.get("phonetic") or "", ""))[0] or (row.get("phonetic") or "")
        uk = fix_uk(HAND_PHONETIC.get(w, (None, None))[0]) if w in HAND_PHONETIC else fix_uk(uk_raw)
        if w in HAND_PHONETIC:
            uk, us = HAND_PHONETIC[w]
        else:
            us = ""
            cps = cmu.get(w.lower())
            if cps:
                us = arpabet_to_ipa(cps[0])
            elif row.get("us_phonetic"):
                us = fix_uk(row["us_phonetic"])
        frq = int(row.get("frq") or 0) or None
        zh = f"{senses[w][0].get('pos') or 'n.'} {senses[w][0]['definition_zh']}"
        if w not in gi_have:
            gi.write(json.dumps({"i": i, "w": w, "zh": zh, "uk": uk, "frq": frq, "seg": "P13"},
                                ensure_ascii=False) + "\n")
        enrich.write(json.dumps({"word": w, "definition_zh": zh, "phonetic_uk": uk,
                                 "phonetic_us": us, "frq": frq}, ensure_ascii=False) + "\n")
        print(f"  {w:16} uk={uk} us={us} frq={frq}")
    gi.close(); enrich.close()

    # 4) 分片输入清单（校验器对账用；幂等追加到主清单，新词 cur 空）
    master_in = BASE / "gen_out/senses_v2_input.jsonl"
    have_in = {json.loads(l)["word"] for l in master_in.read_text(encoding="utf-8").splitlines() if l.strip()}
    with master_in.open("a", encoding="utf-8") as f:
        for w in todo:
            if w in have_in:
                continue
            row = ecd.get(w.lower(), {})
            zh = f"{senses[w][0].get('pos') or 'n.'} {senses[w][0]['definition_zh']}"
            f.write(json.dumps({"word": w, "frq": int(row.get("frq") or 0) or 0,
                                "seg": "P13", "definition_zh": zh, "cur_n": 0, "cur": []},
                               ensure_ascii=False) + "\n")
    (BASE / "gen_out/v2_shard_13_input.jsonl").unlink(missing_ok=True)

    # 5) 义项展平并入 word_senses.jsonl（同词旧行删除语义：重建文件去重）
    flat_path = BASE / "word_senses.jsonl"
    lines = [l for l in flat_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    kept, replaced = [], set(senses)
    for l in lines:
        if json.loads(l)["word"] not in replaced:
            kept.append(l)
    for w, ss in senses.items():
        for s in ss:
            kept.append(json.dumps({"word": w, **{k: v for k, v in s.items()}},
                                   ensure_ascii=False))
    flat_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    print(f"word_senses.jsonl: {len(lines)} → {len(kept)} 行（替换 {len(replaced)} 词）")


if __name__ == "__main__":
    main()

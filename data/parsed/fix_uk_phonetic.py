"""uk 音标二次归一化：修 ECDICT 风格残留（西里尔字符/ASCII冒号/DJ双元音/错位重音）。
用法: python3 -I fix_uk_phonetic.py（在 data/parsed 目录，原地修 phonetic_patch.jsonl 的 uk 列）"""
import json
import re
from pathlib import Path

F = Path(__file__).parent / "phonetic_patch.jsonl"

# 西里尔→拉丁替换表（ECDICT 混入的字符）
CYR = {"ә": "ə", "є": "e", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
       "у": "y", "х": "x", "і": "i"}
# DJ→IPA 双元音/长元音（ECDICT 老式记法 → 现代 IPA）
DI = [("e i", "eɪ"), ("ai", "aɪ"), ("ɔi", "ɔɪ"), ("au", "aʊ"), ("əu", "əʊ"),
      ("ei", "eɪ"), ("iə", "ɪə"), ("ɛə", "eə"), ("uə", "ʊə"), ("ɔ:", "ɔː"),
      ("ɑ:", "ɑː"), ("i:", "iː"), ("u:", "uː"), ("ɜ:", "ɜː"), ("æ:", "æː")]


def fix_uk(p):
    if not p:
        return p
    for k, v in CYR.items():
        p = p.replace(k, v)
    p = p.replace("'", "ˈ").replace("`", "ˌ")
    p = p.lstrip(".")            # 开头错误的前导点（operation 的 .ɒ → 去，再判重音）
    for a, b in DI:
        p = p.replace(a, b)
    p = p.replace(":", "ː")      # 其余 ASCII 冒号
    # u（非长元音前）→ ʊ、i（非长元音前）→ ɪ：先保护长元音再替换还原
    p = p.replace("uː", "⟀U").replace("u", "ʊ").replace("⟀U", "uː")
    p = p.replace("iː", "⟀I").replace("i", "ɪ").replace("⟀I", "iː")
    # 重音符号应贴在音节前：若首字符是元音且无重音标记，保持（无重音可接受）
    return p


rows = [json.loads(l) for l in F.read_text(encoding="utf-8").splitlines() if l.strip()]
changed = 0
for d in rows:
    if d.get("phonetic_uk"):
        new = fix_uk(d["phonetic_uk"])
        if new != d["phonetic_uk"]:
            d["phonetic_uk"] = new
            changed += 1
F.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in rows) + "\n", encoding="utf-8")
print(f"uk 修正 {changed}/{len(rows)} 行")
# 验证：不应再有西里尔/ASCII冒号/直引号
bad = [d for d in rows if any(c in (d.get("phonetic_uk") or "") for c in "әє'`:аорсухі")]
for d in bad[:10]:
    print("  残留:", d["word"], d["phonetic_uk"])
print("残留异常:", len(bad))
for d in rows[:3] + rows[500:503] + rows[-3:]:
    print(" ", d["word"], "|", d.get("phonetic_uk"), "|", d.get("phonetic_us"))

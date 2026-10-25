"""词形变化提取（v4.2）：ECDICT exchange → words.forms JSON
键：p过去式 d过去分词 i现在分词 3三单 s复数 r比较级 t最高级
按词的核心词性（word_senses sense_order=1）过滤——如 even(adv.) 不收 evened，
避免 even→evening 这类跨词性混淆；混合词性（book n.+v.）全收。
输出 word_forms.jsonl：{word, forms}（seed.py forms 命令导入）
"""
import csv
import json
import sqlite3
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"
ECDICT = BASE.parent / "ext" / "ecdict" / "ecdict.csv"

# exchange 键 → 词性归属（决定哪类核心词性收哪些键）
V_KEYS = ("p", "d", "i", "3")       # 动词变化
N_KEYS = ("s",)                     # 名词复数
ADJ_KEYS = ("r", "t")               # 形容词比较级/最高级


def main():
    csv.field_size_limit(10 ** 9)
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    # 词表词 → 全部义项词性集合（多词性词收全部对应变化）
    words = {}
    for r in db.execute("""SELECT w.word, s.pos FROM words w
                           JOIN word_senses s ON s.word_id=w.id"""):
        words.setdefault(r["word"], []).append(r["pos"] or "")
    db.close()

    def allowed(pos_list):
        allow = set()
        for pos in pos_list:
            if pos.startswith(("v.", "vt.", "vi.", "aux.", "modal.")):
                allow.update(V_KEYS)
            if pos.startswith("n."):
                allow.update(N_KEYS)
            if pos.startswith("adj."):
                allow.update(ADJ_KEYS)
        return allow

    out = []
    with open(ECDICT, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            w = row["word"].lower()
            if w not in words or not row.get("exchange"):
                continue
            allow = allowed(words[w])
            forms = {}
            for part in row["exchange"].split("/"):
                k, _, v = part.partition(":")
                if k in allow and v and v != w:
                    forms[k] = v
            if forms:
                out.append({"word": w, "forms": forms})

    dest = BASE / "word_forms.jsonl"
    with dest.open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    print(f"词形表: {len(out)} / {len(words)} 词 → {dest}")


if __name__ == "__main__":
    main()

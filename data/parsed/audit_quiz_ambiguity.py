"""题库歧义审计（2026-10-27 专项审查沉淀）：以 word_senses（孩子所学释义）为基准。

检查两类问题：
1. 结构：answer 不在选项 / 选项重复 / 少于 4 项 / cloze 无空格
2. 义项歧义：答案与干扰项的中文义项有交集（按 ；，、/ 拆分后集合相交）——
   孩子按所学释义选干扰项同样正确，判错即冤枉。

注意：cloze 的"语境歧义"（干扰释义与答案不同，但填入句子后自然成立，如
"Look at that ____ dog" small/white）检测器算不出，需人工审；2026-10-27 已
人工全审一轮（形容词位 135 / 句首位 146 / 副词与宾语位 83），新生成的题库
若 cloze 例句有变需重审。

用法: python3 -I audit_quiz_ambiguity.py   （在 data/parsed 目录；exit 1 = 有问题）
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

# 已知豁免：语境可排除 / senses 干净按所学可区分（2026-10-27 人工判定）
KNOWN_OK = {22785}   # "We obey the stars" 语义荒谬，语境排除


def norm(s):
    return re.sub(r"\s+", "", s)


def split_zh(zh):
    zh = re.sub(r"^[a-zA-Z.]+\.?", "", (zh or "").strip())
    return {norm(p) for p in re.split(r"[；;，,、/]", zh) if norm(p)}


def main():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    senses = {}
    for r in cur.execute("""SELECT wd.word, s.definition_zh FROM word_senses s
                            JOIN words wd ON s.word_id=wd.id"""):
        senses.setdefault(r["word"], set()).update(split_zh(r["definition_zh"]))

    problems = []
    for r in cur.execute("""SELECT q.id, q.quiz_type, q.stem, q.options, q.answer, wd.word AS wword
                            FROM word_quiz q JOIN words wd ON wd.id=q.word_id
                            WHERE q.quiz_type != 'spell'"""):
        opts = json.loads(r["options"])
        a = r["answer"].strip().lower()
        # 结构
        if a not in [o.strip().lower() for o in opts] and r["quiz_type"] != "spell":
            problems.append(f"#{r['id']} answer 不在选项: {opts}")
        if len(opts) < 4 or len({o.strip() for o in opts}) != 4:
            problems.append(f"#{r['id']} 选项数量/重复异常: {opts}")
        if r["quiz_type"] == "cloze" and not re.search(r"_+", r["stem"]):
            problems.append(f"#{r['id']} cloze 无空格: {r['stem']!r}")
        if r["id"] in KNOWN_OK:
            continue
        # 义项歧义
        if r["quiz_type"] in ("word_zh", "listen_zh"):
            a_s = split_zh(r["answer"])
            for o in opts:
                if o.strip().lower() != a and a_s & split_zh(o):
                    problems.append(f"#{r['id']} [{r['wword']}] answer={r['answer']!r} 与干扰 {o!r} 义项交集")
        else:
            stem_s = split_zh(r["stem"]) if r["quiz_type"] in ("zh_word", "emoji") else senses.get(r["answer"].strip().lower(), set())
            for o in opts:
                if o.strip().lower() == a:
                    continue
                inter = stem_s & senses.get(o.strip().lower(), set())
                if inter:
                    problems.append(f"#{r['id']} [{r['wword']}] 题面 {r['stem']!r} 与干扰 {o!r} 义项交集 {sorted(inter)}")

    if problems:
        print(f"发现 {len(problems)} 处问题：")
        for p in problems:
            print(" ", p)
        sys.exit(1)
    print("题库审计通过：结构与义项歧义零问题。")


if __name__ == "__main__":
    main()

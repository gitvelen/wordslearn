"""义项补全 pass 输入生成器（v4.2 词典式完备）：
全库词补全任务清单（当前义项数 + 已有义），LLM 分片生成的输入。
输出 gen_out/senses_v2_input.jsonl：{word, frq, definition_zh, cur_n, cur:[{pos,definition_zh}]}
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"


def build():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    words = db.execute("""SELECT w.id, w.word, w.definition_zh, w.frq,
                                 (SELECT MIN(CASE l.grade_code WHEN 'P13' THEN 1 WHEN 'P45' THEN 2
                                                     WHEN 'JUNIOR' THEN 3 ELSE 4 END)
                                  FROM word_levels l WHERE l.word_id=w.id) seg_rank,
                                 (SELECT GROUP_CONCAT(DISTINCT l.grade_code) FROM word_levels l WHERE l.word_id=w.id) segs
                          FROM words w
                          WHERE EXISTS (SELECT 1 FROM word_levels l WHERE l.word_id=w.id)
                          ORDER BY COALESCE(w.frq, 999999)""").fetchall()
    SEG = {1: "P13", 2: "P45", 3: "JUNIOR"}
    cur = defaultdict(list)
    for s in db.execute("""SELECT s.word_id, s.sense_order, s.pos, s.definition_zh
                           FROM word_senses s ORDER BY s.word_id, s.sense_order"""):
        cur[s["word_id"]].append({"pos": s["pos"], "definition_zh": s["definition_zh"]})
    out = []
    for w in words:
        out.append({"word": w["word"], "frq": w["frq"], "seg": SEG.get(w["seg_rank"], "JUNIOR"),
                    "definition_zh": w["definition_zh"],
                    "cur_n": len(cur.get(w["id"], [])),
                    "cur": cur.get(w["id"], [])})
    with (BASE / "gen_out" / "senses_v2_input.jsonl").open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    n1 = sum(1 for o in out if o["cur_n"] == 1)
    print(f"输入清单: {len(out)} 词（单义 {n1} / {len(out)} = {n1/len(out):.0%}）")


if __name__ == "__main__":
    build()

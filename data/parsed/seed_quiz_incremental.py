#!/usr/bin/env python3
"""增量题库导入（v3.0 教材词专用）：只插入新词的题，不动存量。
替代 seed.py quiz 的全清重灌——存量题 quiz_id 保持稳定，用户 user_quiz_used
防重出记账完整保留（mily 的 706 条不受影响）。
用法: WL_DB=... python3 -I seed_quiz_incremental.py   （在 data/parsed 目录）
"""
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent            # data/parsed
DB = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE.parents[1] / "app/data/wordslearn.db"

db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

before = db.execute("SELECT COUNT(*) c FROM word_quiz").fetchone()["c"]
for line in (BASE / "word_quiz.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    d = json.loads(line)
    wid = db.execute("SELECT id FROM words WHERE word=?", (d["word"],)).fetchone()
    if not wid:
        continue
    db.execute("""INSERT INTO word_quiz(word_id, quiz_type, stage, idx, stem, options, answer)
                  VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(word_id, quiz_type, stage, idx) DO UPDATE SET
                    stem=excluded.stem, options=excluded.options, answer=excluded.answer""",
               (wid["id"], d["quiz_type"], d["stage"], d.get("idx", 1),
                d.get("stem", ""), json.dumps(d.get("options", []), ensure_ascii=False),
                d["answer"]))
db.commit()
after = db.execute("SELECT COUNT(*) c FROM word_quiz").fetchone()["c"]
inserted = after - before          # sqlite rowcount 对 DO UPDATE 也返 1，按总数差计
users = db.execute("SELECT COUNT(*) c FROM user_quiz_used").fetchone()["c"]
print(f"题库 {before} → {after}（新增 {inserted}，存量题 id 未动）")
print(f"user_quiz_used 保留 {users} 条（未清空）")
db.close()

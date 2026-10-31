#!/usr/bin/env python3
"""教材分册词表 → 预置词库（user_id=NULL, source_type='textbook'）。
词源：新东方词典分册 tag（与建库同源）；顺序=页面序（字母序，单元序未提供）。
用法：python3 import_textbooks.py [DB_PATH]"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]             # /root/wordslearn
sys.path.insert(0, str(BASE / "app"))
import database  # noqa: E402

NAMES = {"1A": "一年级上册（牛津上海版）", "1B": "一年级下册（牛津上海版）",
         "2A": "二年级上册（牛津上海版）", "2B": "二年级下册（牛津上海版）"}

# 教材以大写形式教学的城市名（沿用 china→China 既有约定）；词表源为小写
CAPS = {"shanghai": "Shanghai"}

def main():
    db = database.get_db()
    sems = json.loads((BASE / "data/parsed/oxford_sh_semesters.json").read_text())
    for sem, words in sems.items():
        exists = db.execute("SELECT id FROM word_libraries WHERE name=? AND user_id IS NULL",
                            (NAMES[sem],)).fetchone()
        if exists:
            print(f"{sem}: 已存在 (id={exists['id']})，跳过")
            continue
        resolved = []
        for w in words:
            w = CAPS.get(w, w)
            row = db.execute("SELECT id FROM words WHERE word=?", (w,)).fetchone()
            if not row:
                cur = db.execute("INSERT INTO words(word) VALUES(?)", (w,))
                row = {"id": cur.lastrowid}
            resolved.append(row["id"])
        cur = db.execute("""INSERT INTO word_libraries(user_id, name, source_type, note)
                            VALUES(NULL, ?, 'textbook', '新东方分册词表（字母序，非单元序）')""",
                         (NAMES[sem],))
        lib_id = cur.lastrowid
        db.executemany("INSERT INTO library_words(library_id, word_id, seq) VALUES(?,?,?)",
                       [(lib_id, wid, i + 1) for i, wid in enumerate(resolved)])
        db.commit()
        print(f"{sem}: {len(resolved)} 词 → 库 id={lib_id}")
    db.close()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""mido 首库：1A 教材词（词表序）+ P13 高频补充（frq 序）混排 → 用户自建库。
用法: WL_DB=... python3 build_first_library.py <username> [补充词数=40]
幂等：同名库已存在则跳过。"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / "app"))
import database  # noqa: E402
from scheduler import create_library, get_library  # noqa: E402

NAME = "一年级·上册教材+高频补充"


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "mido"
    supp_n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    db = database.get_db()
    user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        sys.exit(f"用户不存在: {username}")
    exists = db.execute("SELECT id FROM word_libraries WHERE user_id=? AND name=?",
                        (user["id"], NAME)).fetchone()
    if exists:
        print(f"库已存在 (id={exists['id']})，跳过")
        return
    sems = json.loads((BASE / "data/parsed/oxford_sh_semesters.json").read_text())
    l1a = [w if w != "shanghai" else "Shanghai" for w in sems["1A"]]
    supp = [r["word"] for r in db.execute("""
        SELECT w.word FROM words w JOIN word_levels l ON l.word_id=w.id AND l.grade_code='P13'
        WHERE w.word NOT IN (%s)
        ORDER BY COALESCE(w.frq, 999999), w.id LIMIT ?""" % ",".join("?" * len(l1a)),
        (*l1a, supp_n)).fetchall()]
    ids = []
    for w in l1a + supp:
        r = db.execute("SELECT id FROM words WHERE word=?", (w,)).fetchone()
        if r:
            ids.append(r["id"])
        else:
            print(f"  ⚠️ 词表缺 {w}，跳过（应先 import_textbooks + enrich）")
    out = create_library(user["id"], NAME, [{"word_id": i} for i in ids])
    lib = get_library(user["id"], out["library_id"])
    print(f"建库完成: id={out['library_id']} 总 {out['total']} 词，待生成 {out['pending']}，"
          f"ready={lib['ready']}")
    db.close()


if __name__ == "__main__":
    main()

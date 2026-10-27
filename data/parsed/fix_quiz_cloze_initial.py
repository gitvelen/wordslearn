"""cloze 语境歧义第四轮（2026-10-27）：句首挖空位（连词/副词/代词/短语）。

同第三轮盲区：干扰释义与答案不同，但填入句首后句子自然（"Begin your homework
first" / "Lie down, please" / "Eventually, the door opened"）。
替换词均经人工填空验证荒谬（water/apple 填副词/形容词位=语法荒谬级）。

用法: python3 -I fix_quiz_cloze_initial.py [--apply]
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

FIX = {
    218:   [("that", "water")],            # That is my brother 成立
    484:   [("this", "water")],            # This is my teacher 成立
    956:   [("already", "apple"), ("for the first time", "water")],
    1108:  [("go", "water")],              # Go to the blackboard 成立
    1252:  [("her", "me")],                # her classroom 成立
    1274:  [("actually", "water")],        # Actually, where is Tom 成立
    1307:  [("other", "water")],           # Other books are mine 成立
    1439:  [("all right", "quickly")],     # All right, I am not tired 成立
    1472:  [("yes", "water"), ("please", "away")],
    1662:  [("little", "much")],           # little student 成立; much student 语法错
    1955:  [("yes", "away"), ("no", "water"), ("please", "toothless")],
    2132:  [("white", "much"), ("single", "water")],
    2354:  [("with great care", "water")],
    2410:  [("come on", "water"), ("all right", "away")],
    2421:  [("take out", "water")],        # Take out the book on the desk 成立
    2455:  [("take out", "water")],
    2563:  [("take out", "water")],
    3424:  [("on the other hand", "water")],
    3577:  [("all right", "away")],
    3753:  [("at home", "water"), ("in the end", "apple"), ("on business", "pop")],
    4063:  [("today", "water")],           # Today I will help you 成立
    5152:  [("yet", "water")],             # Yet, cars will fly 成立
    5427:  [("education", "water"), ("test", "apple")],   # 均为 my favourite subject 成立
    5558:  [("hello", "water"), ("yeah", "quickly")],
    5635:  [("story", "quickly"), ("service", "pop")],    # 均为 is important 成立
    5668:  [("everyone", "apple")],        # Everyone is at the door 成立
    7492:  [("at present", "water"), ("perhaps", "apple"), ("especially", "quickly")],
    7808:  [("seek", "water")],            # Seek the ball 成立
    8808:  [("look", "water")],            # Look! The birds are singing 成立
    8872:  [("success", "quickly")],       # Success makes perfect 通
    9702:  [("in a minute", "water"), ("tonight", "apple"), ("seriously", "pop")],
    10035: [("consider", "water")],        # Consider your homework first 成立
    10588: [("touch", "water"), ("draw", "apple")],
    10931: [("yes", "water"), ("come on", "away")],
    11176: [("glad", "water"), ("clean", "daily")],
    11319: [("begin", "water")],           # Begin your homework first 成立
    11552: [("lie", "water")],             # Lie down, please 成立
    12532: [("focus on", "water")],
    13028: [("smart", "married"), ("stupid", "medical")],
    13062: [("none", "apple")],            # None is knocking 成立
    14935: [("eventually", "water")],      # Eventually, the door opened 成立
    15986: [("obviously", "water")],
    16702: [("absolutely", "water")],
    17415: [("apparently", "water"), ("suddenly", "apple")],
    18777: [("ok", "water")],
    19254: [("ok", "water")],
    20075: [("lift", "water")],            # Lift the bottle 通
    21185: [("gently", "water")],          # Gently, the rain will stop 通
    21717: [("correctly", "water"), ("beautifully", "apple")],
    22251: [("sadly", "water"), ("happily", "apple")],
    22295: [("meanwhile", "water"), ("gently", "apple")],
    23706: [("happily", "water"), ("barely", "apple")],
}


def main():
    apply = "--apply" in sys.argv
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    cur = db.cursor()
    in_vocab = {r["word"] for r in cur.execute("SELECT word FROM words")}
    senses = {}
    for r in cur.execute("""SELECT wd.word, s.definition_zh FROM word_senses s
                            JOIN words wd ON s.word_id=wd.id"""):
        senses.setdefault(r["word"], set()).update(
            re.split(r"[；;，,、/]", re.sub(r"^[a-zA-Z.]+\.?", "", (r["definition_zh"] or "").strip())))
    norm = lambda s: re.sub(r"\s+", "", s)

    errors = []
    for qid, pairs in FIX.items():
        r = cur.execute("SELECT options, answer, wd.word AS wword FROM word_quiz q "
                        "JOIN words wd ON wd.id=q.word_id WHERE q.id=?", (qid,)).fetchone()
        if not r:
            errors.append(f"#{qid} 不存在"); continue
        opts = json.loads(r["options"])
        a_s = {norm(x) for x in senses.get(r["wword"], set()) if norm(x)}
        for bad, new in pairs:
            if bad not in opts:
                errors.append(f"#{qid} {bad!r} 不在 {opts}"); continue
            if new not in in_vocab:
                errors.append(f"#{qid} {new!r} 不在词库"); continue
            n_s = {norm(x) for x in senses.get(new, set()) if norm(x)}
            if n_s & a_s:
                errors.append(f"#{qid} {new!r} 与答案义项交集")
            opts[opts.index(bad)] = new
        if len(set(opts)) != 4 or r["answer"] not in opts:
            errors.append(f"#{qid} 修复后异常 {opts}")
    if errors:
        print("验证失败："); [print(" ", e) for e in errors]; sys.exit(1)

    if not apply:
        print(f"验证通过，{len(FIX)} 题待应用（dry-run）"); return
    cur.execute("BEGIN")
    for qid, pairs in FIX.items():
        opts = json.loads(cur.execute("SELECT options FROM word_quiz WHERE id=?", (qid,)).fetchone()[0])
        for bad, new in pairs:
            opts[opts.index(bad)] = new
        cur.execute("UPDATE word_quiz SET options=? WHERE id=?", (json.dumps(opts, ensure_ascii=False), qid))
    cur.execute("COMMIT")
    print(f"已应用 {len(FIX)} 题")


if __name__ == "__main__":
    main()

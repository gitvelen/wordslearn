"""cloze 语境歧义第三轮修复（2026-10-27）：形容词位/连词位的"填入自然"干扰。

检测器（senses 义项零交集）对此类盲区：干扰释义与答案完全不同，但填入挖空后
句子依然自然（a long book / a big house），孩子按语境无法唯一判定。
修复：把此类干扰换成与该名词搭配荒谬的形容词（married 没牙的 daily 等无生命
错配），每词均经人工填空验证荒谬、避开儿童题不妥搭配（delicious 不用于人）。

用法: python3 -I fix_quiz_cloze_context.py [--apply]   （默认 dry-run+验证）
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"

# qid -> [(坏干扰, 新干扰), ...]（全部经人工填空验证：填入后荒谬/语法错）
FIX = {
    595:   [("long", "married")],
    628:   [("when", "apple"), ("while", "water"), ("because", "quickly")],  # 连词全成立，整题重做
    1640:  [("different", "married")],
    2210:  [("different", "delicious")],
    2520:  [("right", "married")],          # right hand 成立
    2796:  [("national", "delicious")],     # national library 成立
    3323:  [("good", "married")],           # good park 成立
    3664:  [("nice", "freezing")],
    3709:  [("long", "married"), ("american", "toothless")],
    3786:  [("large", "married"), ("red", "toothless")],
    4052:  [("best", "toothless")],
    4248:  [("true", "wooden"), ("perfect", "married"), ("low", "delicious")],
    4434:  [("black", "married"), ("high", "toothless")],
    4445:  [("free", "married"), ("big", "toothless")],
    4644:  [("black", "married")],
    4732:  [("pretty", "toothless")],
    4953:  [("beautiful", "married")],
    5186:  [("green", "married"), ("common", "toothless")],
    6141:  [("single", "married")],
    6731:  [("safe", "delicious"), ("social", "married")],
    6764:  [("short", "toothless")],
    7633:  [("fine", "married")],
    7897:  [("natural", "married"), ("clean", "toothless")],
    8389:  [("beautiful", "married")],
    8786:  [("blue", "married")],
    9004:  [("perfect", "medical"), ("stupid", "daily")],
    9215:  [("british", "toothless")],
    9448:  [("amazing", "married")],
    9603:  [("safe", "toothless")],
    9669:  [("regular", "delicious")],
    9792:  [("wide", "married")],
    9858:  [("quick", "married")],
    10165: [("professional", "married"), ("australian", "delicious")],
    10554: [("modern", "married"), ("safe", "toothless")],
    10887: [("responsible", "delicious")],
    10920: [("famous", "married")],
    11407: [("interesting", "delicious")],
    11640: [("african", "married")],
    12170: [("regular", "delicious")],
    12202: [("regular", "toothless")],
    12397: [("wonderful", "married"), ("heavy", "delicious"), ("clean", "toothless")],
    12420: [("lucky", "married")],
    12641: [("useful", "plastic")],
    12874: [("golden", "married"), ("heavy", "toothless")],
    13980: [("busy", "married")],
    14298: [("smart", "married")],
    14386: [("bright", "toothless"), ("warm", "married"), ("dangerous", "medical")],
    14509: [("electric", "married")],
    14890: [("friendly", "toothless"), ("exciting", "married"), ("busy", "delicious")],
    14924: [("dirty", "married")],
    15919: [("loud", "married")],
    16229: [("loud", "toothless")],
    16647: [("usual", "married")],
    16989: [("empty", "married"), ("pink", "toothless")],
    17150: [("angry", "married"), ("sharp", "delicious"), ("plastic", "daily")],
    17349: [("growing", "married"), ("exciting", "toothless")],
    17482: [("soft", "married")],
    17603: [("empty", "toothless"), ("dirty", "married")],
    17924: [("creative", "toothless"), ("olympic", "married")],
    18097: [("ordinary", "married"), ("pink", "toothless"), ("thin", "daily")],
    18208: [("upset", "married"), ("thick", "medical"), ("smooth", "daily")],
    18397: [("smooth", "married")],
    18668: [("silly", "toothless"), ("everyday", "married")],
    18722: [("homeless", "daily"), ("strict", "medical")],
    18854: [("wise", "married")],
    18976: [("nervous", "married")],
    19031: [("tiny", "married"), ("fit", "toothless"), ("broad", "delicious")],
    19425: [("wet", "married")],
    19727: [("broad", "toothless")],
    19988: [("boring", "married")],
    20010: [("wooden", "married")],
    20216: [("embarrassed", "delicious"), ("confident", "daily")],
    20464: [("actual", "delicious"), ("boring", "toothless")],
    20550: [("frozen", "married"), ("horrible", "medical")],
    20636: [("favorite", "toothless")],
    20692: [("wooden", "married"), ("actual", "pop")],
    20855: [("lonely", "married"), ("favorite", "toothless")],
    20942: [("awful", "married")],
    21118: [("spare", "toothless")],
    21229: [("unusual", "married"), ("keen", "medical")],
    21285: [("enjoyable", "toothless")],
    21674: [("polite", "toothless"), ("dull", "daily")],
    21762: [("wounded", "married"), ("embarrassed", "medical"), ("homeless", "daily")],
    22752: [("favorite", "plastic"), ("unhealthy", "daily"), ("shy", "medical")],
    22950: [("obvious", "toothless")],
    23995: [("rare", "married")],
    24256: [("dishonest", "daily")],
    24289: [("noisy", "toothless")],
    24696: [("typical", "married")],
    24719: [("windy", "married"), ("changeable", "toothless")],
    25184: [("toothless", "daily"), ("gray", "medical")],
    25924: [("tidy", "toothless"), ("changeable", "married"), ("frightening", "daily")],
    25978: [("forgetful", "daily"), ("thirsty", "plastic")],
    26297: [("careless", "toothless")],
    26308: [("shiny", "married"), ("typical", "toothless")],
}


def main():
    apply = "--apply" in sys.argv
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # 词库与 senses 校验数据
    in_vocab = {r["word"] for r in cur.execute("SELECT word FROM words")}
    senses = {}
    for r in cur.execute("""SELECT wd.word, s.definition_zh FROM word_senses s
                            JOIN words wd ON s.word_id=wd.id"""):
        senses.setdefault(r["word"], set()).update(
            re.split(r"[；;，,、/]", re.sub(r"^[a-zA-Z.]+\.?", "", (r["definition_zh"] or "").strip())))

    def norm(s):
        return re.sub(r"\s+", "", s)

    errors = []
    for qid, pairs in FIX.items():
        r = cur.execute("""SELECT q.stem, q.options, q.answer, wd.word AS wword, wd.id AS wid
                           FROM word_quiz q JOIN words wd ON wd.id=q.word_id WHERE q.id=?""", (qid,)).fetchone()
        if not r:
            errors.append(f"#{qid} 不存在"); continue
        opts = json.loads(r["options"])
        a_senses = senses.get(r["wword"], set())
        for bad, new in pairs:
            if bad not in opts:
                errors.append(f"#{qid} 坏干扰 {bad!r} 不在选项 {opts}"); continue
            if new not in in_vocab:
                errors.append(f"#{qid} 新干扰 {new!r} 不在词库"); continue
            new_s = {norm(x) for x in senses.get(new, set()) if norm(x)}
            a_s = {norm(x) for x in a_senses if norm(x)}
            if new_s & a_s:
                errors.append(f"#{qid} 新干扰 {new!r} 与答案义项交集 {new_s & a_s}")
            opts[opts.index(bad)] = new
        if len(set(opts)) != 4 or r["answer"] not in opts:
            errors.append(f"#{qid} 修复后选项异常 {opts}")
    if errors:
        print("验证失败：")
        for e in errors:
            print(" ", e)
        sys.exit(1)

    if not apply:
        print(f"验证通过，{len(FIX)} 题待应用（dry-run）")
        return

    cur.execute("BEGIN")
    for qid, pairs in FIX.items():
        r = cur.execute("SELECT options FROM word_quiz WHERE id=?", (qid,)).fetchone()
        opts = json.loads(r["options"])
        for bad, new in pairs:
            opts[opts.index(bad)] = new
        cur.execute("UPDATE word_quiz SET options=? WHERE id=?", (json.dumps(opts, ensure_ascii=False), qid))
    cur.execute("COMMIT")
    print(f"已应用 {len(FIX)} 题")


if __name__ == "__main__":
    main()

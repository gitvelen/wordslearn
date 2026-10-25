"""题库生成器（v4.2）：从精修义项 word_senses.jsonl 脚本化生成全部题目。
确定性生成（随机种子固定），零幻觉——干扰项规则：同词性、frq 邻近优先。

每词生成（9-12 题）：
  learn:    word_zh×1 + listen_zh×1 + zh_word×1 + spell×1   （新词首学 4 题：形→义/音→义/义→形/音→形）
            + emoji×1（仅有 emoji 的词；低龄听音选词的视觉形态）
  d2:       zh_word×2（不同干扰组）           （复习·再认）
  d5:       cloze×1 + zh_word×1              （复习·语境）
  d12:      spell×1 + zh_word×1              （复习·产出）
  confirm:  word_zh×1                        （表格验证轮）

用法: python3 -I gen_quiz.py   （在 data/parsed 目录，读 gen_input.jsonl + gen_out/word_senses.jsonl +
DB words.emoji，输出 gen_out/word_quiz.jsonl + 统计报告）
"""
import json
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
SEED = 20261024

# 词形同族黑名单：互为变形/近形态的词不得互为干扰项（can↔could、will↔would、goes→go 等）——
# 它们的释义在初学者眼里无法区分，"能；可以" 对 can/could 是双正确题。
FAMILIES = [
    # 情态动词整体一族：释义（能/将/愿/会/必须）对初学者互不可分
    {"can", "could", "will", "would", "shall", "should", "may", "might", "must"},
    {"use", "used", "used to"}, {"do", "does", "did"}, {"go", "went", "gone", "goes"},
    {"be", "am", "is", "are", "was", "were", "been"}, {"have", "has", "had"},
    {"this", "that", "these", "those"}, {"he", "she", "it", "they", "we", "you", "i", "me", "him", "her", "them", "us"},
    {"come", "came"}, {"see", "saw", "seen"}, {"take", "took", "taken"},
    {"get", "got", "gotten"}, {"give", "gave", "given"}, {"make", "made"},
    {"good", "better", "best"}, {"many", "much", "more", "most"},
    {"little", "less", "least"}, {"bad", "worse", "worst"},
    {"far", "farther", "further"}, {"old", "older", "elder"},
]


def same_family(a, b):
    if a == b:
        return True
    al, bl = a.lower(), b.lower()
    for f in FAMILIES:
        if al in f and bl in f:
            return True
    # 词形同根：一方是另一方的屈折变形（goes/going/ran）
    for x, y in ((al, bl), (bl, al)):
        if len(x) > 3 and (y == x + "s" or y == x + "es" or y == x + "ing"
                           or y == x + "d" or y == x + "ed"
                           or (x.endswith("e") and y == x[:-1] + "ing")
                           or (x.endswith("y") and y == x[:-1] + "ied")):
            return True
    return False


def load():
    words = {}          # word -> {frq, seg}
    for line in (BASE / "gen_input.jsonl").read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        words[d["w"]] = d
    emoji = {}          # word -> emoji（低龄 emoji 题题干）
    db = sqlite3.connect(f"file:{BASE.parent.parent / 'app' / 'data' / 'wordslearn.db'}?mode=ro", uri=True)
    for w, e in db.execute("SELECT word, emoji FROM words WHERE emoji IS NOT NULL AND emoji != ''"):
        emoji[w] = e
    db.close()
    senses = defaultdict(list)
    # v4.2：读 v2 分片（嵌套格式 {word, senses:[...]}，词典式完备），展平
    for f in sorted((BASE / "gen_out").glob("v2_shard_[0-9][0-9].jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            senses[d["word"]].extend(d["senses"])
    for v in senses.values():
        v.sort(key=lambda s: s["sense_order"])
    return words, senses, emoji


def main():
    random.seed(SEED)
    words, senses, emoji = load()
    # 选项池：pos -> [(zh, word, frq)]（核心义；同词性抽干扰）
    pool = defaultdict(list)
    for w, ss in senses.items():
        meta = words.get(w)
        if not ss:
            continue
        pool[ss[0].get("pos") or "?"].append((ss[0]["definition_zh"], w, (meta or {}).get("frq") or 999999))
    for k in pool:
        pool[k].sort(key=lambda t: t[2])

    out = []
    stats = defaultdict(int)
    idx_counter = defaultdict(int)   # (word, quiz_type, stage) → 已出题数（保证 idx 唯一防 UPSERT 覆盖）

    def emit(qt, stage, idx, stem, options, answer):
        options = options[:]
        random.shuffle(options)
        idx_counter[(w, qt, stage)] += 1
        out.append({"word": w, "quiz_type": qt, "stage": stage, "idx": idx_counter[(w, qt, stage)],
                    "stem": stem, "options": options, "answer": answer})
        stats[f"{stage}/{qt}"] += 1

    def distract_zh(w, pos, n=3, exclude=()):
        """同词性、frq 邻近的中文义干扰。exclude 里的 zh 排除。
        同义排除：核心义与目标词相同的词不作干扰；同族排除：词形/时态变体不作干扰。"""
        zh1 = senses.get(w, [{}])[0].get("definition_zh")
        cand = [t for t in pool.get(pos, []) if t[1] != w and t[0] not in exclude
                and t[0] != zh1 and not same_family(w, t[1])]
        if not cand:
            cand = [t for k in pool for t in pool[k] if t[1] != w and t[0] not in exclude
                    and t[0] != zh1 and not same_family(w, t[1])]
        # frq 邻近：以当前词 frq 为中心窗口抽样，不足放宽到全池
        frq = (words.get(w) or {}).get("frq") or 999999
        for window in (200, 800, 3000, 10 ** 9):
            near = [t for t in cand if abs(t[2] - frq) <= window]
            if len(near) >= n:
                return random.sample(near, n)
        return cand[:n]

    def distract_word(w, pos, n=3, exclude=()):
        zh1 = senses.get(w, [{}])[0].get("definition_zh")
        syn = {t[0] for k in pool for t in pool[k] if t[0] == zh1}   # 同义词条目集合
        cand = [t[1] for t in pool.get(pos, []) if t[1] != w and t[1] not in exclude
                and t[1] not in syn and not same_family(w, t[1])]
        if len(cand) < n:
            cand += [t[1] for k in pool for t in pool[k] if t[1] != w and t[1] not in exclude
                     and t[1] not in syn and not same_family(w, t[1])]
        frq = (words.get(w) or {}).get("frq") or 999999
        for window in (200, 800, 3000, 10 ** 9):
            near = [t for t in pool.get(pos, []) if t[1] != w and abs(t[2] - frq) <= window
                    and t[1] not in exclude and not same_family(w, t[1])]
            if len(near) >= n:
                return [t[1] for t in random.sample(near, n)]
        return list(dict.fromkeys(cand))[:n]

    for w, meta in words.items():
        ss = senses.get(w)
        if not ss:
            stats["无senses跳过"] += 1
            continue
        s1 = ss[0]
        pos = s1.get("pos") or "?"
        zh1 = s1["definition_zh"]
        ex = (s1.get("example_en") or "").strip()
        seg = meta["seg"]

        # learn：word_zh + listen_zh（不同干扰组）
        d1 = [t[0] for t in distract_zh(w, pos, 3, exclude={zh1})]
        if len(set(d1 + [zh1])) == 4:
            emit("word_zh", "learn", 1, w, d1 + [zh1], zh1)
        d2_ = [t[0] for t in distract_zh(w, pos, 3, exclude={zh1} | set(d1))]
        if len(set(d2_ + [zh1])) == 4:
            emit("listen_zh", "learn", 1, "", d2_ + [zh1], zh1)
        # learn v4.2：zh_word（中文义→选英文，反向检索）+ spell（当天听写，产出）
        lw = distract_word(w, pos, 3, exclude={w})
        if len(set(lw + [w])) == 4:
            emit("zh_word", "learn", 1, zh1, lw + [w], w)
        emit("spell", "learn", 1, "", [], w)
        # learn v4.2：emoji（低龄听音选词·视觉锚点；仅有 emoji 的词）
        em = emoji.get(w)
        if em:
            ew = distract_word(w, pos, 3, exclude={w} | set(lw))
            if len(set(ew + [w])) == 4:
                emit("emoji", "learn", 1, em, ew + [w], w)
        # confirm：word_zh（第三组干扰）
        d3 = [t[0] for t in distract_zh(w, pos, 3, exclude={zh1} | set(d1) | set(d2_))]
        if len(set(d3 + [zh1])) == 4:
            emit("word_zh", "confirm", 1, w, d3 + [zh1], zh1)
        # d2 / d5 / d12：zh_word ×4（不同干扰）
        wsets = []
        for i in range(4):
            excl = {w} | {x for s in wsets for x in s}
            ws = distract_word(w, pos, 3, exclude=excl)
            if len(set(ws + [w])) == 4:
                wsets.append(ws)
        stages = ["d2", "d2", "d5", "d12"]
        for stage, ws in zip(stages, wsets):
            emit("zh_word", stage, 1, zh1, ws + [w], w)
        # d5：cloze（例句挖空）
        if ex:
            import re
            stem = re.sub(rf"\b{re.escape(w)}\b", "____", ex, count=1, flags=re.IGNORECASE)
            if "____" in stem:
                cw = distract_word(w, pos, 3)
                if len(set(cw + [w])) == 4:
                    emit("cloze", "d5", 1, stem, cw + [w], w)
        # d12：spell
        out.append({"word": w, "quiz_type": "spell", "stage": "d12", "idx": 1,
                    "stem": "", "options": [], "answer": w})
        stats["d12/spell"] += 1

    dest = BASE / "gen_out" / "word_quiz.jsonl"
    with dest.open("w", encoding="utf-8") as f:
        for q in out:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    print(f"生成 {len(out)} 题 → {dest}")
    for k in sorted(stats):
        print(f"  {k}: {stats[k]}")


if __name__ == "__main__":
    main()

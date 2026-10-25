"""senses 终检（合并全部分片后的语义级审计）：
A. 例句必须包含目标词（含词形变化）——cloze 挖空题的前提
B. 例句词汇超纲检测——例句用词 ∈ 词表 ∪ 白名单（功能词/数字/常见人名）
C. 释义重复检测——不同词的核心义完全相同（会破坏四选一区分度）
用法: python3 -I final_audit.py（在 data/parsed 目录）"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

# 功能词/白名单（音标表外但例句必需的安全词）
WHITELIST = set("""the a an and or but if of to in on at by for with from as is am are was were be been
being do does did doing have has had having will would can could shall should may might must not no
yes this that these those there here it its he she they them his her their our your my me you us we
i what which who whom whose when where why how all any both each few more most other some such only
own same so than too very just also very much many lot lots one two three four five six seven eight
nine ten eleven twelve hundred new old good bad big small long short tall high low now then today
tomorrow yesterday very really quite let please thanks ok dear mum dad mom mother father brother
sister grandma grandpa teacher miss mr mrs ms dr sir hello goodbye bye please again always never
often sometimes usually soon early late first last next every day week month year morning afternoon
evening night time school home class grade name friend boy girl child children man woman people
family game sport ball food drink eat drink like love go come see look say speak tell ask answer
know think want need help give get make take put bring play run walk jump swim read write listen
sing dance draw sleep wake up down out off over under near far behind front back left right round
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday zero tenth eleventh twelfth twentieth
thanksgiving christmas easter halloween
kangaroo penguin puppy snail spider monster mosquito dolphin parrot rabbit duck cow pig horse
knife sock stocking pillow towel sofa couch vase pan pot nail tap roof tent camp mobile phone
ladder necklace medal shell hiking sail surfing surf swim swimmer runner runner runner jog
musical hoe weed lever meter metre litre liter kilo mooncake dumpling noodle biscuit
smith tim tom jack mary lily lucy ben anna texas london paris china york mimi guangzhou shanghai beijing
juicy kindly neat sudden stripe sunset sunshine treasure symbol storybook valley wizard email app cannot ed chimney bamboo pe favour centre kung cock edison fu britain tyre
shoelace mall farmhouse beehive wheelbarrow
red blue green yellow black white brown orange pink purple grey gray warm cold hot cool dry wet
clean dirty happy sad angry tired hungry thirsty afraid sorry glad sure ready fine great nice
because so that who which when while before after during about around against between into onto
through under without during something anything nothing everything someone anyone everyone nobody
everybody anything myself yourself himself herself itself ourselves themselves himself whom whose
isn't aren't wasn't werenen't don't doesn't didn't can't couldn't won't wouldn't shouldn't isn
it's that's i'm we're they're you're he's she's let's what's there's o'clock clock pm am
usa uk un abc tv cd dvd id ok twenty thirty forty fifty sixty seventy eighty ninety
math maths bike bikes beans bean stairs stair schoolbag schoolbags clap claps clapping
coming doing going making takes gives knows shows yuan mr mrs miss dr sing sings
song songs film films zoo zoos picnic picnics earthquake earthquakes usa uk un abc tv
cd dvd id ok february firemen fireman shine shines shining yuan""".split())

NAMES = {"tom", "lily", "lucy", "jack", "mary", "ann", "anna", "jane", "jim", "kate", "sam",
         "amy", "peter", "paul", "alice", "bob", "ben", "bill", "bill", "carl", "david", "danny",
         "emma", "eric", "grace", "helen", "henry", "jerry", "john", "kevin", "linda", "lisa",
         "mark", "mike", "nancy", "nick", "rose", "roy", "sue", "tony", "victor", "wang", "li",
         "chen", "zhang", "liu", "wu", "beijing", "shanghai", "london", "paris", "china",
         "england", "america", "australia", "canada", "india", "japan", "korea", "asia",
         "europe", "africa", "english", "chinese", "american", "british", "tom", "lily", "wang", "li", "ming"}

def load_all():
    rows = []
    for f in sorted((BASE / "gen_out").glob("v2_shard_[0-9][0-9].jsonl")):
        for l in f.read_text(encoding="utf-8").splitlines():
            if not l.strip():
                continue
            d = json.loads(l)
            for s in d["senses"]:
                rows.append({"word": d["word"], **s})
    if not rows:   # 兜底：v4.1 合并文件
        rows = [json.loads(l) for l in (BASE / "word_senses.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    vocab = set()
    for line in (BASE / "gen_input.jsonl").read_text(encoding="utf-8").splitlines():
        vocab.add(json.loads(line)["w"].lower())
    return rows, vocab


IRREGULAR = {"be": {"be", "am", "is", "are", "was", "were", "been", "being"},
             "go": {"go", "goes", "went", "gone", "going"},
             "have": {"have", "has", "had", "having"},
             "do": {"do", "does", "did", "done", "doing"},
             "say": {"say", "says", "said", "saying"},
             "make": {"make", "makes", "made", "making"},
             "foot": {"foot", "feet"}}


POSSESSIVES = ["one's", "oneself", "one’s"]


def word_forms(w):
    """目标词的常见词形变化集合（be 动词变形、词组首词三单、one's 代词替换、连字符/引号变体）"""
    w = w.lower().replace("’", "'")   # 弯直引号归一
    if w in IRREGULAR:
        return IRREGULAR[w]
    first = w.split()[0] if " " in w else w          # 词组：首词变形
    rest = w[len(first):]
    forms = {w}
    f1 = {first}
    if first not in IRREGULAR and "'" not in first:
        f1 |= {first + "s", first + "es", first + "ed", first + "d", first + "ing"}
        if first.endswith("e") and len(first) > 2:
            f1.add(first[:-1] + "ing")
        if re.match(r".*[^aeiou][aeiou][ptbdgmnrv]$", first):
            f1.add(first + first[-1] + "ing")
        if first.endswith("y") and len(first) > 2 and first[-2] not in "aeiou":
            f1 |= {first[:-1] + "ies", first[:-1] + "ied"}
    else:
        f1 |= IRREGULAR.get(first, set())
    forms |= {f + rest for f in f1}
    # 连字符变体与去引号变体
    forms |= {f.replace("-", " ") for f in list(forms) if "-" in f}
    forms |= {f.replace("-", "") for f in list(forms) if "-" in f}
    if " " not in w and "'" not in w:                 # 整词变形（非词组）
        forms |= {w + "s", w + "es", w + "ed", w + "d", w + "ing"}
        if w.endswith("e") and len(w) > 2:
            forms.add(w[:-1] + "ing")
        if re.match(r".*[^aeiou][aeiou][ptbdgmnrv]$", w):
            forms.add(w + w[-1] + "ing")
        if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
            forms |= {w[:-1] + "ies", w[:-1] + "ied"}
    return forms


def lemma_in_vocab(t, vocab):
    """token 词形归一后查词表：apples→apple、taller→tall、running→run"""
    if t in vocab:
        return True
    cands = {t}
    for suf in ("s", "es", "d", "ed", "ing", "er", "est"):
        if t.endswith(suf) and len(t) > len(suf) + 1:
            cands.add(t[: -len(suf)])
    if t.endswith("ies") and len(t) > 4:
        cands.add(t[:-3] + "y")
    if t.endswith("ied") and len(t) > 4:
        cands.add(t[:-3] + "y")
    if t.endswith("ing") and len(t) > 5:
        cands.add(t[:-3] + "e")    # making→make, coming→come
        cands.add(t[:-4])          # running→run（双写辅音还原）
    for verb, forms in IRREGULAR.items():
        if t in forms:
            return True
    return any(c in vocab for c in cands)


def load_ecdict_frq():
    """ECDICT word→frq（frq>0 才收录：ECDICT 只给常用词标词频，全集含 77 万专业词，
    用全集做兜底会把 photosynthesis 一并放行——兜底必须是"常用词频"判定）"""
    import csv as _csv
    path = BASE.parent / "ext" / "ecdict" / "ecdict.csv"
    d = {}
    if not path.exists():
        return d
    with path.open(encoding="utf-8", errors="ignore") as f:
        for row in _csv.DictReader(f):
            w = (row["word"] or "").lower().strip()
            s = (row.get("frq") or "").strip()
            if w and s:
                try:
                    r = int(float(s))
                    if r > 0:
                        d[w] = r
                except ValueError:
                    pass
    return d


def load_lemma_map():
    """lemma.en.txt（BNC 变形表）：token→原形 映射，sang→sing、teeth→tooth"""
    path = BASE.parent / "ext" / "ecdict" / "lemma.en.txt"
    m = {}
    if not path.exists():
        return m
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(";"):
            continue
        head, _, right = line.partition("->")
        lemma = head.split("/")[0].strip().lower()
        for tok in right.split(","):
            tok = tok.strip().lower()
            if tok:
                m[tok] = lemma
    return m


def main():
    rows, vocab = load_all()
    ecdict = load_ecdict_frq()
    lemma_map = load_lemma_map()
    print(f"合并 {len(rows)} 行 senses")
    problems_a, problems_b, problems_c = [], [], Counter()

    zh_core = {}
    for d in rows:
        w = d["word"].lower()
        ex = (d.get("example_en") or "").replace("’", "'")
        ex_l = ex.lower()
        # A. 例句含目标词（复用 validate_senses_v2 的强词形归一：不规则表+分隔式短语+可变成分）
        from validate_senses_v2 import word_forms_ok
        if not word_forms_ok(d["word"], ex):
            problems_a.append(f"{d['word']}: 例句未含目标词 → {ex}")
        # B. 例句词汇超纲（词形归一后查表；人名白名单兜底）
        tokens = re.findall(r"[a-z']+", ex_l)
        tokens = [t.strip("'") for t in tokens]
        tokens = [t[:-2] if t.endswith("'s") else t for t in tokens]
        tokens = [t for t in tokens if t]
        for t in tokens:
            if t in vocab or t in WHITELIST or t.isdigit() or len(t) <= 1:
                continue
            if lemma_in_vocab(t, vocab):
                continue
            lm = lemma_map.get(t)
            if lm is not None and (lm in vocab or lm in ecdict):
                continue   # 变形→原形（BNC lemma 表）后命中词表/ECDICT
            if t in NAMES or t.replace("'s", '') in NAMES or t.replace("'s", '') in WHITELIST:
                continue
            if lemma_in_vocab(t, WHITELIST):
                continue
            if ecdict.get(t, 99999) <= 8000:      # ECDICT 词频内=常用可读词
                continue
            if lemma_in_vocab(t.replace("'s", ''), vocab):
                continue
            problems_b.append(f"{d['word']}: 超纲词 '{t}' → {ex}")
        # C. 核心义重复
        core = (d["definition_zh"] or "").strip()
        if core:
            zh_core.setdefault(core, []).append(d["word"])

    dups = {k: v for k, v in zh_core.items() if len(v) > 1}
    print(f"\n[A] 例句未含目标词: {len(problems_a)}")
    for x in problems_a[:25]:
        print("   ", x)
    print(f"\n[B] 例句超纲词: {len(problems_b)} 条")
    by_word = {}
    for x in problems_b:
        by_word.setdefault(x.split(':')[0], []).append(x)
    for k in list(by_word)[:25]:
        print("   ", by_word[k][0], f"(共{len(by_word[k])}条)")
    print(f"\n[C] 相同核心义（四选一区分度风险）: {len(dups)} 组")
    for k, v in list(dups.items())[:20]:
        print(f"    '{k}': {', '.join(v)}")

    (BASE / "gen_out" / "audit_report.json").write_text(
        json.dumps({"no_target_word": problems_a, "offlist": problems_b,
                    "dup_core": dups}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n明细 → gen_out/audit_report.json")


if __name__ == "__main__":
    main()

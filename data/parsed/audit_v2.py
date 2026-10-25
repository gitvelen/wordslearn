"""v2 释义数据全量审计：10 条规则扫全部 v2_shard 释义。
输出: gen_out/v2_audit.json（违规明细，按词聚合）+ 控制台摘要。"""
import json
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
words_meta = {}
for line in (BASE / "gen_input.jsonl").read_text(encoding="utf-8").splitlines():
    d = json.loads(line)
    words_meta[d["w"]] = d

IRREGULAR = {"be": {"be","am","is","are","was","were","been","being"},
             "go": {"go","goes","went","gone","going"}, "have": {"have","has","had","having"},
             "do": {"do","does","did","done","doing"}, "say": {"say","says","said","saying"},
             "make": {"make","makes","made","making"}, "get": {"get","gets","got","getting"},
             "foot": {"foot","feet"}}

def forms(w):
    wl = w.lower().replace("’", "'")
    if wl in IRREGULAR: return IRREGULAR[wl]
    out = {wl}
    if " " not in wl and "'" not in wl:
        out |= {wl+"s", wl+"es", wl+"ed", wl+"d", wl+"ing", wl[:-1]+"ing", wl+"ies"}
        if len(wl) > 3 and wl[-1] == wl[-2]:
            out.add(wl + wl[-1] + "ing")
    if " " in wl:
        first, rest = wl.split()[0], wl[len(wl.split()[0]):]
        out |= {(first+s)+rest for s in ("s","es","ed","ing")}
    # one's 代词
    for p in ("one's", "oneself"):
        if p in wl:
            base = wl.replace(p, "X")
            for pron in ("my","his","her","their","our","your","its","one's",
                         "myself","himself","herself","ourselves","themselves","oneself"):
                out.add(base.replace("X", pron))
    return out

issues = defaultdict(list)   # word -> [issue strings]
low_words = []
allrows = {}
for f in sorted((BASE / "gen_out").glob("v2_shard_[0-9][0-9].jsonl")):
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        allrows[d["word"]] = d["senses"]

for w, ss in allrows.items():
    meta = words_meta.get(w)
    seg = meta["seg"] if meta else "JUNIOR"
    seen_zh = []
    for s in ss:
        so, pos, zh = s.get("sense_order"), s.get("pos"), (s.get("definition_zh") or "").strip()
        en, ex, exz = (s.get("definition_en") or "").strip(), (s.get("example_en") or "").strip(), (s.get("example_zh") or "").strip()
        if s.get("confidence") == "low":
            low_words.append(f"{w}#{so}")
        # R1 同词义项重复
        zh_norm = re.sub(r"[；;，,（）()\s·]", "", zh)
        for prev in seen_zh:
            inter = len(set(prev) & set(zh_norm))
            if zh_norm and inter >= max(2, min(len(prev), len(zh_norm)) * 0.7) and zh_norm != prev:
                issues[w].append(f"义项{so}与前一义疑似重复: '{zh}'")
            if zh_norm and zh_norm == prev:
                issues[w].append(f"义项{so}与前一义完全相同: '{zh}'")
        seen_zh.append(zh_norm)
        # R2 释义超长
        if len(re.sub(r"[（）()\s·]", "", zh)) > 9:
            issues[w].append(f"义项{so}释义超长({len(zh)}字): {zh}")
        # R3 过去式错位
        m = re.match(r"(.{1,12}?)(?:的)?(?:过去式|过去分词|现在分词|复数|第三人称)", zh)
        if m and m.group(1).strip().lower() not in forms(w):
            issues[w].append(f"义项{so}错位标注: '{zh}'（不是 {w} 的变形）")
        # R4 释义自循环
        if w.lower() in zh.lower():
            issues[w].append(f"义项{so}释义含词条本身: {zh}")
        # R5 例句缺失/超长/不含目标词
        if not ex:
            issues[w].append(f"义项{so}缺例句")
        else:
            lim = 10 if seg in ("P13","P45") else 12
            nw = len(ex.split())
            if nw > lim + 1:
                issues[w].append(f"义项{so}例句超长({nw}>{lim}): {ex}")
            exl = ex.lower().replace("’","'")
            if not any(re.search(rf"(?<![a-z]){re.escape(fx)}(?![a-z])", exl) for fx in forms(w)):
                issues[w].append(f"义项{so}例句未含目标词: {ex}")
            # R6 进行时/被动粗查
            if re.search(r"\b(is|are|am|was|were)\s+\w+ing\b", exl):
                issues[w].append(f"义项{so}例句疑进行时: {ex}")
            if re.search(r"\b(was|were|is|are|am)\s+\w+(ed|en|wn|ne|t)\b", exl) and not w.startswith("be "):
                if not re.search(r"\b(is|are|am|was|were)\s+(red|open|closed|free|kind|good|bad|happy|sad|afraid|sorry|glad|late|tired|hungry|thirsty|ready|interested|excited|born|supposed|used|located|made|grown)\b", exl):
                    issues[w].append(f"义项{so}例句疑被动: {ex}")
            if not (ex[0].isupper() or ex[0] in '"\'('):
                issues[w].append(f"义项{so}例句未大写: {ex}")
        # R7 中文翻译
        if not exz or not re.search(r"[一-鿿]", exz):
            issues[w].append(f"义项{so}缺中文翻译")
        # R8 pos 非法
        if pos not in {"n.","v.","vt.","vi.","adj.","adv.","prep.","conj.","pron.","num.","int.","aux.","modal."}:
            issues[w].append(f"义项{so}词性异常: {pos}")
    # R9 义项数
    if len(ss) > 4:
        issues[w].append(f"义项数异常: {len(ss)}")

out = {"low_confidence": low_words, "issues": dict(issues)}
(BASE / "gen_out" / "v2_audit.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
n_w = len(issues)
n_i = sum(len(v) for v in issues.values())
print(f"审计 {len(allrows)} 词 {sum(len(v) for v in allrows.values())} 义项")
print(f"低置信义项: {len(low_words)} | 违规词: {n_w} | 违规条目: {n_i}")
by_rule = defaultdict(int)
for w, v in issues.items():
    for x in v:
        rule = re.match(r"义项\d+(.+?)[:：]", x)
        by_rule[rule.group(1)[:10] if rule else x[:10]] += 1
for k, c in sorted(by_rule.items(), key=lambda kv: -kv[1]):
    print(f"  {c:5d}  {k}")

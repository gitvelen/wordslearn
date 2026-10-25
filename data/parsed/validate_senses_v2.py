"""senses v2 独立校验器（机器全量检查，不信生成方自检）：
覆盖 / 结构 / 字段硬约束 / 例句含目标词 / 同词释义去重 / 语域黑名单粗筛
用法: python3 -I validate_senses_v2.py   （在 data/parsed 目录）
"""
import json
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
POS_OK = {"n.", "v.", "vt.", "vi.", "adj.", "adv.", "prep.", "conj.", "pron.", "num.", "art.", "int.", "aux.", "modal."}
BANNED_ZH = ["威士忌", "朗姆", "鸡尾酒", "弹药", "赌", "海洛因", "可卡因", "大麻",
             "妓", "色情", "处决", "暗杀", "吗啡", "雪茄"]

# 常见不规则动词变形（词形归一用；覆盖初中词表）
IRR = {
    "go": "went gone goes going", "come": "came comes coming", "tell": "told tells telling",
    "lose": "lost loses losing", "pay": "paid pays paying", "leave": "left leaves leaving",
    "lead": "led leads leading", "break": "broke broken breaks breaking", "catch": "caught catches catching",
    "be": "am is are was were been being", "send": "sent sends sending", "make": "made makes making",
    "fly": "flew flown flies flying", "throw": "threw thrown throws throwing",
    "rise": "rose risen rises rising", "draw": "drew drawn draws drawing",
    "take": "took taken takes taking", "get": "got getting gets", "run": "ran runs running",
    "feel": "felt feels feeling", "keep": "kept keeps keeping", "sleep": "slept sleeps sleeping",
    "meet": "met meets meeting", "sell": "sold sells selling", "buy": "bought buys buying",
    "bring": "brought brings bringing", "think": "thought thinks thinking",
    "teach": "taught teaches teaching", "sit": "sat sits sitting", "stand": "stood stands standing",
    "understand": "understood understands understanding", "find": "found finds finding",
    "have": "had has having", "do": "did done does doing", "say": "said says saying",
    "see": "saw seen sees seeing", "eat": "ate eaten eats eating", "give": "gave given gives giving",
    "write": "wrote written writes writing", "ride": "rode ridden rides riding",
    "sing": "sang sung sings singing", "drink": "drank drunk drinks drinking",
    "swim": "swam swum swims swimming", "begin": "began begun begins beginning",
    "grow": "grew grown grows growing", "know": "knew known knows knowing",
    "wear": "wore worn wears wearing", "cut": "cuts cutting", "put": "puts putting",
    "read": "reads reading", "shut": "shuts shutting", "hurt": "hurts hurting",
    "let": "lets letting", "beat": "beaten beats beating", "build": "built builds building",
    "spend": "spent spends spending", "lend": "lent lends lending", "win": "won wins winning",
    "hold": "held holds holding", "hear": "heard hears hearing", "burn": "burnt burns burning",
    "stop": "stopped stops stopping", "beg": "begged begs begging", "die": "died dies dying",
    "become": "became becomes becoming", "fall": "fell fallen falls falling",
    "choose": "chose chosen chooses choosing", "speak": "spoke spoken speaks speaking",
    "steal": "stole stolen steals stealing", "wake": "woke wakes waking",
    "shake": "shook shaken shakes shaking", "slide": "slid slides sliding",
}

VARIABLE = {"be", "one's", "one’s", "oneself", "one’s self", "sb", "somebody", "do"}   # 短语可变成分（含弯引号）

def _forms(word):
    """目标词的词形族（小写）：直接构造 规则屈折（含去e/y变i/fe变ves/双写辅音）+ 不规则表。"""
    w = word.lower()
    out = {w}
    out.update(w + suf for suf in ("s", "es", "ed", "d", "ing", "ly", "er", "est", "ies"))
    if w.endswith("e"):                                   # 去 e 屈折 joke→joking/joked
        out.update([w[:-1] + "ing", w[:-1] + "d", w + "d"])
    if re.search(r"[^aeiou]y$", w):                       # y→ied/ies cry→cried/cries
        out.update([w[:-1] + "ied", w[:-1] + "ies"])
    if w.endswith(("f", "fe")):                           # f/fe→ves life→lives
        out.update([w.rstrip("e").rstrip("f") + "ves", w + "s"])
    if re.match(r".*[^aeiou][aeiou][ptndgmblr]$", w):     # 双写辅音 stop→stopped refer→referred
        out.update([w + w[-1] + "ed", w + w[-1] + "ing"])
    if w in IRR:
        out.update(IRR[w].split())
    return out

def word_forms_ok(word, sent):
    """例句含目标词（原形/规则或不规则变形/分隔式短语/可变成分短语实例）。"""
    s = sent.lower().replace("’", "'")
    word = word.lower().replace("’", "'")
    parts = [p for p in word.lower().split() if p not in VARIABLE]
    stem = parts[0] if parts else word.lower()            # 剔除可变成分后的实词主干
    if len(parts) <= 1:
        return any(re.search(rf"\b{re.escape(f)}\b", s) for f in _forms(stem))
    # 短语：首实词与末实词（各按词形归一）都出现即通过（容忍分隔式 take it out）
    head, tail = _forms(parts[0]), _forms(parts[-1])
    return (any(re.search(rf"\b{re.escape(f)}\b", s) for f in head)
            and any(re.search(rf"\b{re.escape(f)}\b", s) for f in tail))

def main():
    inputs = {json.loads(l)["word"]: json.loads(l) for l in
              (BASE / "gen_out" / "senses_v2_input.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    shard_files = sorted((BASE / "gen_out").glob("v2_shard_[0-9][0-9].jsonl"))
    errs = defaultdict(list)
    seen = set()
    n_senses = 0
    for f in shard_files:
        for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception as e:
                errs[str(f.name)].append(f"L{ln} JSON解析失败: {e}")
                continue
            w = d.get("word")
            tag = f"{f.name}:{w}"
            if w in seen:
                errs[tag].append("重复词")
            seen.add(w)
            if w not in inputs:
                errs[tag].append("不在输入清单")
                continue
            senses = d.get("senses") or []
            if not senses:
                errs[tag].append("无义项")
                continue
            n_senses += len(senses)
            zh_seen = set()
            for i, s in enumerate(senses, 1):
                if s.get("sense_order") != i:
                    errs[tag].append(f"义{i} sense_order={s.get('sense_order')} 不连续")
                if s.get("pos") not in POS_OK:
                    errs[tag].append(f"义{i} pos 非法: {s.get('pos')}")
                zh = s.get("definition_zh") or ""
                if len(zh) > 12:
                    errs[tag].append(f"义{i} 中文释义超12字: {zh}")
                if zh in zh_seen:
                    errs[tag].append(f"义{i} 中文释义与此前重复: {zh}")
                zh_seen.add(zh)
                en = s.get("definition_en") or ""
                if len(en.split()) > 14:
                    errs[tag].append(f"义{i} 英文释义超14词")
                ex = s.get("example_en") or ""
                seg = inputs[w]["seg"]
                limit = 10 if seg in ("P13", "P45") else 12
                if len(ex.split()) > limit:
                    errs[tag].append(f"义{i} 例句超{limit}词: {ex}")
                if ex and not word_forms_ok(w, ex):
                    errs[tag].append(f"义{i} 例句不含目标词: {ex}")
                if s.get("confidence") not in ("high", "low"):
                    errs[tag].append(f"义{i} confidence 非法: {s.get('confidence')}")
                for b in BANNED_ZH:
                    if b in zh:
                        errs[tag].append(f"义{i} 疑似语域违禁: {zh}")

    missing = set(inputs) - seen
    print(f"输入 {len(inputs)} 词 | 分片覆盖 {len(seen)} 词 | 义项 {n_senses} 条")
    if missing:
        print(f"缺失 {len(missing)} 词: {sorted(missing)[:20]}{'...' if len(missing) > 20 else ''}")
    if errs:
        print(f"\n违规 {sum(len(v) for v in errs.values())} 处，涉 {len(errs)} 词:")
        for k, v in list(errs.items())[:40]:
            print(f"  {k}: {'; '.join(v[:3])}")
        raise SystemExit(1)
    print("✅ 校验全部通过")

if __name__ == "__main__":
    main()

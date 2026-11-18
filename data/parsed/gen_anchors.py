#!/usr/bin/env python3
"""v4.3 音节切分 + 搭配锚 生成管线（规格见 ANCHORS_SPEC.md）。

L1 双源算音节数（cmudict 主锚 × IPA 交叉）→ L2 LLM 双跑（run1 规则式 / run2 示例式，
temperature=0，断点续跑）→ L3 裁决（一致且过校验收；不一致 judge 仲裁；REJECT/失败弃）。
搭配独立校验（含词/超纲/义1/双跑交集投票）。单音节词只生成搭配（n=1 无可切分）。
产物：word_anchors.jsonl + anchors_stats.json + anchors_review.csv。

用法: python3 -I gen_anchors.py            （data/parsed 目录）
      python3 -I gen_anchors.py --limit 50 （只跑前 50 个候选词，试跑校准用）
      python3 -I gen_anchors.py --stage summarize（不调 LLM，仅重汇总已有产物）
"""
import csv
import json
import random
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE.parent.parent / "app"))
import llm  # noqa: E402

DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"
OUT = BASE / "gen_out"
RUN1, RUN2 = OUT / "anchors_run1.jsonl", OUT / "anchors_run2.jsonl"
FINAL, STATS, REVIEW = (OUT / "word_anchors.jsonl", OUT / "anchors_stats.json",
                        OUT / "anchors_review.csv")
BATCH, WORKERS = 10, 3

# 搭配成分豁免表：小学 1-3 年级已掌握的最高频功能词/助动词/代词/数词（不依赖外部词频文件）
FUNCTION_WORDS = set("""a an the this that these those is are was were be been am being
do does did doing have has had having will would shall should can could may might must
not no nor yes i you he she it we they me him her us them my your his its our their
mine yours hers ours theirs myself yourself himself herself itself ourselves themselves
of to in on at for with from by about against between into through during before after
above below up down out off over under again further once here there when where why how
and or but so if because until while as than what which who whom whose
all any some few more most other such only own same too very just also
one two three four five six seven eight nine ten eleven twelve first second third
go goes went gone come came comes make makes made get gets got let say says said
see saw seen know knew known think want take took come here there now then ever never
always usually often sometimes ok okay oh wow please thanks thank hello goodbye
morning afternoon evening night day week month year time today tomorrow yesterday
""".split())

# ARPAbet 元音集（与 pipeline.ARP_VOWELS 一致，独立声明避免 import app 依赖）
ARP_VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY",
              "OW", "OY", "UH", "UW"}
IPA_VOWELS = set("ɑæəɒɔɚɜeɛiɪuʊʌ")         # 单字符元音（ː 不计；含英式ɒ与r-colored ɚ/ɜ）
IPA_DIPHTHS = ["aɪ", "aʊ", "eɪ", "ɔɪ", "oʊ", "əʊ", "ɪə", "eə", "ʊə"]  # 两字符合并为 1 节

_CMU = None


def _cmu():
    global _CMU
    if _CMU is None:
        import cmudict
        _CMU = cmudict.dict()
    return _CMU


# ---------- L1：音节数双源 ----------
def cmu_n(word):
    """cmudict ARPAbet 元音计数（主锚，无解析歧义）。缺词返回 None。"""
    phones = _cmu().get(word.lower())
    if not phones:
        return None
    return sum(1 for p in phones[0] if p.rstrip("012") in ARP_VOWELS)


def ipa_n(ph):
    """IPA 音标串元音计数（交叉源）：剥符号后贪心合并双元音，数元音单元。"""
    if not ph:
        return None
    ph = re.split(r"[,;]", ph)[0]
    ph = re.sub(r"[ˈˌː().*\s]", "", ph)
    i, n = 0, 0
    while i < len(ph):
        if ph[i:i + 2] in IPA_DIPHTHS:
            n += 1
            i += 2
        elif ph[i] in IPA_VOWELS:
            n += 1
            i += 1
        else:
            i += 1
    return n or None


# ---------- L1：切分硬校验 ----------
def check_split(word, syllables, n):
    if not isinstance(syllables, list) or not syllables:
        return "非列表或空"
    if any(not isinstance(s, str) or not s for s in syllables):
        return "空块"
    if "".join(syllables) != word:
        return "拼接不还原"
    if len(syllables) != n:
        return f"块数{len(syllables)}!=n{n}"
    return ""


# ---------- 搭配校验 ----------
def _norm(w):
    return re.sub(r"[.,!?;:\"()']", "", w.lower().strip())


def target_forms(word):
    """目标词原形+规则变形集合（含词校验用，思路同 pipeline._lemma_forms）。"""
    base = _norm(word)
    forms = {base}
    if base.endswith("y"):
        forms |= {base[:-1] + "ies", base[:-1] + "ier"}
    if base.endswith("e"):
        forms |= {base + "d", base + "r", base[:-1] + "ing"}
    else:
        forms |= {base + "s", base + "es", base + "ing", base + "ed", base + "d",
                  base + "er", base + "est", base[:-1] + "ing", base[:-1] + "ed"}
    return base, forms


def check_colloc(word, c, example_en, vocab_ok):
    """单条搭配校验，返回错误串（空=通过）。vocab_ok=在纲词白名单集合。"""
    if not isinstance(c, dict):
        return "结构错"
    en, zh = c.get("en"), c.get("zh")
    if not isinstance(en, str) or not isinstance(zh, str) or not en or not zh:
        return "字段缺失"
    toks = [_norm(t) for t in en.split()]
    if not (2 <= len(toks) <= 6):
        return f"词数{len(toks)}"
    base, forms = target_forms(word)
    if not any(t in forms for t in toks):
        return "不含目标词"
    if len(zh) > 10:
        return "zh超长"
    for t in toks:
        if t not in forms and t not in vocab_ok:
            return f"超纲词:{t}"
    if example_en and _norm(en) == _norm(example_en):
        return "与例句重复"
    return ""


# ---------- Prompt（两跑同规则不同框架）----------
RULES = """切分规则（自然拼读 phonics，严格执行）：
1. 形态学优先：前缀 un-/re-/dis-/pre-/per-/con-/com-/op-/sub- 等成块；后缀 -tion/-sion/-ic/-al/-ly/-er/-est/-ed/-ing/-ment/-ness/-ful/-less 整块不拆（opportunity → op|por|tu|ni|ty）
2. 二合字母 sh/ch/th/ph/wh/qu/ck/ng 不拆；双写辅音从中间切（letter → let|ter）
3. 单辅音归后一节（tiger → ti|ger 不是 tig|er）；辅音簇能做节首则整体归后（basket → bas|ket）
4. 辅音+-le 结尾成节（table → ta|ble, apple → ap|ple）
5. r-controlled 元音（ar/er/or/ir/ur）与 magic-e 不拆
6. 硬约束：块数必须等于给定的 n；所有块按顺序拼接必须逐字符还原原词（保留大小写）；无空块"""

PROMPT1 = """你是儿童英语自然拼读（phonics）教辅编辑，为小学中高年级中国学生做单词音节切分，并给抽象词/动词配高频搭配锚。

{rules}

任务：对下面每个词——
A. 若 n≥2：把单词切成 n 个音节块（syllables 数组，块拼接逐字符还原原词保留大小写）；n=1 则 syllables 为 null
B. collocations：1-2 条该词最常用的高频搭配（必须匹配给定的核心义），格式 {{"en":"≤6个小学词汇的英文短语且必须包含该词","zh":"≤10字中文"}}；具体名词等自解释词输出 null；与例句不同

词列表（ipa 为美式音标供参考）：
{items}

只输出 JSON 数组，**元素数量必须等于词数，每个词一个元素（字段可为 null，不可跳词）**：
[{{"word":"...","syllables":[...]|null,"collocations":[{{"en":"..","zh":".."}}]|null}}]"""

PROMPT2 = """你在给小学中高年级中国学生准备单词学习卡。参考以下已完成的范例，用完全相同的标准处理新词列表。

范例（注意切分位置与搭配选择）：
- beautiful (ipa bjuːtɪfl, n=3) → syllables ["beau","ti","ful"], collocations [{{"en":"a beautiful day","zh":"美丽的一天"}}]
- letter (ipa ˈletər, n=2) → syllables ["let","ter"], collocations [{{"en":"write a letter","zh":"写信"}}]
- tiger (ipa ˈtaɪɡər, n=2) → syllables ["ti","ger"], collocations [{{"en":"a big tiger","zh":"一只大老虎"}}]
- cinema (ipa ˈsɪnəmə, n=3) → syllables ["ci","ne","ma"], collocations [{{"en":"go to the cinema","zh":"去电影院"}}]
- network (ipa ˈnetwɜːrk, n=2) → syllables ["net","work"], collocations [{{"en":"a computer network","zh":"计算机网络"}}]
- upon (ipa əˈpɒn, n=2) → syllables ["u","pon"], collocations [{{"en":"once upon a time","zh":"从前"}}]

{rules}

新词列表（ipa 为美式音标供参考）：
{items}

搭配要求：1-2 条该词最常用的高频搭配（必须匹配给定的核心义），格式 {{"en":"≤6个小学词汇的英文短语且必须包含该词","zh":"≤10字中文"}}；具体名词等自解释词输出 null（与范例中简单词同理）。

只输出 JSON 数组，**元素数量必须等于词数，每个词一个元素（字段可为 null，不可跳词；简单词如 cat 的 syllables 为 null，但仍要输出该元素）**：

只输出 JSON 数组，每词一个元素：
[{{"word":"...","syllables":[...]|null,"collocations":[{{"en":"..","zh":".."}}]|null}}]"""

JUDGE_PROMPT = """你是儿童英语 phonics 教编审校。词：{word}（ipa {ipa}，音节数 n={n}）。
两个候选切分：
A: {a}
B: {b}
按自然拼读教学标准判断哪个正确，常见判据（按优先级）：
- 后缀 -tion/-sion/-ly/-er 等整块不拆（nation → na|tion 不是 nat|ion）
- 单辅音归后一节（cinema → ci|ne|ma 不是 cin|e|ma）
- 辅音+-le 结尾成节（table → ta|ble）
- 二合字母 sh/ch/th 不拆，双写辅音中间切（letter → let|ter）
- r-controlled 元音不拆
若两个都有明显错误（块数不对/违反规则）判 REJECT。只输出一行 JSON：{{"v":"A或B或REJECT","r":"一句话理由"}}"""


def parse_json_arr(text):
    """从 LLM 输出提取 JSON 数组（容错：截取首个 [ 到末个 ]）。"""
    if not text:
        return None
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e <= s:
        return None
    try:
        arr = json.loads(text[s:e + 1])
        return arr if isinstance(arr, list) else None
    except Exception:
        return None


def gen_batch(prompt_tpl, batch, attempt=1):
    """一批生成：返回 word(小写)→raw 元素 dict。失败重试 2 轮。"""
    items = json.dumps([{"word": w["word"], "ipa": w["ipa"], "n": w["n"],
                         "forms": w["forms"], "核心义": w["sense1"]}
                        for w in batch], ensure_ascii=False)
    try:
        text = llm.chat(prompt_tpl.format(rules=RULES, items=items),
                        max_tokens=3000, temperature=0)
    except Exception:
        if attempt < 3:
            time.sleep(5 * attempt)
            return gen_batch(prompt_tpl, batch, attempt + 1)
        return {}
    arr = parse_json_arr(text)
    out = {}
    if arr:
        for el in arr:
            if isinstance(el, dict) and isinstance(el.get("word"), str):
                out[el["word"].strip().lower()] = el
    missing = [w for w in batch if w["word"].lower() not in out]
    if missing and attempt < 3:
        time.sleep(3)
        out.update(gen_batch(prompt_tpl, missing, attempt + 1))
    return out


def judge_word(word, ipa, n, a, b):
    try:
        text = llm.chat(JUDGE_PROMPT.format(word=word, ipa=ipa or "-", n=n,
                                            a=json.dumps(a, ensure_ascii=False),
                                            b=json.dumps(b, ensure_ascii=False)),
                        max_tokens=200, temperature=0)
        m = re.search(r"\{[^{}]*\}", text or "")
        j = json.loads(m.group(0)) if m else {}
        return j.get("v") if j.get("v") in ("A", "B", "REJECT") else "REJECT"
    except Exception:
        return "REJECT"


def load_done(path):
    done = {}
    if path.exists():
        for l in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(l)
                done[d["word"].lower()] = d
            except Exception:
                continue
    return done


def main():
    limit = 10 ** 9
    stage = "gen"
    args = sys.argv[1:]
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--stage" in args:
        stage = args[args.index("--stage") + 1]

    # ---- 加载词库与义1/例句/词形 ----
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT w.id, w.word, w.phonetic_uk, w.phonetic_us, w.forms,
                                (SELECT pos||' '||definition_zh FROM word_senses s
                                 WHERE s.word_id=w.id ORDER BY sense_order LIMIT 1) AS sense1,
                                (SELECT example_en FROM word_senses s
                                 WHERE s.word_id=w.id ORDER BY sense_order LIMIT 1) AS ex1
                         FROM words w ORDER BY w.id""").fetchall()
    # 在纲词白名单：主库词表 ∪ 高频功能词豁免表（搭配成分超纲校验）
    vocab_ok = {r["word"].lower() for r in rows} | FUNCTION_WORDS

    # ---- L1：双源音节数 + 范围筛选 ----
    cands, phrase, manual = [], [], []
    n_mono = 0
    for r in rows:
        w = r["word"]
        if re.search(r"[\s-]", w):
            phrase.append(w)
            continue
        ipa = r["phonetic_us"] or r["phonetic_uk"]
        c1, c2 = cmu_n(w), ipa_n(ipa)
        if c1 is None or c2 is None or c1 != c2:
            manual.append({"word": w, "ipa": ipa, "cmu": c1, "ipa_n": c2,
                           "why": "cmu缺" if c1 is None else
                                  "ipa缺" if c2 is None else f"两源不一致{c1}vs{c2}"})
            continue
        if c1 == 1:
            n_mono += 1                      # 单音节：无切分，仍生成搭配
        cands.append({"word": w, "ipa": ipa or "", "n": c1, "forms": r["forms"],
                      "sense1": r["sense1"] or "", "ex1": r["ex1"] or ""})
    cands = cands[:limit]
    print(f"共 {len(rows)} 词：候选 {len(cands)}（截取 {limit}，含单音节 {n_mono}）"
          f" 短语 {len(phrase)} 两源异常 {len(manual)}")

    # ---- L2：双跑（断点续跑）----
    d1, d2 = load_done(RUN1), load_done(RUN2)
    todo = [w for w in cands if w["word"].lower() not in d1 or w["word"].lower() not in d2]
    if stage == "gen" and todo:
        batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
        print(f"双跑待生成 {len(todo)} 词，{len(batches)} 批×2 跑")
        t0 = time.time()

        def run(i):
            b = batches[i]
            return gen_batch(PROMPT1, b), gen_batch(PROMPT2, b)

        def flush_out(new1, new2):
            for path, nd in ((RUN1, new1), (RUN2, new2)):
                if nd:
                    with path.open("a", encoding="utf-8") as f:
                        for k, v in sorted(nd.items()):
                            f.write(json.dumps(v, ensure_ascii=False) + "\n")

        new1, new2 = {}, {}
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for i, (r1, r2) in enumerate(ex.map(run, range(len(batches)))):
                d1.update(r1)
                d2.update(r2)
                new1.update(r1)
                new2.update(r2)
                if (i + 1) % 5 == 0:
                    flush_out(new1, new2)
                    new1, new2 = {}, {}
                    print(f"  {i + 1}/{len(batches)} 批，{time.time() - t0:.0f}s")
        flush_out(new1, new2)

    # ---- L3：裁决 + 搭配校验 + 产物 ----
    final, judged, dropped = [], [], []
    n_agree = n_judge_ok = n_judge = n_drop = n_coll = 0
    syll_cands = [w for w in cands if w["n"] >= 2]
    for w in cands:
        wl = w["word"].lower()
        e1, e2 = d1.get(wl), d2.get(wl)
        if not e1 or not e2:
            dropped.append([w["word"], w["ipa"], w["n"], "", "生成缺失"])
            n_drop += 1
            continue
        # 切分（仅 n≥2）：双跑一致且过校验 → AGREE；不一致 → judge 仲裁
        syll, tag = None, None
        if w["n"] >= 2:
            s1, s2 = e1.get("syllables"), e2.get("syllables")
            err1 = check_split(w["word"], s1, w["n"]) if s1 else "run1无切分"
            err2 = check_split(w["word"], s2, w["n"]) if s2 else "run2无切分"
            if not err1 and not err2 and s1 == s2:
                syll = s1
                n_agree += 1
            else:
                v = judge_word(w["word"], w["ipa"], w["n"], s1, s2)
                n_judge += 1
                pick = s1 if v == "A" else s2 if v == "B" else None
                errp = check_split(w["word"], pick, w["n"]) if pick else "REJECT"
                if pick and not errp:
                    syll = pick
                    n_judge_ok += 1
                    judged.append([w["word"], w["ipa"], w["n"],
                                   f"A:{s1}|B:{s2}", f"judge:{v}"])
                else:
                    dropped.append([w["word"], w["ipa"], w["n"],
                                    f"A:{s1}({err1})|B:{s2}({err2})", f"judge:{v} {errp}"])
                    n_drop += 1
        # 搭配：双跑 en 交集投票 + 硬校验（投票 key=归一整串或剥首尾冠词串）
        c_pool = {}

        def vote_keys(en):
            k = _norm(en)
            toks = k.split()
            if len(toks) > 1 and toks[0] in ("a", "an", "the"):
                toks = toks[1:]
            if len(toks) > 1 and toks[-1] in ("a", "an", "the"):
                toks = toks[:-1]
            return {k, " ".join(toks)}

        for e in (e1, e2):
            for c in (e.get("collocations") or []):
                if isinstance(c, dict) and isinstance(c.get("en"), str):
                    for k in vote_keys(c["en"]):
                        c_pool.setdefault(k, []).append(c)
        picked, seen_en = [], set()
        for k, lst in c_pool.items():
            if len(lst) < 2:                 # 只在某一跑出现 → 不收（双跑投票）
                continue
            c = lst[0]
            if check_colloc(w["word"], c, w["ex1"], vocab_ok):
                continue
            if _norm(c["en"]) in seen_en:    # 同一搭配可能命中 k/stripped 两池
                continue
            seen_en.add(_norm(c["en"]))
            picked.append({"en": c["en"].strip(), "zh": c["zh"].strip()})
            if len(picked) == 2:
                break
        if picked:
            n_coll += 1
        if syll or picked:
            final.append({"word": w["word"], "syllables": syll,
                          "collocations": picked or None})

    FINAL.write_text("".join(json.dumps(d, ensure_ascii=False) + "\n"
                             for d in final), encoding="utf-8")
    stats = {"total": len(rows), "candidates": len(cands), "monosyllabic": n_mono,
             "syll_cands": len(syll_cands), "phrase": len(phrase),
             "manual_src": len(manual), "agree": n_agree, "judged_ok": n_judge_ok,
             "judge_calls": n_judge, "dropped": n_drop,
             "syll_ok": n_agree + n_judge_ok, "coll_ok": n_coll,
             "agree_rate": round(n_agree / max(len(syll_cands), 1), 3),
             "drop_rate": round(n_drop / max(len(syll_cands), 1), 3)}
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False))

    # ---- 人工核验清单：JUDGED/DROPPED/MANUAL/PHRASE 全量 + AGREE 随机 100 ----
    with REVIEW.open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(["word", "ipa", "n", "detail", "tag", "note"])
        for row in judged:
            wr.writerow([*row, "JUDGED", "仲裁通过，请人工确认"])
        for row in dropped:
            wr.writerow([*row, "DROPPED", "已弃，请人工确认弃得对"])
        for m in manual:
            wr.writerow([m["word"], m["ipa"] or "", m["cmu"] or m["ipa_n"] or "",
                         f"cmu={m['cmu']} ipa={m['ipa_n']}", "MANUAL", m["why"]])
        for w in phrase:
            wr.writerow([w, "", "", "", "PHRASE", "首批不生成（计数见 stats）"])
        agree_words = [d for d in final if d["syllables"]]
        for d in random.sample(agree_words, min(100, len(agree_words))):
            wr.writerow([d["word"], "", "", json.dumps(d["syllables"], ensure_ascii=False),
                         "AGREE_SAMPLE", json.dumps(d["collocations"], ensure_ascii=False)])
    print(f"产物：{FINAL.name} {len(final)} 行 / {STATS.name} / {REVIEW.name}")


if __name__ == "__main__":
    main()

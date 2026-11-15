#!/usr/bin/env python3
"""L2 全量走查：LLM judge 审 listen_def 题歧义（规则层算不出语义近义/互译）。

对 gen_out/def_quiz.jsonl 每题判定："孩子听到该英文释义后，是否存在除标准答案外
也可合理选择的选项"。temperature=0；自评 confidence!=high 的题双跑投票，
verdict 不一致标 UNSTABLE。结果断点续跑（gen_out/def_judge.jsonl，key=word|idx），
汇总 gen_out/def_review.csv（SUSPECT/UNSTABLE 置顶，供人工复核回注
gen_def_quiz.DEF_EXCLUDE_PAIRS 后重生成）。

用法: python3 -I judge_def_quiz.py          （data/parsed 目录，app 目录需可 import llm）
      python3 -I judge_def_quiz.py --limit N （只判前 N 题，试跑用）
"""
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE.parent.parent / "app"))
import llm  # noqa: E402

SRC = BASE / "gen_out" / "def_quiz.jsonl"
DONE = BASE / "gen_out" / "def_judge.jsonl"
REVIEW = BASE / "gen_out" / "def_review.csv"
BATCH = 12
WORKERS = 3

PROMPT = """你是儿童英语教辅审校（受众：6-9 岁中国孩子）。题型：孩子听到一段英文释义音频，
从 4 个英文单词中选出该释义对应的单词。判断每题是否存在"除标准答案外，孩子也可能
合理选择的选项"——近义（hot/warm）、上下位（animal/dog）、中英互译歧义、或释义对某干扰
词同样成立。

题目列表：
{items}

对每题输出一行 JSON（共 {n} 行，别输出其他文字）：
{{"i":<题号>,"v":"OK或SUSPECT","c":"high或low","r":"SUSPECT 时一句话说明哪个选项为何歧义，OK 时空串"}}"""


def key(d):
    return f"{d['word']}|{d['idx']}"


def parse_lines(text, keyset):
    out = {}
    for m in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            j = json.loads(m.group(0))
            i = j.get("i")
            if i in keyset and ("v" in j):
                out[i] = j
        except Exception:
            continue
    return out


def judge_batch(batch, attempt=1):
    items = json.dumps([{"i": k, "definition": d["stem"], "options": d["options"],
                         "answer": d["answer"]} for k, d in batch], ensure_ascii=False)
    try:
        text = llm.chat(PROMPT.format(items=items, n=len(batch)), max_tokens=3000, temperature=0)
    except Exception as e:
        if attempt < 3:
            time.sleep(5 * attempt)
            return judge_batch(batch, attempt + 1)
        return {k: {"v": "FAILED", "c": "low", "r": str(e)[:80]} for k, _ in batch}
    got = parse_lines(text, {k for k, _ in batch})
    # 缺行视为失败重试一轮，再缺标 FAILED
    missing = [k for k, _ in batch if k not in got]
    if missing and attempt < 3:
        time.sleep(3)
        retry = judge_batch([(k, d) for k, d in batch if k in missing], attempt + 1)
        got.update(retry)
    for k, _ in batch:
        if k not in got:
            got[k] = {"v": "FAILED", "c": "low", "r": "输出缺行"}
    return got


def main():
    limit = 10 ** 9
    if "--limit" in sys.argv[1:]:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    quizzes = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines()][:limit]
    done = {}
    if DONE.exists():
        for l in DONE.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(l)
                done[d["key"]] = d
            except Exception:
                continue
    todo = [(k, d) for k, d in ((key(d), d) for d in quizzes)
            if done.get(k, {}).get("v") != "OK"]   # 只认 OK 为已判：
            # SUSPECT/UNSTABLE 旧记录会挡住重生成题（同 key 新题）的重判
    print(f"共 {len(quizzes)} 题，已判 {len(done)}，待判 {len(todo)}")

    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    results = {}
    t0 = time.time()

    def run(i_b):
        b = batches[i_b]
        first = judge_batch(b)
        # 低置信双跑投票（temperature=0 仍可能边界抖动）
        second_keys = [k for k, _ in b if first.get(k, {}).get("c") != "high"]
        if second_keys:
            m = {k: d for k, d in b if k in second_keys}
            second = judge_batch(list(m.items()))
            for k in second_keys:
                if second.get(k, {}).get("v") and second[k]["v"] != first[k]["v"]:
                    first[k] = {"v": "UNSTABLE", "c": "low",
                                "r": f"两跑不一致: {first[k].get('v')}/{second[k].get('v')} {first[k].get('r', '')[:60]}"}
        return first

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, res in zip(range(len(batches)), ex.map(run, range(len(batches)))):
            results.update(res)
            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(batches)} 批，{time.time() - t0:.0f}s")
    with DONE.open("a", encoding="utf-8") as f:
        for k, v in results.items():
            f.write(json.dumps({"key": k, **v}, ensure_ascii=False) + "\n")

    # 汇总 CSV（复核清单：SUSPECT/UNSTABLE/FAILED 置顶）
    verd = {}
    for d in quizzes:
        j = done.get(key(d)) or results.get(key(d)) or {}
        verd[key(d)] = (j.get("v", "?"), j.get("r", ""))
    order = {"SUSPECT": 0, "UNSTABLE": 1, "FAILED": 2, "?": 3, "OK": 4}
    with REVIEW.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["word", "sense_order", "definition_en", "options", "answer", "verdict", "reason"])
        for d in sorted(quizzes, key=lambda d: (order.get(verd[key(d)][0], 9), d["word"])):
            v, r = verd[key(d)]
            w.writerow([d["word"], d["idx"], d["stem"], "|".join(d["options"]), d["answer"], v, r])
    stat = {}
    for v, _ in verd.values():
        stat[v] = stat.get(v, 0) + 1
    print(f"判定完成 {dict(sorted(stat.items()))} → {REVIEW}")


if __name__ == "__main__":
    main()

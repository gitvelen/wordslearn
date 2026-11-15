#!/usr/bin/env python3
"""L3 音频走查：ASR 回验释义音频内容（gen_def_tts 产物的内容级检查——_ok 只拦指纹/
截断，拦不住"读错内容"；glm-asr 转写音频与 def_en 原文比对）。

用法（data/parsed 目录）:
  python3 -I verify_def_tts.py --calibrate 50   # 校准：打印相似度分布，人工定阈值
  python3 -I verify_def_tts.py                   # 全量回验，输出 flag 清单
flag 分流（对抗审查 P2-3）：指纹/时长异常 → 删文件重生成可修（gen_def_tts 幂等）；
ASR 系统性转写偏差（同音词 their/there、连读）→ 豁免清单 DEF_ASR_EXCLUDE，重生成
不改文本不无限重试。
"""
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "app"))
import llm  # noqa: E402

BASE = Path(__file__).parent
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"
AUDIO = BASE.parent.parent / "app" / "data" / "audio" / "def"
THRESHOLD = 0.88
WORKERS = 2          # glm-asr 限流敏感（429），低并发+节流+长退避
# 人工复核后的豁免（word__order）——ASR 确定性偏差，音频实际正确
DEF_ASR_EXCLUDE = set()

NUM_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
             "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten"}


def norm(s):
    s = (s or "").casefold()
    s = re.sub(r"\d+", lambda m: NUM_WORDS.get(m.group(0), m.group(0)), s)
    return " ".join(re.findall(r"[a-z]+", s))


def main():
    calibrate = 0
    if "--calibrate" in sys.argv[1:]:
        calibrate = int(sys.argv[sys.argv.index("--calibrate") + 1])
    sample = 0
    if "--sample" in sys.argv[1:]:      # 随机抽样（分层质检：全量被限流时降级）
        sample = int(sys.argv[sys.argv.index("--sample") + 1])
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [(r["word"], r["sense_order"], r["definition_en"]) for r in db.execute(
        """SELECT wd.word, s.sense_order, s.definition_en FROM word_senses s
           JOIN words wd ON wd.id=s.word_id WHERE s.definition_en IS NOT NULL
           ORDER BY wd.id, s.sense_order""")]
    db.close()
    items = []
    for w, n, text in rows:
        f = AUDIO / f"{w}__{n}.mp3"
        if f.exists() and f"{w}__{n}" not in DEF_ASR_EXCLUDE:
            items.append((w, n, text, f))
    if calibrate:
        items = items[:calibrate]
    if sample and sample < len(items):
        import random as _r
        _r.Random(20261115).shuffle(items)
        items = items[:sample]
    print(f"回验 {len(items)} 条释义音频（阈值 {THRESHOLD}）", flush=True)

    results = []

    def check(it):
        w, n, text, f = it
        err = ""
        for attempt in range(5):
            try:
                got = llm.transcribe(f.read_bytes(), "audio.mp3")
                time.sleep(0.8)                    # 基础节流
                sim = SequenceMatcher(None, norm(text), norm(got)).ratio()
                return (w, n, text, got, sim)
            except Exception as e:
                err = str(e)[:60]
                # 429 限流：长退避（与 LLM chat 共用 key，叠加时尤甚）
                time.sleep(30 if "429" in err else 3 * (attempt + 1))
        return (w, n, text, f"TRANSCRIBE-FAIL: {err}", -1)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, res in enumerate(ex.map(check, items)):
            results.append(res)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(items)}，{time.time() - t0:.0f}s", flush=True)

    sims = sorted(r[4] for r in results if r[4] >= 0)
    fails = [r for r in results if r[4] < 0]
    flags = [r for r in results if 0 <= r[4] < THRESHOLD]
    print(f"完成 {len(results)} 条，耗时 {time.time() - t0:.0f}s", flush=True)
    if sims:
        print(f"相似度分布：min={sims[0]:.3f} p1={sims[max(0, len(sims)//100)]:.3f} "
              f"p5={sims[max(0, len(sims)//20)]:.3f} 中位={sims[len(sims)//2]:.3f}", flush=True)
    print(f"转写失败 {len(fails)} 条；低于阈值 {THRESHOLD} 的 flag {len(flags)} 条：", flush=True)
    for w, n, text, got, sim in sorted(flags, key=lambda r: r[4])[:80]:
        print(f"  {w}__{n} sim={sim:.2f}\n    原文: {text}\n    转写: {got}", flush=True)
    if fails:
        print("转写失败（网络/服务）：", flush=True)
        for w, n, _, got, _ in fails[:20]:
            print(f"  {w}__{n} {got}", flush=True)
    report = BASE / "gen_out" / "def_asr_report.txt"
    with report.open("w", encoding="utf-8") as f:
        for w, n, text, got, sim in sorted(results, key=lambda r: r[4]):
            f.write(f"{w}__{n}\t{sim:.3f}\t{text}\t{got}\n")
    print(f"明细 → {report}", flush=True)


if __name__ == "__main__":
    main()

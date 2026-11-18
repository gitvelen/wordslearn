#!/usr/bin/env python3
"""v4.3 权威切分抓取：Longman Dictionary of Contemporary English (ldoceonline.com)。

对主库单词（非短语）逐词抓词条页，提取 headword 音节切分（‧ 分隔）与
COLLOCATIONS 板块。断点续跑。产物 gen_out/ldoce_raw.jsonl：{word, syllables, collocs}。

用法: python3 -I fetch_ldoce.py           （全量缺词）
      python3 -I fetch_ldoce.py --limit 20
"""
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
OUT = BASE / "gen_out" / "ldoce_raw.jsonl"
DB = BASE.parent.parent / "app" / "data" / "wordslearn.db"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
SPLIT_RE = re.compile(r"[A-Za-z][A-Za-z‧]*‧[A-Za-z][A-Za-z‧]*")   # 至少一个 ‧


def fetch(word):
    url = "https://www.ldoceonline.com/dictionary/" + urllib.parse.quote(
        word.replace(" ", "-"))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "ignore")


def extract(html, word):
    """返回 (syllables|None, collocs[list])。切分取去‧后==word 的首个形态。"""
    target = word.lower()
    syl = None
    for m in SPLIT_RE.findall(html):
        cand = m.replace("‧", "").lower()
        if cand == target:
            parts = [p for p in m.split("‧") if p]
            if len(parts) >= 2:
                syl = parts
                break
    # COLLOCATIONS 板块：粗提取（英文短语行），供搭配对照，非结构化
    collocs = []
    seg = html.split("COLLOCATIONS")
    if len(seg) >= 2:
        text = re.sub(r"<[^>]+>", " ", seg[1][:6000])
        text = re.sub(r"\s+", " ", text)
        collocs = text.strip()[:400]
    return syl, collocs


def main():
    import urllib.parse
    limit = 10 ** 9
    if "--limit" in sys.argv[1:]:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    done = {}
    if OUT.exists():
        for l in OUT.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(l)
                done[d["word"]] = d
            except Exception:
                continue
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    words = [r[0] for r in db.execute(
        "SELECT word FROM words WHERE word NOT LIKE '% %' AND word NOT LIKE '%-%' "
        "ORDER BY id")]
    todo = [w for w in words if w not in done][:limit]
    print(f"共 {len(words)} 单词，已抓 {len(done)}，待抓 {len(todo)}", flush=True)
    t0, got, miss = time.time(), 0, 0
    with OUT.open("a", encoding="utf-8") as f:
        for i, w in enumerate(todo):
            rec = {"word": w, "syllables": None, "collocs": ""}
            for attempt in (1, 2):
                try:
                    syl, col = extract(fetch(w), w)
                    rec["syllables"], rec["collocs"] = syl, col
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        break                            # 无词条
                    time.sleep(2 * attempt)
                except Exception:
                    time.sleep(2 * attempt)
            if rec["syllables"]:
                got += 1
            else:
                miss += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(todo)}，切分 {got} 无 {miss}，{time.time() - t0:.0f}s",
                      flush=True)
            time.sleep(0.6)
    print(f"完成：切分 {got} / 无 {miss}，共 {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""fetch_vocab.py v2 — 上海牛津版词表获取器
  python fetch_vocab.py collect   # 列表页收集单词+详情页ID（秒级）
  python fetch_vocab.py details   # 详情页抓取（断点续传，每词0.9s限速，可后台跑）
  python fetch_vocab.py junior    # 初中考纲PDF解析（词性跨行状态机）
  python fetch_vocab.py report    # 统计
"""
import sys, re, json, time, urllib.request, urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent
RAW, PARSED = BASE/"data"/"raw", BASE/"data"/"parsed"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "zh-CN,zh;q=0.9"}

KOOLEARN_TAGS = [("primary_1_3", "tag_1780"), ("primary_4_5", "tag_1795")]
WORD_LIST = PARSED/"word_list.json"
DETAILS = PARSED/"koolearn_details.jsonl"

def http_get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:
            print(f"  retry {i+1}/{tries}: {e}", flush=True)
            time.sleep(2*(i+1))
    return None

def cmd_collect():
    PARSED.mkdir(parents=True, exist_ok=True)
    all_words = {}
    for name, tag in KOOLEARN_TAGS:
        p = 1
        while p <= 60:
            body = http_get(f"https://www.koolearn.com/dict/{tag}_{p}.html")
            if body is None:
                break
            html = body.decode("utf-8", "ignore")
            links = re.findall(r'<a[^>]*href="/dict/(wd_\d+)\.html"[^>]*>\s*([A-Za-z][A-Za-z\'\s-]{1,25})\s*</a>', html)
            if not links:
                print(f"[{name}] p{p} 无词条链接，停止", flush=True)
                break
            new = 0
            for wid, w in links:
                w = w.strip().lower()
                if w and w not in all_words:
                    all_words[w] = {"wd_id": wid, "grade": name}
                    new += 1
            print(f"[{name}] p{p}: {len(links)}链接, 新增{new}", flush=True)
            p += 1
            time.sleep(1.0)
    WORD_LIST.write_text(json.dumps(all_words, ensure_ascii=False, indent=0), "utf-8")
    print(f"总计 {len(all_words)} 词 → {WORD_LIST}")

def parse_detail(html):
    d = {}
    m = re.search(r"英\s*\[([^\]]+)\]", html); d["phonetic_uk"] = m.group(1) if m else None
    m = re.search(r"美\s*\[([^\]]+)\]", html); d["phonetic_us"] = m.group(1) if m else None
    m = re.search(r'data-url="(//picflow[^"]+\.mp3)"', html)
    d["mp3"] = "https:" + m.group(1) if m else None
    # 中文释义：details-content-title-box 内的 prop + spans
    m = re.search(r'details-content-title-box(.{0,3000}?)<h2 class="details-content-title"', html, re.S)
    if m:
        block = m.group(1)
        props = re.findall(r'<span class="prop">([^<]+)</span>', block)
        defs = re.findall(r'<p>(.*?)</p>', block, re.S)
        parts = []
        for pr, df in zip(props, defs):
            zh = "".join(re.findall(r'<span>([^<]*)</span>', df)).strip()
            if zh:
                parts.append(f"{pr.strip()} {zh}")
        d["definition_zh"] = " | ".join(parts) if parts else None
    else:
        d["definition_zh"] = None
    # 英英释义（取前2条 def 拼接）
    defs_en = re.findall(r'<span class="def">([^<]+)</span>', html)
    d["definition_en"] = " / ".join(defs_en[:2]) if defs_en else None
    # 例句（英英区的引用例句）
    ex = re.findall(r'<em>"([^"]{10,160})"</em>', html)
    d["example_en"] = ex[0] if ex else None
    return d

def cmd_details():
    if not WORD_LIST.exists():
        print("先跑 collect"); return
    words = json.loads(WORD_LIST.read_text(encoding="utf-8"))
    done = set()
    if DETAILS.exists():
        for line in DETAILS.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["word"])
            except Exception:
                pass
    todo = [(w, info) for w, info in words.items() if w not in done]
    print(f"共{len(words)}词, 已完成{len(done)}, 待抓{len(todo)}", flush=True)
    with DETAILS.open("a", encoding="utf-8") as fo:
        for i, (w, info) in enumerate(todo):
            body = http_get(f"https://www.koolearn.com/dict/{info['wd_id']}.html")
            if body is None:
                print(f"  ✗ {w} 下载失败，跳过", flush=True)
                time.sleep(1)
                continue
            d = parse_detail(body.decode("utf-8", "ignore"))
            d.update({"word": w, "grade": info["grade"], "wd_id": info["wd_id"]})
            fo.write(json.dumps(d, ensure_ascii=False) + "\n")
            fo.flush()
            if (i+1) % 25 == 0:
                print(f"  进度 {i+1}/{len(todo)}", flush=True)
            time.sleep(0.9)
    print("详情抓取完成", flush=True)

POS = r"(?:n|v|adj|adv|prep|conj|pron|num|art|int|abbr|aux|vt|vi|modal)\."

def cmd_junior():
    pdf = RAW/"junior_2020.pdf"
    if not pdf.exists():
        print("缺 data/raw/junior_2020.pdf"); return
    import pdfplumber
    with pdfplumber.open(pdf) as d:
        text = "\n".join(pg.extract_text() or "" for pg in d.pages)
    if len(text) < 1000:
        print("⚠ PDF无文本层(疑似扫描版)"); return
    entries, cur = [], None
    def push():
        nonlocal cur
        if cur and cur["definition_zh"]:
            entries.append(cur)
        cur = None
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        # PDF提取时词性与点号被空格断开（"n ."），归一化为 "n."
        ln = re.sub(r"([A-Za-z]) \.(?=\s|$)", r"\1.", ln)
        # 纯词性开头的续行（如 "prep. 关于" / "n. 事故"）
        if re.match(rf"^{POS}", ln):
            if cur:
                cur["definition_zh"] += "；" + ln
            continue
        # 纯中文续行
        if re.match(r"^[一-鿿]", ln):
            if cur:
                cur["definition_zh"] += ln
            continue
        word = zh = None
        # 严格式：词 + 词性. + 中文（非贪婪词组，词性锚定——"ice cream n. 冰淇淋" 与 "ability n. 能力" 都正确）
        m = re.match(rf"^([A-Za-z][A-Za-z'’\- ]*?)\s+({POS})\s+([一-鿿].*)$", ln)
        if m and len(m.group(1)) <= 30:
            word, zh = m.group(1), f"{m.group(2)} {m.group(3)}"
        else:
            # 跨行式：词 + 词性 单独成行，释义在下一行（如 "above prep." + "在……上面"）
            m3 = re.match(rf"^([A-Za-z][A-Za-z'’\- ]*?)\s+({POS})$", ln)
            if m3 and len(m3.group(1)) <= 30:
                push()
                cur = {"word": m3.group(1), "definition_zh": m3.group(2), "source": "junior_2020"}
                continue
            # 无词性式：词 + 中文
            m = re.match(r"^([A-Za-z][A-Za-z'’\- ]*?)\s+([一-鿿].*)$", ln)
            if m and len(m.group(1)) <= 30:
                word, zh = m.group(1), m.group(2)
        if word and not re.match(r"^[A-Z]{2,}", word):
            push()
            cur = {"word": word, "definition_zh": zh, "source": "junior_2020"}
    push()
    # 自检：词尾不得粘词性
    bad = [e["word"] for e in entries if re.search(rf"\s(?:n|v|adj|adv|prep|conj|pron|num|art|int|abbr|aux|vt|vi|modal)$", e["word"])]
    if bad:
        print(f"⚠ 自检失败: {len(bad)} 个词尾粘词性: {bad[:5]}")
    out = PARSED/"junior_2020.jsonl"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries), "utf-8")
    print(f"初中考纲: {len(entries)} 词条 → {out.name}")
    for e in entries[:5]:
        print("  ✓", e["word"], e["definition_zh"][:40])

def cmd_frq():
    """用 wordfreq 生成全库词频排名 → data/parsed/frq.csv（宿主机跑；容器 seed frq 消费）"""
    import sqlite3
    from wordfreq import zipf_frequency
    db = sqlite3.connect(BASE / "app" / "data" / "wordslearn.db")
    words = [r[0] for r in db.execute("SELECT word FROM words")]
    db.close()
    scored = sorted(((w, zipf_frequency(w, "en")) for w in words), key=lambda x: -x[1])
    out = PARSED / "frq.csv"
    with out.open("w", encoding="utf-8") as f:
        for rank, (w, _z) in enumerate(scored, 1):
            f.write(f"{w},{rank}\n")
    print(f"词频表: {len(scored)} 词 → {out}")
    print("最高频前10:", [w for w, _ in scored[:10]])
    print("最低频后5:", [w for w, _ in scored[-5:]])


def cmd_report():
    wl = json.loads(WORD_LIST.read_text(encoding="utf-8")) if WORD_LIST.exists() else {}
    print(f"列表词数: {len(wl)}")
    for seg in ("primary_1_3", "primary_4_5"):
        n = sum(1 for v in wl.values() if v["grade"] == seg)
        print(f"  {seg}: {n}")
    if DETAILS.exists():
        lines = DETAILS.read_text(encoding="utf-8").splitlines()
        print(f"详情已抓: {len(lines)}")
        ok = sum(1 for l in lines if json.loads(l).get("definition_zh"))
        mp3 = sum(1 for l in lines if json.loads(l).get("mp3"))
        print(f"  有中文释义: {ok}, 有mp3: {mp3}")
    j = PARSED/"junior_2020.jsonl"
    if j.exists():
        print(f"初中考纲: {len(j.read_text(encoding='utf-8').splitlines())} 词条")

if __name__ == "__main__":
    {"collect": cmd_collect, "details": cmd_details,
     "junior": cmd_junior, "frq": cmd_frq, "report": cmd_report}[sys.argv[1]]()

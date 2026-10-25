"""校对工作台数据生成：senses jsonl → app/static/review_data.js
低置信词排前。用法: python3 -I gen_review_page.py（在 data/parsed 目录）"""
import json
from pathlib import Path

BASE = Path(__file__).parent
rows = []
for line in (BASE / "word_senses.jsonl").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line:
        continue
    rows.append(json.loads(line))

# 聚合到词级
words = {}
for d in rows:
    words.setdefault(d["word"], []).append(d)
out = []
for w, ss in words.items():
    ss.sort(key=lambda s: s["sense_order"])
    # 词级置信 = 任一义 low 即 low（低置信多在非核心义，核心义正常不代表整个词条无需复核）
    word_conf = "low" if any(s.get("confidence") == "low" for s in ss) else "high"
    out.append({
        "word": w, "phonetic_uk": "", "seg": "", "confidence": word_conf,
        "senses": [{k: s.get(k) for k in ("sense_order", "pos", "definition_zh", "definition_en",
                                          "example_en", "example_zh", "confidence")} for s in ss],
    })
out.sort(key=lambda x: (x["confidence"] != "low", x["word"]))   # low 在前
dest = Path("/root/wordslearn/app/static/review_data.js")
dest.write_text("window.REVIEW = " + json.dumps(out, ensure_ascii=False) + ";\n", encoding="utf-8")
low = sum(1 for x in out if x["confidence"] == "low")
print(f"review_data.js: {len(out)} 词（低置信 {low}）→ {dest}")

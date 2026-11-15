"""edge-tts 批量生成「英文释义音频」→ app/data/audio/def/{word}__{sense_order}.mp3

listen_def 题型的听音源。输入 word_senses 全量 definition_en（3581 义，含污染义——
音频无害冗余，审计/回退朗读可用）。仿 gen_tts_audio.py 的自检循环模式：
缺失或校验不过才生成，生成后自检，3 次重试，失败记 def_tts_failed.txt。

DUR_MAX 独立 = 8.0s（释义 ≤14 词，实测最长例句 p99 4.1s，线性外推 ≤5s，余量 >60%；
沿用单词口径 3.5s 会把全部释义音判废无限重生成——对抗审查 P2-3）。

用法: python3 -I gen_def_tts.py            （data/parsed 目录；全量）
      python3 -I gen_def_tts.py --limit 50 （小样本试跑校准）
      nohup python3 -I gen_def_tts.py > def_tts.log 2>&1 &
"""
import asyncio
import sqlite3
import sys
from pathlib import Path

import edge_tts

AUDIO = Path("/root/wordslearn/app/data/audio") / "def"
VOICE = "en-US-AriaNeural"
CONCURRENCY = 15
DUR_MAX = 8.0    # 释义朗读时长上限（秒）；下限 0.5（比单词口径宽，短释义 "not wet" 也合法）
DUR_MIN = 0.5
RATE = "-8%"      # 与例句音一致（释义是短语/短句，非单词）

# edge-tts 默认输出 audio-24khz-48kbitrate-mono-mp3 的帧头指纹（同 gen_tts_audio.py）
BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SR2 = [22050, 24000, 16000]


def _ok(path):
    data = path.read_bytes()
    i = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]) if data[:3] == b"ID3" else 0
    while i < len(data) - 4:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            ver = (data[i + 1] >> 3) & 3
            layer = (data[i + 1] >> 1) & 3
            brx = (data[i + 2] >> 4) & 15
            srx = (data[i + 2] >> 2) & 3
            if ver == 2 and layer == 1 and srx < 3 and BR2[brx]:
                if (BR2[brx], SR2[srx]) != (48, 24000):
                    return False
                fl = int(576 / 8 * BR2[brx] * 1000 / SR2[srx]) + 1
                dur = len(data) / fl * 576 / SR2[srx]
                return DUR_MIN <= dur <= DUR_MAX
            return False
        i += 1
    return False


AUDIO.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect("file:/root/wordslearn/app/data/wordslearn.db?mode=ro", uri=True)
rows = [(r[0], r[1], r[2]) for r in db.execute(
    """SELECT wd.word, s.sense_order, s.definition_en FROM word_senses s
       JOIN words wd ON wd.id=s.word_id WHERE s.definition_en IS NOT NULL
       ORDER BY wd.id, s.sense_order""")]
db.close()
if "--limit" in sys.argv[1:]:
    rows = rows[:int(sys.argv[sys.argv.index("--limit") + 1])]

todo = []
for w, n, text in rows:
    f = AUDIO / f"{w}__{n}.mp3"
    if not f.exists() or not _ok(f):
        todo.append((w, n, text))
print(f"共 {len(rows)} 义，缺/校验不过 {len(todo)} 个音频", flush=True)

sem = asyncio.Semaphore(CONCURRENCY)
failed = []


async def gen(item):
    w, n, text = item
    async with sem:
        f = AUDIO / f"{w}__{n}.mp3"
        for attempt in range(3):
            try:
                c = edge_tts.Communicate(text, VOICE, rate=RATE)
                await c.save(str(f))
                if _ok(f):
                    return True
            except Exception:
                pass
            await asyncio.sleep(2 * attempt + 1)
        failed.append(f"{w}__{n}")
        return False


async def main():
    done = 0
    for chunk_start in range(0, len(todo), 100):
        chunk = todo[chunk_start:chunk_start + 100]
        results = await asyncio.gather(*[gen(it) for it in chunk])
        done += sum(1 for r in results if r)
        print(f"进度 {chunk_start + len(chunk)}/{len(todo)}（累计成功 {done}）", flush=True)
    if failed:
        Path("/root/wordslearn/data/parsed/def_tts_failed.txt").write_text("\n".join(failed))
        print(f"失败 {len(failed)} 条 → def_tts_failed.txt", flush=True)
    print("完成", flush=True)


asyncio.run(main())

"""例句音频批量生成（v4.2）：word_senses.jsonl 例句 → app/data/audio/sent/{word}__{order}.mp3
音色 en-US-AriaNeural（与单词音频统一），15 并发幂等（存在即跳过）。
用法: nohup python3 -I gen_sentence_tts.py > sent_tts.log 2>&1 &（data/parsed 目录，系统 python3）"""
import asyncio
import json
from pathlib import Path

import edge_tts

BASE = Path(__file__).parent
DEST = Path("/root/wordslearn/app/data/audio/sent")
VOICE = "en-US-AriaNeural"
CONCURRENCY = 15

DEST.mkdir(parents=True, exist_ok=True)

todo = []
for line in (BASE / "word_senses.jsonl").read_text(encoding="utf-8").splitlines():
    d = json.loads(line)
    ex = (d.get("example_en") or "").strip()
    if not ex:
        continue
    f = DEST / f"{d['word']}__{d['sense_order']}.mp3"
    if not f.exists():
        todo.append((d["word"], d["sense_order"], ex, f))
print(f"例句音频: 缺 {len(todo)} 条", flush=True)

sem = asyncio.Semaphore(CONCURRENCY)
failed = []


async def gen(word, order, ex, f):
    async with sem:
        for attempt in range(3):
            try:
                c = edge_tts.Communicate(ex, VOICE, rate="-8%")
                await c.save(str(f))
                return True
            except Exception:
                await asyncio.sleep(2 * attempt + 1)
        failed.append(f"{word}__{order}")
        return False


async def main():
    done = 0
    for r in await asyncio.gather(*(gen(*t) for t in todo)):
        done += r
        if done % 200 == 0:
            print(f"  进度 {done}/{len(todo)}", flush=True)
    print(f"完成 {done}/{len(todo)}，失败 {len(failed)}", flush=True)
    if failed:
        (BASE / "sent_tts_failed.txt").write_text("\n".join(failed), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())

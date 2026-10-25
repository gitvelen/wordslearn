"""edge-tts 批量补齐缺失单词音频 → app/data/audio/{word}.mp3
用法: nohup python3 -I gen_tts_audio.py > tts.log 2>&1 &（data/parsed 目录）
15 并发，跳过已有文件，失败词记录到 tts_failed.txt 供重跑。"""
import asyncio
import sqlite3
from pathlib import Path

import edge_tts

AUDIO = Path("/root/wordslearn/app/data/audio")
VOICE = "en-US-AriaNeural"
CONCURRENCY = 15

AUDIO.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect("/root/wordslearn/app/data/wordslearn.db")
words = [r[0] for r in db.execute(
    "SELECT word FROM words WHERE EXISTS (SELECT 1 FROM word_levels l WHERE l.word_id=words.id)")]
db.close()
todo = [w for w in words if not (AUDIO / f"{w}.mp3").exists()]
print(f"共 {len(words)} 词，缺 {len(todo)} 个音频", flush=True)

sem = asyncio.Semaphore(CONCURRENCY)
failed = []


async def gen(w):
    async with sem:
        for attempt in range(3):
            try:
                c = edge_tts.Communicate(w, VOICE, rate="-5%")
                await c.save(str(AUDIO / f"{w}.mp3"))
                return True
            except Exception:
                await asyncio.sleep(2 * attempt + 1)
        failed.append(w)
        return False


async def main():
    done = 0
    for chunk_start in range(0, len(todo), 100):
        chunk = todo[chunk_start:chunk_start + 100]
        results = await asyncio.gather(*[gen(w) for w in chunk])
        done += sum(1 for r in results if r)
        print(f"进度 {chunk_start + len(chunk)}/{len(todo)}（累计成功 {done}）", flush=True)
    if failed:
        Path("/root/wordslearn/data/parsed/tts_failed.txt").write_text("\n".join(failed))
        print(f"失败 {len(failed)} 词 → tts_failed.txt", flush=True)
    print("完成", flush=True)


asyncio.run(main())

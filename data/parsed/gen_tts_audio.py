"""edge-tts 批量补齐/修复单词音频 → app/data/audio/{word}.mp3
用法: nohup python3 -I gen_tts_audio.py > tts.log 2>&1 &（data/parsed 目录）
15 并发。重生成范围 = 缺失文件 + 校验不过的文件（非 edge-tts 格式指纹或时长超界——
混入词典抓取等异构音源时会整批换音色，靠校验兜住），失败词记录到 tts_failed.txt 供重跑。
生成后自检不过同样计失败（防 edge-tts 偶发输出异常）。"""
import asyncio
import sqlite3
from pathlib import Path

import edge_tts

AUDIO = Path("/root/wordslearn/app/data/audio")
VOICE = "en-US-AriaNeural"
CONCURRENCY = 15
DUR_MAX = 3.5   # 单词朗读时长上限（秒），超界=内容不对（读句/读两遍）

# edge-tts 默认输出 audio-24khz-48kbitrate-mono-mp3 的帧头指纹
BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SR2 = [22050, 24000, 16000]


def _ok(path):
    """校验 mp3 是否为 edge-tts 标准指纹且时长合理（首帧定位+总字节数估算）"""
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
                return 0.2 <= dur <= DUR_MAX
            return False
        i += 1
    return False


AUDIO.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect("/root/wordslearn/app/data/wordslearn.db")
words = [r[0] for r in db.execute(
    "SELECT word FROM words WHERE EXISTS (SELECT 1 FROM word_levels l WHERE l.word_id=words.id)")]
db.close()
todo = []
for w in words:
    f = AUDIO / f"{w}.mp3"
    if not f.exists() or not _ok(f):
        todo.append(w)
print(f"共 {len(words)} 词，缺/校验不过 {len(todo)} 个音频", flush=True)

sem = asyncio.Semaphore(CONCURRENCY)
failed = []


async def gen(w):
    async with sem:
        for attempt in range(3):
            try:
                f = AUDIO / f"{w}.mp3"
                c = edge_tts.Communicate(w, VOICE, rate="-5%")
                await c.save(str(f))
                if _ok(f):
                    return True
            except Exception:
                pass
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

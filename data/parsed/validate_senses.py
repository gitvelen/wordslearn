"""精修义项产物校验器：schema/句长/词匹配/义项数的客观检查。
用法: python3 -I validate_senses.py shard_00.jsonl [shard_05.jsonl ...]
（在 data/parsed 目录下运行；输入文件为 gen_out/ 下的 senses_*.jsonl）
退出码 0=全过，1=有硬错误；输出硬错误明细与软警告统计。"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
INPUT_WORDS = {}   # w -> seg
for line in (BASE / "gen_input.jsonl").read_text(encoding="utf-8").splitlines():
    d = json.loads(line)
    INPUT_WORDS[d["w"]] = d["seg"]


def shard_input(fname):
    """分片产物只对其对应输入分片做覆盖检查（senses_shard_05 ← shard_05.jsonl）"""
    import re as _re
    m = _re.search(r"shard_(\d+)", fname)
    if m:
        f = BASE / "gen_out" / f"shard_{m.group(1)}.jsonl"
        words = {}
        for line in f.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            words[d["w"]] = INPUT_WORDS[d["w"]]
        return words
    return INPUT_WORDS

POS_OK = {"n.", "v.", "vt.", "vi.", "adj.", "adv.", "prep.", "conj.", "pron.", "num.", "int."}


def check(fname):
    hard, soft = [], []
    words_map = shard_input(fname)
    seen = {}
    path = BASE / "gen_out" / fname
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception as e:
            hard.append(f"{fname}:{ln} JSON 解析失败: {e}")
            continue
        w = d.get("word", "?")
        if w not in words_map:
            hard.append(f"{fname}:{ln} 词表无此词: {w}")
            continue
        seg = words_map[w]
        seen.setdefault(w, 0)
        seen[w] += 1
        if d.get("sense_order") != seen[w]:
            hard.append(f"{fname}:{ln} {w} sense_order 应为 {seen[w]} 实为 {d.get('sense_order')}")
        if d.get("pos") not in POS_OK:
            hard.append(f"{fname}:{ln} {w} 非法词性: {d.get('pos')}")
        zh = d.get("definition_zh") or ""
        if not zh or len(zh) > 10 or zh.startswith(("n.", "v.", "adj.", "adv.")):
            hard.append(f"{fname}:{ln} {w} 中文释义异常: {zh!r}")
        en = d.get("definition_en") or ""
        if not en:
            soft.append(f"{w} 缺英文释义")
        elif len(en.split()) > 14:
            soft.append(f"{w} 英文释义偏长({len(en.split())}词)")
        ex = d.get("example_en") or ""
        if not ex:
            hard.append(f"{fname}:{ln} {w} 缺例句")
        else:
            n = len(ex.split())
            lim = 10 if seg in ("P13", "P45") else 12
            if n > lim + 1:   # 1 词容差（如缩写计数）
                soft.append(f"{w} 例句超长({n}>{lim}词): {ex}")
            if not (ex[0].isupper() and ex.rstrip().endswith((".", "!", "?"))):
                hard.append(f"{fname}:{ln} {w} 例句不完整: {ex}")
        if not (d.get("example_zh") or "").strip():
            hard.append(f"{fname}:{ln} {w} 缺例句中文")
        if d.get("confidence") not in ("high", "low"):
            hard.append(f"{fname}:{ln} {w} confidence 非法")
    # 覆盖检查
    missing = [w for w in words_map if w not in seen]
    for w in missing[:50]:
        hard.append(f"{fname} 缺失词: {w}")
    if len(missing) > 50:
        hard.append(f"{fname} 缺失词共 {len(missing)} 个（仅列前50）")
    dist = {1: 0, 2: 0, 3: 0}
    for w, n in seen.items():
        dist[min(n, 3)] = dist.get(min(n, 3), 0) + 1
    print(f"== {fname}: 词 {len(seen)}/{len(words_map)} | 义项分布 1义:{dist.get(1,0)} 2义:{dist.get(2,0)} 3义+:{dist.get(3,0)} | "
          f"硬错误 {len(hard)} | 软警告 {len(soft)}")
    low = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
              if line.strip() and json.loads(line).get("confidence") == "low")
    print(f"   confidence=low: {low} 行")
    return hard, soft


if __name__ == "__main__":
    all_hard = []
    for f in sys.argv[1:]:
        h, s = check(f)
        all_hard += h
        for x in s[:15]:
            print("   warn:", x)
    if all_hard:
        print(f"\n硬错误 {len(all_hard)} 条（前 30）:")
        for x in all_hard[:30]:
            print("  HARD:", x)
        sys.exit(1)
    print("\n校验通过")

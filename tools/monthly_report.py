#!/usr/bin/env python3
"""wordslearn 月报（v5 过程埋点分析，离线手动跑，纯标准库）。

用法：python3 tools/monthly_report.py [--user mily] [--month 2026-11] [--db app/data/wordslearn.db]
缺省：上月（当月 1 日跑上月报）。输出纯文本到 stdout。

============================== 归因协议（先读这个） ==============================
1. 系统优先：audio_fallback / 题库哨兵未排除，不对孩子归因（先查工具再查人）。
2. 自参照：所有阈值 = 该用户同题型滚动中位数的倍数。N=1 无人群常模，禁止绝对值判读。
3. 跨通道收敛：任何标签（如"急躁"）需 ≥2 个不同通道（运动/时间/结果/语境）同日同段
   偏离基线——同通道多指标互证（快错率+撞锁+空提交都是运动冲动性）不算收敛。
4. 预测效度：信号预测 D+2/D+12/audit 留存才保留；校准节输出"保留/降级"结论
   （kill criteria：|差| < KILL_MIN_DIFF_PP 或 n < KILL_MIN_N → 无预测力，降级弃用，
   下游功能如 audit 定向抽检永不建立在死信号上）。
5. 时序+剂量：单日偏离=当日状态；跨 2-4 周复现=特质。本脚本只呈现，结论由人下。
6. 人工语境：家长陪读/生病/考试周不可自动测量——月报审阅时人工注记作覆盖层。

阈值常量全部冷启动拍的（2026-11 无历史过程数据），首个整月数据后复核校准。
================================================================================
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# ---- 阈值（冷启动拍的，待校准）----
SLOW_MULT = 2.0          # 慢而对：latency ≥ 同题型中位数 × 2
FAST_MULT = 0.5          # 快：latency ≤ 同题型中位数 × 0.5（快错=急躁候选）
IDLE_MS = 90000          # 亮屏挂机段下限（与前端一致）
SEG_GAP_MIN = 15         # 分段切分 gap（与日报口径一致）
SENTINEL_MIN_N = 3       # 题库哨兵：(词×题型) 最小首答数
SENTINEL_ERR = 0.6       # 题库哨兵：错误率阈值
KILL_MIN_N = 30          # 校准 kill criteria：每组最小样本
KILL_MIN_DIFF_PP = 5.0   # 校准 kill criteria：留存差（百分点）下限
REPLAY_HIGH = 2          # 重听率哨兵：audio_plays ≥ 2 的题占比词数阈值

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def pct(a, b):
    return round(a / b * 100) if b else None


def get_db(db_arg):
    p = Path(db_arg) if db_arg else Path(__file__).resolve().parent.parent / "app" / "data" / "wordslearn.db"
    if not p.exists():
        sys.exit(f"数据库不存在: {p}")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def month_range(month):
    y, m = int(month[:4]), int(month[5:7])
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start.isoformat(), (end - timedelta(days=1)).isoformat()


def fetch_all(db, uid, d0, d1):
    quiz = db.execute("""SELECT q.*, w.word, w.frq, LENGTH(w.word) wlen
                         FROM quiz_log q JOIN words w ON w.id=q.word_id
                         WHERE q.user_id=? AND q.date BETWEEN ? AND ?
                         ORDER BY q.id""", (uid, d0, d1)).fetchall()
    events = db.execute("""SELECT * FROM event_log WHERE user_id=? AND date BETWEEN ? AND ?
                           ORDER BY id""", (uid, d0, d1)).fetchall()
    daily = db.execute("""SELECT * FROM daily_logs WHERE user_id=? AND date BETWEEN ? AND ?
                          ORDER BY date""", (uid, d0, d1)).fetchall()
    states = {r["word_id"]: r for r in db.execute(
        """SELECT word_id, state, stage, wrong_count, first_learned_date FROM user_word_state
           WHERE user_id=?""", (uid,)).fetchall()}
    return quiz, events, daily, states


# ---------------- 1. 周滑动趋势 ----------------
def sec_weekly(quiz, events, daily, out):
    out.append("=" * 72)
    out.append("1. 周滑动趋势（首答口径 retry=0）")
    out.append("=" * 72)
    weeks = {}
    for r in quiz:
        if r["retry"]:
            continue
        d = date.fromisoformat(r["date"])
        monday = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(monday, {"n": 0, "ok": 0, "lat": [], "fastwrong": 0,
                                      "blocked": 0, "replays": 0, "days": set()})
        w["n"] += 1
        w["ok"] += r["correct"]
        w["days"].add(r["date"])
        if r["latency_ms"] is not None:
            w["lat"].append(r["latency_ms"])
        w["blocked"] += r["blocked_taps"] or 0
        w["replays"] += r["audio_plays"] or 0
    # 中位数基准下的快错
    by_type = {}
    for r in quiz:
        if not r["retry"] and r["latency_ms"] is not None and r["quiz_type"]:
            by_type.setdefault(r["quiz_type"], []).append(r["latency_ms"])
    med_by_type = {t: median(v) for t, v in by_type.items()}
    for r in quiz:
        if r["retry"] or r["latency_ms"] is None:
            continue
        m = med_by_type.get(r["quiz_type"])
        if m and r["latency_ms"] <= m * FAST_MULT and not r["correct"]:
            d = date.fromisoformat(r["date"])
            monday = (d - timedelta(days=d.weekday())).isoformat()
            if monday in weeks:
                weeks[monday]["fastwrong"] += 1
    out.append(f"{'周(周一)':<12}{'天数':>4}{'首答':>6}{'正确率':>7}{'时延中位(s)':>12}"
               f"{'快错':>6}{'撞锁':>6}{'点播':>6}")
    for k in sorted(weeks):
        w = weeks[k]
        out.append(f"{k:<12}{len(w['days']):>4}{w['n']:>6}{str(pct(w['ok'], w['n'])) + '%':>7}"
                   f"{(median(w['lat']) / 1000 if w['lat'] else None)!s:>12}"
                   f"{w['fastwrong']:>6}{w['blocked']:>6}{w['replays']:>6}")
    out.append("读法：快错=时延≤0.5×同题型中位数且答错（运动通道急躁候选，需跨通道收敛才可归因）。")
    out.append("")


# ---------------- 2. 信号自校准（控制难度） ----------------
def sec_calibration(quiz, states, out):
    out.append("=" * 72)
    out.append("2. 信号自校准：慢而对 vs 快而对 → 后续留存（裁决脆弱编码 vs 合意困难，不预设方向）")
    out.append("=" * 72)
    # 同题型中位数（首答、答对样本）
    lat_by_type = {}
    for r in quiz:
        if not r["retry"] and r["correct"] and r["latency_ms"] is not None and r["quiz_type"]:
            lat_by_type.setdefault(r["quiz_type"], []).append(r["latency_ms"])
    med = {t: median(v) for t, v in lat_by_type.items()}
    # 词的后续留存 proxy：该词后续（更晚日期）是否存在答错（review/audit 首答）
    later_wrong = {}
    for r in quiz:
        if not r["retry"] and not r["correct"]:
            later_wrong.setdefault(r["word_id"], []).append(r["date"])
    frqs = sorted({r["frq"] for r in quiz if r["frq"]})
    frq_cut1, frq_cut2 = (frqs[len(frqs) // 3], frqs[2 * len(frqs) // 3]) if len(frqs) >= 3 else (None, None)
    # 分层：frq 三分位 × 词长二分
    strat = {}
    for r in quiz:
        if r["retry"] or not r["correct"] or r["latency_ms"] is None:
            continue
        m = med.get(r["quiz_type"])
        if not m:
            continue
        cls = "slow" if r["latency_ms"] >= m * SLOW_MULT else ("fast" if r["latency_ms"] <= m else None)
        if not cls:
            continue
        if frq_cut1 is not None:
            tercile = 0 if r["frq"] is None or r["frq"] <= frq_cut1 else (1 if r["frq"] <= frq_cut2 else 2)
        else:
            tercile = 0
        lstr = "short" if (r["wlen"] or 0) <= 5 else "long"
        later = [d for d in later_wrong.get(r["word_id"], []) if d > r["date"]]
        key = (tercile, lstr, cls)
        s = strat.setdefault(key, {"n": 0, "fail": 0})
        s["n"] += 1
        s["fail"] += 1 if later else 0
    out.append(f"分层：frq三分位(0高频..2低频)×词长(≤5/>5)；慢=≥{SLOW_MULT}×中位，快≤1×中位（中间带剔除）")
    out.append(f"{'层':<18}{'慢而对n':>8}{'后续错':>8}{'快而对n':>8}{'后续错':>8}{'差(pp)':>8}")
    verdict_slow = verdict_fast = None
    strat_keys = sorted({(k[0], k[1]) for k in strat})
    for tercile, lstr in strat_keys:
        s_slow = strat.get((tercile, lstr, "slow"), {"n": 0, "fail": 0})
        s_fast = strat.get((tercile, lstr, "fast"), {"n": 0, "fail": 0})
        ps, pf = pct(s_slow["fail"], s_slow["n"]), pct(s_fast["fail"], s_fast["n"])
        diff = (ps - pf) if (ps is not None and pf is not None) else None
        out.append(f"f{tercile}·{lstr:<7}{s_slow['n']:>8}{s_slow['fail']:>8}"
                   f"{s_fast['n']:>8}{s_fast['fail']:>8}{(diff if diff is not None else '—')!s:>8}")
        verdict_slow, verdict_fast = s_slow, s_fast
    # 汇总判定（跨层合并）
    n_slow = sum(v["n"] for k, v in strat.items() if k[2] == "slow")
    f_slow = sum(v["fail"] for k, v in strat.items() if k[2] == "slow")
    n_fast = sum(v["n"] for k, v in strat.items() if k[2] == "fast")
    f_fast = sum(v["fail"] for k, v in strat.items() if k[2] == "fast")
    ps, pf = pct(f_slow, n_slow), pct(f_fast, n_fast)
    out.append(f"合并：慢而对 {f_slow}/{n_slow} 后续错（{ps}%） vs 快而对 {f_fast}/{n_fast}（{pf}%）")
    if n_slow < KILL_MIN_N or n_fast < KILL_MIN_N or ps is None or pf is None:
        out.append(f"判定：样本不足（kill criteria n≥{KILL_MIN_N}）——无结论，信号保留观测，不进任何下游")
    elif abs(ps - pf) < KILL_MIN_DIFF_PP:
        out.append(f"判定：无预测力（|{ps - pf}|pp < {KILL_MIN_DIFF_PP}pp）——【降级弃用】，audit 联动不做")
    else:
        direction = "慢而对更易错 → 支持『脆弱编码』（费力词值得提前回访）" if ps > pf \
            else "慢而对更抗忘 → 支持『合意困难』（费力=巩固，不需干预）"
        out.append(f"判定：{direction}；两竞争假设中此方向在本月数据占优，跨月复现才可定性")
    out.append("注意：词内设计（同词多次检索的快慢对比）样本积累后再加；本月先看分层结果。")
    out.append("")


# ---------------- 3. fast-wrong 三解分离 ----------------
def sec_fastwrong(quiz, out):
    out.append("=" * 72)
    out.append("3. fast-wrong 三解分离（急躁 / 自信错误 / 误触）")
    out.append("=" * 72)
    lat_by_type = {}
    for r in quiz:
        if not r["retry"] and r["latency_ms"] is not None and r["quiz_type"]:
            lat_by_type.setdefault(r["quiz_type"], []).append(r["latency_ms"])
    med = {t: median(v) for t, v in lat_by_type.items()}
    wrong_by_word_type = {}
    for r in quiz:
        if not r["retry"] and not r["correct"]:
            wrong_by_word_type.setdefault(r["word_id"], set()).add(r["quiz_type"])
    buckets = {"impatience": [], "confident": [], "isolated": []}
    for r in quiz:
        if r["retry"] or r["latency_ms"] is None or r["correct"]:
            continue
        m = med.get(r["quiz_type"])
        if not (m and r["latency_ms"] <= m * FAST_MULT):
            continue
        tag = f"{r['word']}({r['quiz_type']},{r['latency_ms']}ms)"
        if (r["blocked_taps"] or 0) > 0:
            buckets["impatience"].append(tag)      # 运动通道共现：撞锁点击同桶
        elif len(wrong_by_word_type.get(r["word_id"], set())) >= 2:
            buckets["confident"].append(tag)       # 跨题型稳定错：知识问题
        else:
            buckets["isolated"].append(tag)        # 离散单发：误触候选（不归因）
    names = {"impatience": "急躁候选（快错+撞锁共现，运动通道；需第二通道才可归因）",
             "confident": "自信错误（同词跨题型都错——知识问题，优先回炉）",
             "isolated": "离散快错（单发无共现——误触候选，不归因）"}
    for k, v in buckets.items():
        out.append(f"{names[k]}：{len(v)} 个")
        for t in v[:12]:
            out.append(f"  - {t}")
        if len(v) > 12:
            out.append(f"  ...（共 {len(v)}）")
    out.append("")


# ---------------- 4. 题库哨兵 ----------------
def sec_sentinel(quiz, events, out):
    out.append("=" * 72)
    out.append("4. 题库哨兵（先查题目/音频，再归因孩子）")
    out.append("=" * 72)
    cell = {}
    for r in quiz:
        if not r["retry"] and r["quiz_type"] and r["phase"] != "learn":
            k = (r["word_id"], r["word"], r["quiz_type"])
            s = cell.setdefault(k, {"n": 0, "err": 0, "replay": 0})
            s["n"] += 1
            s["err"] += 0 if r["correct"] else 1
            s["replay"] += r["audio_plays"] or 0
    out.append(f"单题型高错（首答错误率≥{SENTINEL_ERR} 且 n≥{SENTINEL_MIN_N}）——疑题目歧义/技能弱：")
    hits = [(k, v) for k, v in cell.items() if v["n"] >= SENTINEL_MIN_N and v["err"] / v["n"] >= SENTINEL_ERR]
    for (wid, word, qtype), v in sorted(hits, key=lambda x: -x[1]["err"] / x[1]["n"]):
        out.append(f"  - {word} × {qtype}：{v['err']}/{v['n']} 错")
    if not hits:
        out.append("  （无）")
    out.append(f"跨题型高错词（≥2 题型错误率≥50%）——词/记忆问题，调度回炉已覆盖，供辅导：")
    by_word = {}
    for (wid, word, qtype), v in cell.items():
        if v["n"] >= 2 and v["err"] / v["n"] >= 0.5:
            by_word.setdefault((wid, word), []).append(f"{qtype} {v['err']}/{v['n']}")
    for (wid, word), lst in sorted(by_word.items()):
        out.append(f"  - {word}：{'，'.join(lst)}")
    if not by_word:
        out.append("  （无）")
    fb = {}
    for e in events:
        if e["type"] == "audio_fallback":
            try:
                w = json.loads(e["detail"] or "{}").get("word", "?")
            except ValueError:
                w = "?"
            fb[w] = fb.get(w, 0) + 1
    out.append(f"音频回退 TTS（audio_fallback，系统健康度）：{sum(fb.values())} 次 / {len(fb)} 词")
    for w, n in sorted(fb.items(), key=lambda x: -x[1])[:10]:
        out.append(f"  - {w}：{n} 次（先查该词音频文件，再谈听辨弱）")
    hi_replay = {}
    for (wid, word, qtype), v in cell.items():
        if v["replay"] >= REPLAY_HIGH * max(1, v["n"]):
            hi_replay[word] = v["replay"]
    if hi_replay:
        out.append(f"重听率异常词（点播≥题数×{REPLAY_HIGH}）：{', '.join(f'{w}({n})' for w, n in hi_replay.items())}")
    out.append("")


# ---------------- 5. 习惯画像 ----------------
def sec_habit(quiz, events, daily, out):
    out.append("=" * 72)
    out.append("5. 习惯画像（时段×表现 / session 内衰减 / 分段）")
    out.append("=" * 72)
    by_hour = {}
    for r in quiz:
        if r["retry"]:
            continue
        h = int(r["ts"][11:13])
        s = by_hour.setdefault(h, {"n": 0, "ok": 0, "lat": []})
        s["n"] += 1
        s["ok"] += r["correct"]
        if r["latency_ms"] is not None:
            s["lat"].append(r["latency_ms"])
    out.append("时段（小时）× 首答正确率 / 时延中位（跨 2-4 周复现才叫习惯，单日只叫当日状态）：")
    for h in sorted(by_hour):
        s = by_hour[h]
        lat = f"  时延{round(median(s['lat']) / 1000, 1)}s" if s["lat"] else ""
        out.append(f"  {h:02d}时：n={s['n']:<4} 正确率{pct(s['ok'], s['n'])}%{lat}")
    # session 内位置衰减：按 (date, 分段序号) 内的作答序
    pos_acc = {"前1/3": [0, 0], "中1/3": [0, 0], "后1/3": [0, 0]}
    by_seg = {}
    for r in quiz:
        if r["retry"]:
            continue
        by_seg.setdefault(r["date"], []).append(r)
    for d, rows in by_seg.items():
        rows.sort(key=lambda x: x["id"])
        segs, cur, prev = [], [], None
        for r in rows:
            t = datetime.fromisoformat(r["ts"])
            if prev and (t - prev).total_seconds() > SEG_GAP_MIN * 60:
                segs.append(cur)
                cur = []
            cur.append(r)
            prev = t
        if cur:
            segs.append(cur)
        for seg in segs:
            n = len(seg)
            for i, r in enumerate(seg):
                part = "前1/3" if i < n / 3 else ("中1/3" if i < 2 * n / 3 else "后1/3")
                pos_acc[part][0] += r["correct"]
                pos_acc[part][1] += 1
    out.append("session 内位置（疲劳候选；与时段交叉判读）：")
    for k, (ok, n) in pos_acc.items():
        out.append(f"  {k}：n={n:<4} 正确率{pct(ok, n)}%")
    # 会话/离席
    hid, vis, idle_ms, resurrect = 0, 0, 0, 0
    for e in events:
        if e["type"] == "app_hidden":
            hid += 1
        elif e["type"] == "app_visible":
            vis += 1
        elif e["type"] == "idle_visible":
            try:
                idle_ms += int((json.loads(e["detail"] or "{}")).get("ms") or 0)
            except ValueError:
                pass
        elif e["type"] == "day_resurrect":
            resurrect += 1
    out.append(f"离席：hidden {hid} 次 / visible {vis} 次；亮屏挂机合计 {round(idle_ms / 60000)} 分钟"
               f"（日报真实时长已按此扣除）")
    out.append(f"陈旧会话复活（day_resurrect）：{resurrect} 次——stale-session 防御在野复发率")
    out.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="mily")
    ap.add_argument("--month", default="")
    ap.add_argument("--db", default="")
    args = ap.parse_args()
    if not args.month:
        t = date.today().replace(day=1) - timedelta(days=1)
        args.month = t.strftime("%Y-%m")
    d0, d1 = month_range(args.month)
    db = get_db(args.db)
    u = db.execute("SELECT id, username, display_name FROM users WHERE username=?", (args.user,)).fetchone()
    if not u:
        sys.exit(f"用户不存在: {args.user}")
    quiz, events, daily, states = fetch_all(db, u["id"], d0, d1)
    out = [f"wordslearn 月报 · {u['display_name'] or u['username']} · {args.month}（{d0} ~ {d1}）",
           f"首答 {sum(1 for r in quiz if not r['retry'])} 条 / 事件 {len(events)} 条 / 学习日 {len(daily)} 天",
           ""]
    if not quiz:
        out.append("本月无作答数据。")
        print("\n".join(out))
        return
    sec_weekly(quiz, events, daily, out)
    sec_calibration(quiz, states, out)
    sec_fastwrong(quiz, out)
    sec_sentinel(quiz, events, out)
    sec_habit(quiz, events, daily, out)
    out.append("=" * 72)
    out.append("呈现规则：以上全部为行为描述与竞争假设，任何『态度』级结论需跨通道收敛+跨月复现；")
    out.append("给家长的引用请逐条核对『行为描述→竞争假设→证伪条件』三段式。")
    print("\n".join(out))


if __name__ == "__main__":
    main()

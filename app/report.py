"""每日学习报告 + 每日积分：聚合当日全部作答（quiz_log）→ HTML 邮件 → SMTP 发送。

触发：前端走完当日队列（或重开时发现队列已完成）→ POST /api/report/daily。
幂等：daily_logs.report_sent=1 防重发；SMTP 未配置/发送失败不置位（下次完成事件重试）。
发件与凭证：app/data/smtp.json（gitignored，pass 为网易/腾讯邮箱「授权码」非登录密码）：
  {"host": "smtp.126.com", "port": 465, "user": "weilingme@126.com", "pass": "授权码",
   "to": "velenh@qq.com"}

每日积分（100 分制，完成事件时一次性结算且首结算为准——刷新重刷不改变当日分）：
  完成当日任务当天即 100 分；闯关测验每答错一题（首答）扣 1 分，扣到 0 为止。
  补测通过不返分（补测本就强制「测到对为止」，无需分数激励）。
  热身/复习不计分（热身明示不算成绩；复习答错已有调度回炉）。
"""
import json
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

from database import get_db, DB_PATH

SMTP_CONF = DB_PATH.parent / "smtp.json"          # app/data/smtp.json
SMTP_SKIP_USERS = ("test", "jrsmoke")             # 测试账号：照常结算积分，但不发真邮件（防 e2e/冒烟骚扰家长邮箱）
SMTP_SKIP_PREFIX = "e2e_"                        # e2e 临时用户前缀

GATE_NAMES_STD = {1: "认词", 2: "听音", 3: "回想", 4: "听写", 5: "用词"}
GATE_NAMES_JR = {1: "认词", 2: "听音", 3: "选词"}
TYPE_LABEL = {"word_zh": "看词选义", "listen_zh": "听音选义", "zh_word": "中文选词",
              "cloze": "句子挖空", "spell": "听写拼写", "emoji": "听音选词", "sense": "选义项"}

SCORE_FULL = 100       # 完成当日任务当天即满分
SCORE_WRONG_COST = 1   # 闯关首答每错一题扣分


def _today() -> str:
    return date.today().isoformat()


# ---------- 每日积分 ----------
def compute_daily_score(db, user_id, day):
    """当日积分：完成即 100，闯关首答每错一题 -1（0 分保底）。补测不返分。"""
    rows = db.execute("""SELECT correct, retry FROM quiz_log
                         WHERE user_id=? AND date=? AND phase='gate'""", (user_id, day)).fetchall()
    errors = sum(1 for r in rows if not r["retry"] and not r["correct"])
    total = max(0, SCORE_FULL - SCORE_WRONG_COST * errors)
    return {"total": total, "errors": errors}


def settle_daily_score(user_id, day=None):
    """完成事件结算：首结算为准（score IS NULL 才写），返回结算后分值。"""
    day = day or _today()
    db = get_db()
    try:
        row = db.execute("SELECT score FROM daily_logs WHERE user_id=? AND date=?",
                         (user_id, day)).fetchone()
        if row and row["score"] is not None:
            return row["score"]
        detail = compute_daily_score(db, user_id, day)
        db.execute("UPDATE daily_logs SET score=? WHERE user_id=? AND date=?",
                   (detail["total"], user_id, day))
        db.commit()
        return detail["total"]
    finally:
        db.close()


def record_quiz(user_id, entries):
    """前端埋点写入（热身/关卡测验——不进调度、无状态可查的行为数据）。"""
    db = get_db()
    try:
        t = _today()
        for e in entries:
            phase = e.get("phase") or ""
            if phase not in ("warmup", "gate", "review", "learn"):
                continue
            db.execute("""INSERT INTO quiz_log(user_id, date, phase, level, word_id,
                                                 quiz_type, correct, retry)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (user_id, t, phase, e.get("level"), e.get("word_id"),
                        e.get("quiz_type") or "", 1 if e.get("correct") else 0,
                        1 if e.get("retry") else 0))
        db.commit()
    finally:
        db.close()


def _word_defs(db, word_ids):
    """词 → 核心中文释义（sense_order=1 优先，回退 words.definition_zh）。"""
    out = {}
    for wid in word_ids:
        r = db.execute("""SELECT definition_zh FROM word_senses WHERE word_id=? ORDER BY sense_order LIMIT 1""",
                       (wid,)).fetchone()
        if not r:
            r = db.execute("SELECT definition_zh FROM words WHERE id=?", (wid,)).fetchone()
        out[wid] = (r["definition_zh"] or "").split(" | ")[0] if r else ""
    return out


def _round_rows(rows):
    """首答统计：补测(retry=1)不计入首答正确率，单列补测次数。"""
    first = [r for r in rows if not r["retry"]]
    retries = sum(1 for r in rows if r["retry"])
    n = len(first)
    ok = sum(1 for r in first if r["correct"])
    wrong_words = sorted({r["word"] for r in first if not r["correct"]})
    return {"n": n, "ok": ok, "acc": round(ok / n * 100) if n else None,
            "retries": retries, "wrong_words": wrong_words}


def build_daily_report(user_id, day=None):
    """聚合当日数据 → 报告 dict（渲染与发送解耦）。"""
    db = get_db()
    try:
        day = day or _today()
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        junior = bool(user and user["mode"] == "junior")
        uname = (user["display_name"] or user["username"]) if user else "?"
        log = db.execute("SELECT * FROM daily_logs WHERE user_id=? AND date=?", (user_id, day)).fetchone()
        qrows = db.execute("""SELECT q.*, w.word FROM quiz_log q JOIN words w ON w.id=q.word_id
                              WHERE q.user_id=? AND q.date=? ORDER BY q.id""", (user_id, day)).fetchall()
        # 今日新词（附释义，供家长辅导）
        new_words = db.execute("""SELECT w.id, w.word FROM user_word_state s JOIN words w ON w.id=s.word_id
                                  WHERE s.user_id=? AND s.first_learned_date=? ORDER BY w.id""",
                               (user_id, day)).fetchall()
        # 今日复习词汇（复习轮出现过的词 + 对错结果）
        review_words = []
        seen = {}
        for r in qrows:
            if r["phase"] != "review":
                continue
            e = seen.setdefault(r["word_id"], {"word": r["word"], "ok": True})
            if not r["correct"]:
                e["ok"] = False
        defs = _word_defs(db, [r["id"] for r in new_words]
                          + list({r["word_id"] for r in qrows if not r["correct"]})
                          + list(seen))
        for wid, e in seen.items():
            e["def"] = defs.get(wid, "")
            review_words.append(e)
        review_words.sort(key=lambda x: x["ok"])   # 有错的排前（家长重点）
        # 分轮统计
        rounds = []
        for phase, label in (("warmup", "晨间热身"), ("review", "复习轮"), ("audit", "掌握抽检")):
            rows = [r for r in qrows if r["phase"] == phase]
            if rows:
                rounds.append({"label": label, **_round_rows(rows)})
        gate_names = GATE_NAMES_JR if junior else GATE_NAMES_STD
        for lv in sorted({r["level"] for r in qrows if r["phase"] == "gate"}):
            rows = [r for r in qrows if r["phase"] == "gate" and r["level"] == lv]
            rounds.append({"label": f"第{lv}关 · {gate_names.get(lv, '?')}", **_round_rows(rows)})
        # 薄弱环节：题型维度（哪类题错最多）+ 错词清单（错在哪轮）
        type_stat = {}
        for r in qrows:
            if r["retry"]:
                continue
            t = r["quiz_type"] or "card"
            s = type_stat.setdefault(t, {"n": 0, "ok": 0})
            s["n"] += 1
            s["ok"] += 1 if r["correct"] else 0
        weak_types = sorted(((t, s) for t, s in type_stat.items() if s["n"] >= 2 and s["ok"] / s["n"] < 0.8),
                            key=lambda x: x[1]["ok"] / x[1]["n"])
        wrong_map = {}
        for r in qrows:
            if r["correct"]:
                continue
            lbl = {"warmup": "热身", "review": "复习", "learn": "新学", "audit": "抽检"}.get(r["phase"]) \
                or f"第{r['level']}关"
            wrong_map.setdefault(r["word"], {"def": defs.get(r["word_id"], ""), "rounds": []})["rounds"].append(lbl)
        # 会话时长（首末作答时间差，含卡片提交；跨长时间挂起会高估，仅供参考）
        minutes = None
        if qrows:
            t0, t1 = qrows[0]["ts"], qrows[-1]["ts"]
            try:
                from datetime import datetime
                minutes = max(1, round((datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 60))
            except ValueError:
                pass
        # 近 7 日趋势
        trend = []
        for i in range(6, -1, -1):
            d = (date.fromisoformat(day) - timedelta(days=i)).isoformat()
            dl = db.execute("SELECT new_done, review_done FROM daily_logs WHERE user_id=? AND date=?",
                            (user_id, d)).fetchone()
            q = db.execute("""SELECT COUNT(*) n, SUM(correct) ok FROM quiz_log
                              WHERE user_id=? AND date=? AND retry=0""", (user_id, d)).fetchone()
            acc = round(q["ok"] / q["n"] * 100) if q and q["n"] and q["ok"] is not None else None
            trend.append({"date": d, "new": dl["new_done"] if dl else 0,
                          "review": dl["review_done"] if dl else 0, "acc": acc})
        # 连续天数（与 progress 口径一致）
        streak = 0
        d = date.fromisoformat(day)
        while True:
            dl = db.execute("SELECT new_done, review_done FROM daily_logs WHERE user_id=? AND date=?",
                            (user_id, d.isoformat())).fetchone()
            if dl and (dl["new_done"] > 0 or (dl["review_done"] or 0) > 0):
                streak += 1
                d -= timedelta(days=1)
            else:
                break
        tips = _build_tips(rounds, wrong_map, weak_types, junior)
        # 积分：已结算用结算值；未结算（preview 场景）现场估算
        score = log["score"] if log and log["score"] is not None else None
        score_detail = compute_daily_score(db, user_id, day)
        return {"user": uname, "date": day, "streak": streak, "junior": junior,
                "new_done": log["new_done"] if log else 0,
                "review_done": (log["review_done"] if log else 0) or 0,
                "minutes": minutes, "rounds": rounds,
                "score": score if score is not None else score_detail["total"],
                "score_detail": score_detail,
                "new_words": [{"word": r["word"], "def": defs.get(r["id"], "")} for r in new_words],
                "review_words": review_words,
                "weak_types": [{"label": TYPE_LABEL.get(t, t), "n": s["n"], "ok": s["ok"]} for t, s in weak_types],
                "wrong_words": [{"word": w, "def": v["def"], "rounds": v["rounds"]}
                                for w, v in sorted(wrong_map.items())],
                "trend": trend, "tips": tips}
    finally:
        db.close()


def _build_tips(rounds, wrong_map, weak_types, junior):
    """规则化建议：给家长的可执行动作，不给空洞表扬。"""
    tips = []
    quiz_rounds = [r for r in rounds if r["label"].startswith("第")]
    if quiz_rounds:
        worst = min(quiz_rounds, key=lambda r: (r["acc"] if r["acc"] is not None else 100))
        if worst["acc"] is not None and worst["acc"] < 70:
            gate_name = worst["label"].split("· ")[-1]
            tips.append(f"「{gate_name}」首答正确率 {worst['acc']}%，"
                        f"建议今晚针对错词做 3 分钟口头练习（{'跟读' if junior else '拼读'}）")
    if wrong_map:
        tips.append(f"今日错词 {len(wrong_map)} 个，晚间睡前过一遍效果最好："
                    + "、".join(list(wrong_map)[:8]))
    if weak_types:
        w = weak_types[0]
        tips.append(f"「{TYPE_LABEL.get(w[0], w[0])}」类题型近期较弱（{w[1]['ok']}/{w[1]['n']}），"
                    "可在日常对话/阅读中多创造这类接触")
    if not tips:
        tips.append("全部首轮通过，状态很好——今天可以只复习不加压，保持节奏最重要")
    return tips


def render_html(rep):
    """HTML 邮件（内联样式 + table 布局，QQ 邮箱客户端兼容）。"""
    def acc_txt(a):
        return f"{a}%" if a is not None else "—"

    rows_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'>{r['label']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center'>{r['n']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee;text-align:center;font-weight:600;"
        f"color:{'#34b37a' if (r['acc'] is not None and r['acc'] >= 80) else '#f5a623' if (r['acc'] is not None and r['acc'] >= 60) else '#e05252'}'>"
        f"{acc_txt(r['acc'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{r['retries']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{('、'.join(r['wrong_words'][:6]) or '—')}</td></tr>"
        for r in rep["rounds"])
    new_html = "、".join(f"<b>{w['word']}</b>({w['def']})" for w in rep["new_words"]) or "—"
    rvw_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'><b>{w['word']}</b></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{w['def']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{'✓ 通过' if w['ok'] else '<b style=\"color:#e05252\">✗ 有错</b>'}</td></tr>"
        for w in rep["review_words"]) or "<tr><td colspan='3' style='padding:10px;color:#999'>今天没有复习任务</td></tr>"
    wrong_html = "".join(
        f"<tr><td style='padding:6px 10px;border-bottom:1px solid #eee'><b>{w['word']}</b></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{w['def']}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{'、'.join(w['rounds'])}</td></tr>"
        for w in rep["wrong_words"]) or "<tr><td colspan='3' style='padding:10px;color:#999'>今天没有错词 🎉</td></tr>"
    trend_html = "".join(
        f"<td style='padding:6px 4px;text-align:center'>{t['date'][5:]}<br>"
        f"<b>{t['new']}</b>词<br>{acc_txt(t['acc'])}</td>" for t in rep["trend"])
    tips_html = "".join(f"<li style='margin:4px 0'>{t}</li>" for t in rep["tips"])
    total_n = sum(r["n"] for r in rep["rounds"])
    total_ok = sum(r["ok"] for r in rep["rounds"])
    return f"""<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;max-width:640px;margin:0 auto;color:#2a2f3a">
<div style="background:linear-gradient(135deg,#5b7cfa,#7c9bff);border-radius:12px;padding:18px 20px;color:#fff">
<div style="font-size:18px;font-weight:700">🪐 单词星球 · 每日学习报告</div>
<div style="font-size:14px;margin-top:6px;opacity:.9">{rep['user']} · {rep['date']} · 连续学习 {rep['streak']} 天 🔥</div>
<div style="font-size:14px;margin-top:6px">今日得分 <b style="font-size:20px">{rep['score']}</b> / 100 ⭐</div></div>
<table style="width:100%;border-collapse:collapse;margin:14px 0;background:#fff;border-radius:10px">
<tr><td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{rep['new_done']}</b><br><span style="font-size:12px;color:#888">新学单词</span></td>
<td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{rep['review_done']}</b><br><span style="font-size:12px;color:#888">复习通过</span></td>
<td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{total_ok}/{total_n}</b><br><span style="font-size:12px;color:#888">总答题</span></td>
<td style="padding:12px 10px;text-align:center"><b style="font-size:20px">{rep['minutes'] or '—'}</b><br><span style="font-size:12px;color:#888">用时(分)</span></td></tr></table>
<h3 style="font-size:15px;margin:16px 0 6px">📚 今日新词（{len(rep['new_words'])} 个）</h3>
<div style="font-size:13px;line-height:1.9;background:#fff;border-radius:10px;padding:10px 14px">{new_html}</div>
<h3 style="font-size:15px;margin:16px 0 6px">🔁 今日复习（{len(rep['review_words'])} 个）</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:13px">
<tr style="color:#888;font-size:12px"><th style="padding:8px 10px;text-align:left">单词</th><th style="padding:8px 10px;text-align:left">释义</th><th style="padding:8px 10px;text-align:left">结果</th></tr>{rvw_html}</table>
<h3 style="font-size:15px;margin:16px 0 6px">📊 各轮表现（首答正确率）</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:13px">
<tr style="color:#888;font-size:12px"><th style="padding:8px 10px;text-align:left">轮次</th><th>题数</th><th>正确率</th><th>补测</th><th style="text-align:left">错词</th></tr>{rows_html}</table>
{f"<div style='font-size:12px;color:#888;margin-top:4px'>🎯 掌握抽检 = 之前已掌握的词随机回访，正确率即<b>旧词留存率</b>；答错的词已自动回炉重学</div>" if any(r['label']=='掌握抽检' for r in rep['rounds']) else ''}
<h3 style="font-size:15px;margin:16px 0 6px">🔍 错词清单（今晚重点）</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:13px">
<tr style="color:#888;font-size:12px"><th style="padding:8px 10px;text-align:left">单词</th><th style="text-align:left">释义</th><th style="text-align:left">错在</th></tr>{wrong_html}</table>
{f"<h3 style='font-size:15px;margin:16px 0 6px'>⚠️ 薄弱题型</h3><div style='font-size:13px;background:#fff;border-radius:10px;padding:10px 14px'>" + '、'.join(f"{w['label']}（{w['ok']}/{w['n']}）" for w in rep['weak_types']) + '</div>' if rep['weak_types'] else ''}
<h3 style="font-size:15px;margin:16px 0 6px">📈 近 7 日</h3>
<table style="width:100%;border-collapse:collapse;background:#fff;border-radius:10px;font-size:12px"><tr>{trend_html}</tr></table>
<h3 style="font-size:15px;margin:16px 0 6px">💡 给家长的建议</h3>
<ul style="font-size:13px;line-height:1.8;background:#f4f6fb;border-radius:10px;padding:12px 14px 12px 30px;margin:0">{tips_html}</ul>
<div style="text-align:center;color:#aaa;font-size:11px;margin-top:16px">单词星球 WordsLearn · 每日完成学习后自动发送</div></div>"""


def _smtp_conf():
    if not SMTP_CONF.exists():
        return None
    try:
        cfg = json.loads(SMTP_CONF.read_text())
    except (ValueError, OSError):
        return None
    # pass 为空视为未配置（占位文件），避免每次完成都白跑一次 SMTP 登录
    if not (cfg.get("host") and cfg.get("user") and cfg.get("pass")):
        return None
    return cfg


def send_daily_report(user_id):
    """完成事件入口：先结算积分（首结算为准），再发邮件。
    无学习不送；已送幂等跳过；未配置/失败不置 report_sent（可重试，积分已结算不重算）。"""
    day = _today()
    db = get_db()
    try:
        log = db.execute("""SELECT l.*, u.username FROM daily_logs l JOIN users u ON u.id=l.user_id
                            WHERE l.user_id=? AND l.date=?""", (user_id, day)).fetchone()
        if not log or (log["new_done"] == 0 and not (log["review_done"] or 0)):
            return {"sent": False, "reason": "当日无学习记录，不发送"}
        uname = log["username"]
    finally:
        db.close()
    score = settle_daily_score(user_id, day)
    db = get_db()
    try:
        sent_flag = db.execute("SELECT report_sent FROM daily_logs WHERE user_id=? AND date=?",
                               (user_id, day)).fetchone()["report_sent"]
    finally:
        db.close()
    rep = build_daily_report(user_id, day)
    summary = {"new_done": rep["new_done"], "wrong": len(rep["wrong_words"]),
               "streak": rep["streak"], "score": score,
               "rounds": [(r["label"], r["acc"]) for r in rep["rounds"]]}
    if sent_flag:
        return {"sent": False, "reason": "今日报告已发送过", "summary": summary}
    if uname in SMTP_SKIP_USERS or uname.startswith(SMTP_SKIP_PREFIX):
        return {"sent": False, "reason": "测试账号不发信", "summary": summary}
    cfg = _smtp_conf()
    if not cfg:
        return {"sent": False, "reason": "SMTP 未配置（app/data/smtp.json，pass=邮箱授权码），报告已生成未发送",
                "summary": summary}
    subject = (f"【单词星球】{rep['user']} {day[5:]} 学习日报 · {score}分 · "
               f"新词{rep['new_done']} 错词{len(rep['wrong_words'])} 🔥{rep['streak']}天")
    html = render_html(rep)
    try:
        msg = MIMEText(html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = f"单词星球 <{cfg['user']}>"
        msg["To"] = cfg.get("to") or cfg["user"]
        with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), timeout=15) as s:
            s.login(cfg["user"], cfg["pass"])
            s.sendmail(cfg["user"], [msg["To"]], msg.as_string())
    except Exception as e:
        return {"sent": False, "reason": f"发送失败: {e}", "summary": summary}
    db = get_db()
    try:
        db.execute("UPDATE daily_logs SET report_sent=1 WHERE user_id=? AND date=?", (user_id, day))
        db.commit()
    finally:
        db.close()
    return {"sent": True, "reason": "ok", "summary": summary}

"""wordslearn FastAPI 入口"""
import secrets
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

import database
import auth
import scheduler
import report

BASE = Path(__file__).parent
STATIC = BASE / "static"
AUDIO = BASE / "data" / "audio"

app = FastAPI(title="wordslearn", docs_url=None, redoc_url=None)

# session secret 持久化：重启不掉登录
_secret_file = BASE / "data" / "secret.key"
_secret_file.parent.mkdir(parents=True, exist_ok=True)
if _secret_file.exists():
    _secret = _secret_file.read_text().strip()
else:
    _secret = secrets.token_urlsafe(32)
    _secret_file.write_text(_secret)
app.add_middleware(SessionMiddleware, secret_key=_secret, max_age=30 * 86400)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """HTML 不允许启发式缓存（ Safari 会按 Last-Modified 缓存数小时，版本更新不生效）；
    带 ?v= 的 JS/CSS 仍可长缓存。"""
    resp = await call_next(request)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def _startup():
    database.init_db()
    AUDIO.mkdir(parents=True, exist_ok=True)


class LoginIn(BaseModel):
    username: str
    password: str

class GradeIn(BaseModel):
    grade: str

class PlanIn(BaseModel):
    target_words: int
    days: int

class AnswerIn(BaseModel):
    word_id: int
    phase: str      # 'learn' | 'review'
    correct: bool
    quiz_type: str = ""   # 学习日报埋点用（spell/zh_word/...）

class ConfirmSubmitIn(BaseModel):
    known_ids: list   # 速选「认识」的词 ID

class ConfirmVerifyIn(BaseModel):
    word_id: int
    correct: bool


def _client_ip(request):
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    user = auth.login(body.username.strip(), body.password, _client_ip(request))
    request.session["uid"] = user["id"]
    return {"ok": True, "username": user["username"], "mode": user["mode"]}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


VALID_GRADES = {f"{g}{t}" for g in range(1, 10) for t in ("A", "B")} | {"adult"}


@app.get("/api/me")
def me(request: Request):
    user = auth.current_user(request)
    progress = scheduler.get_progress(user["id"])
    db = database.get_db()
    try:
        need_confirm = scheduler.need_confirm(db, user)
    finally:
        db.close()
    confirm = scheduler.confirm_status(user["id"])
    return {"user": dict(user), "progress": progress, "confirm": confirm,
            "need_confirm": need_confirm,
            "need_basic_quiz": need_confirm and not user["basic_quiz_done"]}


@app.post("/api/profile/grade")
def set_grade(body: GradeIn, request: Request):
    user = auth.current_user(request)
    if body.grade not in VALID_GRADES:
        raise HTTPException(400, "无效年级")
    mode = "junior" if body.grade in ("1A", "1B", "2A", "2B") else "standard"
    db = database.get_db()
    try:
        db.execute("UPDATE users SET grade=?, mode=? WHERE id=?", (body.grade, mode, user["id"]))
        db.commit()
    finally:
        db.close()
    return {"ok": True, "grade": body.grade, "mode": mode}


@app.post("/api/profile/skip_screening")
def skip_screening(request: Request):
    """v4.1：跳过表格确认 = 全部当生词直接建计划。"""
    user = auth.current_user(request)
    db = database.get_db()
    try:
        db.execute("UPDATE users SET screening_skipped=1, confirm_done=1 WHERE id=?", (user["id"],))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.post("/api/plan")
def create_plan(body: PlanIn, request: Request):
    user = auth.current_user(request)
    if not (10 <= body.target_words <= 3000 and 14 <= body.days <= 365):
        raise HTTPException(400, "参数范围: 词数10-3000, 天数14-365（含12天复习收尾期）")
    plan, err = scheduler.create_plan(user["id"], body.target_words, body.days)
    if err:
        raise HTTPException(400, err)
    return plan


@app.get("/api/today")
def today(request: Request):
    user = auth.current_user(request)
    return scheduler.get_today_queue(user["id"])


@app.post("/api/answer")
def answer(body: AnswerIn, request: Request):
    user = auth.current_user(request)
    if body.phase not in ("learn", "review"):
        raise HTTPException(400, "phase 必须是 learn 或 review")
    try:
        scheduler.submit_answer(user["id"], body.word_id, body.phase, body.correct, body.quiz_type)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class QuizLogIn(BaseModel):
    entries: list   # [{word_id, phase, level?, quiz_type?, correct, retry?}]


@app.post("/api/quiz_log")
def quiz_log(body: QuizLogIn, request: Request):
    """热身/关卡测验埋点（不进调度，仅入 quiz_log 供学习日报聚合）。"""
    user = auth.current_user(request)
    report.record_quiz(user["id"], body.entries)
    return {"ok": True}


@app.post("/api/report/daily")
def report_daily(request: Request):
    """当日学习完成事件：结算积分 + 生成并邮件发送学习日报（幂等，未完成不发送）。"""
    user = auth.current_user(request)
    return report.send_daily_report(user["id"])


@app.get("/api/score")
def score(request: Request):
    """今日积分与累计（完成事件结算后才有值；明细实时算供完成屏展示）。"""
    user = auth.current_user(request)
    uid = user["id"]
    db = database.get_db()
    try:
        today = db.execute("SELECT score FROM daily_logs WHERE user_id=? AND date=?",
                           (uid, report._today())).fetchone()
        total = db.execute("SELECT COALESCE(SUM(score),0) t FROM daily_logs WHERE user_id=? AND score IS NOT NULL",
                           (uid,)).fetchone()["t"]
        detail = report.compute_daily_score(db, uid, report._today())
        return {"today": today["score"] if today else None, "total": total, "detail": detail}
    finally:
        db.close()


@app.get("/api/report/preview")
def report_preview(request: Request, date: str = "", user: str = ""):
    """日报预览（家长自查/联调）：默认本人今日。"""
    u = auth.current_user(request)
    uid, day = u["id"], (date or None)
    if user and u["username"] == "velen":      # 家长账号可查孩子
        r = database.get_db().execute("SELECT id FROM users WHERE username=?", (user,)).fetchone()
        if r:
            uid = r["id"]
    rep = report.build_daily_report(uid, day)
    return {"report": rep, "html": report.render_html(rep)}


@app.get("/api/word_detail")
def word_detail(request: Request, id: int):
    """完整词条（全部义项+例句+词形变化）：复习答错看卡与词典弹层用。"""
    db = database.get_db()
    try:
        w = db.execute("""SELECT id, word, phonetic_uk, phonetic_us, definition_zh,
                                 mp3, emoji, forms FROM words WHERE id=?""", (id,)).fetchone()
        if not w:
            raise HTTPException(404, "词条不存在")
        senses = db.execute("""SELECT sense_order, pos, definition_zh, definition_en,
                                      example_en, example_zh FROM word_senses
                               WHERE word_id=? ORDER BY sense_order""", (id,)).fetchall()
        d = dict(w)
        d["senses"] = [dict(s) for s in senses]
        return d
    finally:
        db.close()


@app.get("/api/calendar")
def calendar(request: Request, month: str):
    user = auth.current_user(request)
    if not (len(month) == 7 and month[4] == "-"):
        raise HTTPException(400, "month 格式: YYYY-MM")
    return scheduler.get_calendar(user["id"], month)


@app.get("/api/progress")
def progress(request: Request):
    user = auth.current_user(request)
    return scheduler.get_progress(user["id"])


# ---------- 表格确认（v4.1 摸底）----------
@app.get("/api/confirm/basic_quiz")
def confirm_basic_quiz(request: Request):
    user = auth.current_user(request)
    return {"quiz": scheduler.confirm_basic_quiz(user["id"])}


class BasicResultIn(BaseModel):
    answers: dict      # {word_id(str): bool}


@app.post("/api/confirm/basic_result")
def confirm_basic_result(body: BasicResultIn, request: Request):
    user = auth.current_user(request)
    try:
        out = scheduler.confirm_basic_result(user["id"], body.answers)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return out


@app.get("/api/confirm/pool")
def confirm_pool(request: Request, size: int = 1170):
    user = auth.current_user(request)
    if not (100 <= size <= 2600):
        raise HTTPException(400, "size 100-2600")
    return {"words": scheduler.confirm_pool(user["id"], size)}


@app.post("/api/confirm/submit")
def confirm_submit(body: ConfirmSubmitIn, request: Request):
    user = auth.current_user(request)
    scheduler.confirm_submit(user["id"], body.known_ids)
    return {"ok": True}


@app.get("/api/confirm/quiz")
def confirm_quiz(request: Request, size: int = 10):
    user = auth.current_user(request)
    return {"quiz": scheduler.confirm_quiz(user["id"], size)}


@app.post("/api/confirm/verify")
def confirm_verify(body: ConfirmVerifyIn, request: Request):
    user = auth.current_user(request)
    scheduler.confirm_verify(user["id"], body.word_id, body.correct)
    return {"ok": True}


@app.get("/api/confirm/status")
def confirm_status(request: Request):
    user = auth.current_user(request)
    return scheduler.confirm_status(user["id"])


@app.post("/api/confirm/finish")
def confirm_finish(request: Request):
    user = auth.current_user(request)
    scheduler.confirm_finish(user["id"])
    return {"ok": True}


# ---------- 静态资源（挂载放最后，避免吞掉 /api 路由）----------
AUDIO.mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=AUDIO), name="audio")
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")

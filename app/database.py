"""SQLite 数据模型与初始化 — wordslearn
表结构按设计定稿实现（详见交接简报），schema_migrations 用于版本追踪。
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "wordslearn.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
  id            INTEGER PRIMARY KEY,
  word          TEXT NOT NULL UNIQUE,
  phonetic_uk   TEXT,
  phonetic_us   TEXT,
  definition_zh TEXT,
  definition_en TEXT,
  example_en    TEXT,
  mp3           TEXT,
  frq           INTEGER,             -- COCA 词频排名（ECDICT），越小越高频，池排序用
  emoji         TEXT,
  forms         TEXT                 -- 词形变化 JSON（ECDICT exchange）：{"p":"过去式",...}
);

CREATE TABLE IF NOT EXISTS word_levels (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL REFERENCES words(id),
  textbook   TEXT NOT NULL,           -- 'oxford_sh' | 'zhongkao' | 'nce'
  grade_code TEXT NOT NULL,           -- '1A'..'5B' | '6A'..'9B' | 'JUNIOR' | 'NCE1'
  unit       TEXT,
  UNIQUE(word_id, textbook, grade_code)
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name  TEXT,
  mode          TEXT NOT NULL DEFAULT 'standard',   -- 'standard' | 'junior'
  grade         TEXT,                               -- '1A'..'5B','6A'..'9B' | 'adult'
  screening_skipped INTEGER DEFAULT 0,              -- legacy（v4.1 起停用）
  spot_done     INTEGER DEFAULT 0,                  -- legacy（v4.1 起停用）
  confirm_done  INTEGER DEFAULT 0,                  -- 1=表格确认流程已完成（v4.1）
  basic_quiz_done INTEGER DEFAULT 0,                -- 1=基础词快测已完成（v4.1.1）
  created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS study_plans (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  status       TEXT NOT NULL DEFAULT 'active',      -- active|completed|abandoned
  target_words INTEGER NOT NULL,
  start_date   TEXT NOT NULL,
  end_date     TEXT NOT NULL,
  daily_new    INTEGER NOT NULL,
  created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS plan_words (
  plan_id INTEGER NOT NULL REFERENCES study_plans(id),
  word_id INTEGER NOT NULL REFERENCES words(id),
  seq     INTEGER NOT NULL,
  UNIQUE(plan_id, word_id)
);

CREATE TABLE IF NOT EXISTS user_word_state (
  user_id     INTEGER NOT NULL REFERENCES users(id),
  word_id     INTEGER NOT NULL REFERENCES words(id),
  state       TEXT NOT NULL DEFAULT 'new',   -- new|verify|learning|mastered|skipped
  stage       INTEGER NOT NULL DEFAULT 0,    -- 0首学→1(D+2)→2(D+5)→3(D+12)→mastered
  first_learned_date TEXT,
  next_review_date   TEXT,
  wrong_count INTEGER DEFAULT 0,
  verify_count INTEGER DEFAULT 0,            -- 摸底连续验证通过计数（2次才剔除）
  UNIQUE(user_id, word_id)
);

CREATE TABLE IF NOT EXISTS daily_logs (
  user_id     INTEGER NOT NULL,
  date        TEXT NOT NULL,
  new_done    INTEGER DEFAULT 0,
  review_done INTEGER DEFAULT 0,
  minutes     INTEGER DEFAULT 0,
  report_sent INTEGER DEFAULT 0,                 -- 1=当日学习日报已邮件发送（防重发）
  score       INTEGER,                           -- 当日积分（完成事件时一次性结算，NULL=未结算）
  UNIQUE(user_id, date)
);

-- v2.2 学习行为埋点：全部作答（含不进调度的热身/关卡测验），供每日学习报告聚合
CREATE TABLE IF NOT EXISTS quiz_log (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  date       TEXT NOT NULL,
  phase      TEXT NOT NULL,                      -- learn|review|warmup|gate
  level      INTEGER,                            -- gate 关卡号 1-5（其余 NULL）
  word_id    INTEGER NOT NULL REFERENCES words(id),
  quiz_type  TEXT DEFAULT '',                    -- word_zh/listen_zh/zh_word/cloze/spell/emoji/card
  correct    INTEGER NOT NULL,
  retry      INTEGER DEFAULT 0,                  -- gate 关末补测标记
  ts         TEXT DEFAULT (datetime('now'))      -- 会话时长估算用
);
CREATE INDEX IF NOT EXISTS idx_quizlog_user_date ON quiz_log(user_id, date);

CREATE TABLE IF NOT EXISTS screening_batches (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  grade_code TEXT NOT NULL,             -- legacy（v4.1 起停用）
  total      INTEGER,
  done       INTEGER DEFAULT 0,
  status     TEXT DEFAULT 'in_progress'
);

-- v4.1 词义项精修表：每词 1-6+ 义项（v4.2 词典式完备），每义项配例句（双语）与受限英文释义
CREATE TABLE IF NOT EXISTS word_senses (
  id            INTEGER PRIMARY KEY,
  word_id       INTEGER NOT NULL REFERENCES words(id),
  sense_order   INTEGER NOT NULL DEFAULT 1,   -- 1=核心义（junior 模式只展示此义）
  pos           TEXT,                         -- 'n.' 'v.' 等
  definition_zh TEXT,
  definition_en TEXT,
  example_en    TEXT,
  example_zh    TEXT,
  confidence    TEXT DEFAULT 'high',          -- 'high'|'low'（低置信词校对台优先人工复核）
  UNIQUE(word_id, sense_order)
);
CREATE INDEX IF NOT EXISTS idx_senses_word ON word_senses(word_id);

-- v4.1 题库：预生成题目（含干扰项），按阶段取用；user_quiz_used 防重复出题
-- quiz_type: word_zh 看词选义 | listen_zh 听音选义 | zh_word 中文选词 | cloze 例句挖空 | sense 选义项 | spell 听写
-- stage:     learn 新学 | d2 | d5 | d12 复习阶段 | confirm 表格验证轮
CREATE TABLE IF NOT EXISTS word_quiz (
  id         INTEGER PRIMARY KEY,
  word_id    INTEGER NOT NULL REFERENCES words(id),
  quiz_type  TEXT NOT NULL,
  stage      TEXT NOT NULL,
  idx        INTEGER NOT NULL DEFAULT 1,       -- 同词同stage的第几题
  stem       TEXT NOT NULL DEFAULT '',
  options    TEXT NOT NULL DEFAULT '[]',       -- JSON 数组；spell 题为 []
  answer     TEXT NOT NULL,
  UNIQUE(word_id, quiz_type, stage, idx)
);
CREATE INDEX IF NOT EXISTS idx_quiz_lookup ON word_quiz(word_id, stage);

CREATE TABLE IF NOT EXISTS user_quiz_used (
  user_id INTEGER NOT NULL REFERENCES users(id),
  quiz_id INTEGER NOT NULL REFERENCES word_quiz(id),
  used_at TEXT DEFAULT (datetime('now')),
  UNIQUE(user_id, quiz_id)
);

CREATE INDEX IF NOT EXISTS idx_state_review ON user_word_state(user_id, next_review_date);
CREATE INDEX IF NOT EXISTS idx_levels_word ON word_levels(word_id);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn):
    """已有库的列迁移（CREATE IF NOT EXISTS 不会更新已存在表）"""
    words_cols = [r["name"] for r in conn.execute("PRAGMA table_info(words)")]
    if "frq" not in words_cols:
        conn.execute("ALTER TABLE words ADD COLUMN frq INTEGER")
    if "emoji" not in words_cols:
        conn.execute("ALTER TABLE words ADD COLUMN emoji TEXT")
    if "forms" not in words_cols:
        conn.execute("ALTER TABLE words ADD COLUMN forms TEXT")
    uws_cols = [r["name"] for r in conn.execute("PRAGMA table_info(user_word_state)")]
    if "verify_count" not in uws_cols:
        conn.execute("ALTER TABLE user_word_state ADD COLUMN verify_count INTEGER DEFAULT 0")
    users_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
    if "grade" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN grade TEXT")
    if "screening_skipped" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN screening_skipped INTEGER DEFAULT 0")
    if "spot_done" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN spot_done INTEGER DEFAULT 0")
    if "confirm_done" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN confirm_done INTEGER DEFAULT 0")
    if "basic_quiz_done" not in users_cols:
        conn.execute("ALTER TABLE users ADD COLUMN basic_quiz_done INTEGER DEFAULT 0")
    senses_cols = [r["name"] for r in conn.execute("PRAGMA table_info(word_senses)")]
    if "confidence" not in senses_cols:
        conn.execute("ALTER TABLE word_senses ADD COLUMN confidence TEXT DEFAULT 'high'")
    dl_cols = [r["name"] for r in conn.execute("PRAGMA table_info(daily_logs)")]
    if "report_sent" not in dl_cols:
        conn.execute("ALTER TABLE daily_logs ADD COLUMN report_sent INTEGER DEFAULT 0")
    if "score" not in dl_cols:
        conn.execute("ALTER TABLE daily_logs ADD COLUMN score INTEGER")


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"数据库初始化完成: {DB_PATH}")

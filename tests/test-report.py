"""日报发送路径单测（mock SMTP，不真实外发）：
生成→发送→置位→幂等跳过。在仓库根目录跑：python3 tests/test-report.py
"""
import os
import sys
import sqlite3
import tempfile
import datetime
from pathlib import Path
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import report  # noqa: E402

DB = os.environ.get('WL_DB') or os.path.join(os.path.dirname(__file__), '..', 'app', 'data', 'wordslearn.db')
os.environ.setdefault('WL_DB', DB)   # report 模块经 database.get_db() 也指向同一库（默认生产，勿在生产跑）
TODAY = datetime.date.today().isoformat()


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    # 临时用户（绕开发信护栏），测完清理
    import bcrypt as _bcrypt
    old = db.execute("SELECT id FROM users WHERE username='wl_rt'").fetchone()  # wl_rt 不在 e2e_ 前缀内
    if old:
        db.execute("DELETE FROM quiz_log WHERE user_id=?", (old['id'],))
        db.execute("DELETE FROM daily_logs WHERE user_id=?", (old['id'],))
        db.execute("DELETE FROM users WHERE id=?", (old['id'],))
    uid = db.execute("INSERT INTO users(username,password_hash) VALUES('wl_rt', ?)",
                     (_bcrypt.hashpw(b'x', _bcrypt.gensalt()).decode(),)).lastrowid
    wid = db.execute("SELECT id FROM words LIMIT 1").fetchone()['id']
    wid2 = db.execute("SELECT id FROM words WHERE id != ? LIMIT 1", (wid,)).fetchone()['id']
    # 造当日数据：2 复习（1 错）+ 关卡 2 首答（1 错，两词——v3.2.1 起 (word_id,level) 去重，
    # 同词同关的第二条首答会被当"重开双批"丢弃）+ 1 补测（对）
    # → 积分 = 100 - 1×1 = 99（补测不返分）
    db.execute("INSERT INTO daily_logs(user_id, date, new_done, review_done) VALUES(?,?,1,2) "
               "ON CONFLICT(user_id, date) DO UPDATE SET new_done=1, review_done=2, report_sent=0, score=NULL", (uid, TODAY))
    db.execute("DELETE FROM quiz_log WHERE user_id=? AND date=?", (uid, TODAY))
    for phase, level, correct, retry, w in (('learn', None, 1, 0, wid), ('review', None, 0, 0, wid),
                                            ('review', None, 1, 0, wid),
                                            ('gate', 4, 1, 0, wid), ('gate', 4, 0, 0, wid2),
                                            ('gate', 4, 1, 1, wid2)):
        db.execute("INSERT INTO quiz_log(user_id, date, phase, level, word_id, quiz_type, correct, retry) "
                   "VALUES(?,?,?,?,?,?,?,?)", (uid, TODAY, phase, level, w, 'spell', correct, retry))
    db.commit()
    db.close()

    # 临时 SMTP 配置（假主机；SMTP_SSL mock 掉不真实连接）。
    # 写临时文件并 patch SMTP_CONF——绝不写真实 app/data/smtp.json
    # （旧版收尾写回空 pass 占位，曾覆盖真实授权码导致日报静默停发）
    tmp_conf = Path(tempfile.mkdtemp()) / 'smtp.json'
    tmp_conf.write_text('{"host":"smtp.126.com","port":465,"user":"weilingme@126.com","pass":"x",'
                        '"to":"velenh@qq.com, feishengnan88@163.com"}')
    sent_mails = []
    try:
        with mock.patch.object(report, 'SMTP_CONF', tmp_conf), \
             mock.patch.object(report.smtplib, 'SMTP_SSL') as MS:
            inst = MS.return_value.__enter__.return_value
            inst.sendmail = lambda f, t, m: sent_mails.append((f, t, m))

            out = report.send_daily_report(uid)
            assert out['sent'] is True, out
            assert len(sent_mails) == 1, '应发送一封'
            f, t, raw = sent_mails[0]
            assert sorted(t) == ['feishengnan88@163.com', 'velenh@qq.com'], t  # 多收件人拆分
            # MIME 编码（主题 RFC2047、正文 base64）→ 解码后断言
            import email
            from email.header import decode_header
            m = email.message_from_string(raw)
            subj = ''.join(p.decode(c or 'utf-8') if isinstance(p, bytes) else p
                           for p, c in decode_header(m['Subject']))
            body = m.get_payload(decode=True).decode('utf-8')
            subj_ok = '学习日报' in subj and '99分' in subj and TODAY[5:] in subj
            html_ok = ('每日学习报告' in body and '听写' in body and '给家长的建议' in body
                       and '今日复习' in body and '今日得分' in body and '99' in body)
            print('✓ 发送成功：126发件→QQ收件，主题含得分，HTML 含复习板块', '(subj:', subj_ok, 'html:', html_ok, ')')
            assert subj_ok and html_ok

            db = sqlite3.connect(DB)
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT report_sent, score FROM daily_logs WHERE user_id=? AND date=?",
                             (uid, TODAY)).fetchone()
            db.close()
            assert row['report_sent'] == 1 and row['score'] == 99
            print('✓ report_sent 置位 + 积分结算 99（100-1）')

            out2 = report.send_daily_report(uid)
            assert out2['sent'] is False and '已发送' in out2['reason'], out2
            assert len(sent_mails) == 1, '幂等：不重发'
            print('✓ 二次调用幂等跳过（积分与邮件均不变）')
    finally:
        os.remove(tmp_conf)
        db = sqlite3.connect(DB)
        db.execute("DELETE FROM quiz_log WHERE user_id=?", (uid,))
        db.execute("DELETE FROM daily_logs WHERE user_id=?", (uid,))
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
        db.close()
    print('✅ ALL PASS (report 发送路径单测)')


if __name__ == '__main__':
    main()

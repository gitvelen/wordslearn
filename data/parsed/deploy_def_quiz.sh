#!/bin/bash
# listen_def 题库生产入库（v3.4 部署链：追加→零改动门禁→备份→增量入库→核对→审计）
# 前置：gen_out/def_quiz.jsonl 终版（L1+L2 全绿）；生产服务运行中（题库 bind mount 即时可见，
#       但旧代码无路径命中 listen_def——已验证，题库先行安全）
# 用法: bash data/parsed/deploy_def_quiz.sh
set -e
cd "$(dirname "$0")"
PROD_DB=../../app/data/wordslearn.db
SRC=gen_out/def_quiz.jsonl
DEST=word_quiz.jsonl

echo "== 1) 追加前快照 =="
BEFORE_LINES=$(wc -l < "$DEST")
BEFORE_MD5=$(md5sum "$DEST" | cut -d' ' -f1)
USED_BEFORE=$(python3 -c "
import sqlite3; db=sqlite3.connect('file:$PROD_DB?mode=ro',uri=True)
print(db.execute('SELECT COUNT(*) FROM user_quiz_used').fetchone()[0]); db.close()")
QUIZ_BEFORE=$(python3 -c "
import sqlite3; db=sqlite3.connect('file:$PROD_DB?mode=ro',uri=True)
print(db.execute('SELECT COUNT(*) FROM word_quiz').fetchone()[0]); db.close()")
echo "   word_quiz.jsonl $BEFORE_LINES 行；DB 题 $QUIZ_BEFORE；记账 $USED_BEFORE"

echo "== 2) 追加 def 题（尾部 append，零改动门禁） =="
cat "$SRC" >> "$DEST"
AFTER_LINES=$(wc -l < "$DEST")
[ $((AFTER_LINES - BEFORE_LINES)) -eq $(wc -l < "$SRC") ] || { echo "追加行数不符，中止"; exit 1; }
# 门禁：除尾部新增行外逐字节不变
head -n "$BEFORE_LINES" "$DEST" | md5sum | grep -q "$BEFORE_MD5" \
  && echo "   零改动门禁通过（存量 $BEFORE_LINES 行逐字节不变）" || { echo "存量行被改动，中止"; exit 1; }

echo "== 3) DB 备份 =="
cp "$PROD_DB" "../../app/data/wordslearn.db.bak_v34_$(date +%m%d_%H%M)"
ls ../../app/data/ | grep bak_v34 | tail -1

echo "== 4) 增量入库（UPSERT 保 quiz_id 与记账） =="
python3 -I seed_quiz_incremental.py

echo "== 5) 核对 =="
python3 -c "
import sqlite3
db = sqlite3.connect('file:$PROD_DB?mode=ro', uri=True)
n_def = db.execute(\"SELECT COUNT(*) FROM word_quiz WHERE quiz_type='listen_def'\").fetchone()[0]
n_all = db.execute('SELECT COUNT(*) FROM word_quiz').fetchone()[0]
used = db.execute('SELECT COUNT(*) FROM user_quiz_used').fetchone()[0]
assert n_def == $(wc -l < $SRC), f'listen_def 行数 {n_def} != 源 $SRC'
assert used == $USED_BEFORE, f'记账被清/变：{used} != $USED_BEFORE'
print(f'✓ listen_def {n_def} 行；总题 {n_all}（+$((n_all-$QUIZ_BEFORE))）；记账保留 {used}')
db.close()"

echo "== 6) 生产库 L1 审计 =="
python3 -I audit_quiz_ambiguity.py
echo "== 入库完成 =="

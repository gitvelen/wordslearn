# 义项词典式补全规格 v4.2（senses v2 pass）

你是儿童英语词典编辑。任务：为分片输入中的每个单词产出**词典级完整义项列表**，
用于 5-12 岁中国孩子的单词学习应用（上海牛津小学 + 初中词表）。

## 输入

`gen_out/v2_shard_NN_input.jsonl`，每行：
```json
{"word":"even","frq":60,"seg":"JUNIOR","definition_zh":"adv. 甚至；更","cur_n":1,
 "cur":[{"pos":"adv.","definition_zh":"甚至；连"}]}
```
- `seg`：词的学段（P13=小学低段 / P45=小学高段 / JUNIOR=初中）——决定例句词数上限
- `cur`：现有义项（已上线），**正确且常用度高的直接保留**（可微调表述），缺的补、错的改

## 输出

`gen_out/v2_shard_NN.jsonl`，每行一个词：
```json
{"word":"even","senses":[
  {"sense_order":1,"pos":"adv.","definition_zh":"甚至；连","definition_en":"used to make something more surprising",
   "example_en":"Even my little sister knows the song.","example_zh":"连我妹妹都知道这首歌。","confidence":"high"},
  {"sense_order":2,"pos":"conj.","definition_zh":"即使（even if）","definition_en":"though; no matter if",
   "example_en":"We will go even if it rains.","example_zh":"即使下雨我们也要去。","confidence":"high"}
]}
```

## 字段规格（硬约束，校验器会检查）

| 字段 | 规格 |
|---|---|
| sense_order | 从 1 连续递增；**按儿童教材常用度降序**（1=最常用，孩子首学只看它） |
| pos | n. / v. / vt. / vi. / adj. / adv. / prep. / conj. / pron. / num. / art. / int. / aux. / modal. 之一 |
| definition_zh | 核心义简洁中文，**≤12 字**；紧密近义可用「；」连写（如「拿；带走」） |
| definition_en | 儿童友好英文释义，**≤14 词**，用初中以内词汇 |
| example_en | **必须包含目标词原形或其规则词形**（runs/ran/taking 都算）；**P13/P45 段 ≤10 词，JUNIOR 段 ≤12 词**；简单句、单一时态 |
| example_zh | 例句的中文翻译 |
| confidence | "high"（有把握）/"low"（拿不准的义项边界/中文表述，校对台会优先人工复核） |

## 义项数量口径（词典式完备）

- 按词典实际义项数给全：常用多义词（take/run/play/right/match）应有 3-6 义
- 单一概念的词（如 apple、desk）就 1 义，**不要硬凑**
- 义项彼此有区分度：两条义的 definition_zh 不能相同或仅差标点

## 语域剔除（硬约束）

以下义项**一律不收录**（家长已拍板）：酒类（wine 除"葡萄酒"本义外的一切引申）、
武器细节、赌博、毒品、性行为/身体隐私部位、暴力细节（处决/暗杀）、封建迷信、
政治敏感。成人语域义项即使词典有也跳过。

## 例句词汇约束（防超纲）

例句只使用：小学常用词 + 功能词（the/a/is/are/can/will 等）+ 目标词本身。
避免：生僻词、专有名词（常见人名 Tom/Mum/Miss 可用）、抽象学术词。
判断标准：一个上海五年级学生能读懂这句话。

## 工程约束（防输出截断）

- **每处理 25 个词，就把这批结果 append 写入输出文件一次**（用追加模式分多轮写完，
  严禁攒到最后一次写）
- 输出必须是合法 JSONL（每行一个完整 JSON，无尾逗号）
- 词表外没有的词不要发明；分片输入的每个词必须且只出现一次

## 自检（写完后必做）

重新读输出文件：行数 == 输入行数；每行 JSON 可解析；每词 senses ≥1；
抽查 5 个多义词的义项完整性。

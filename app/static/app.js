/* wordslearn 前端逻辑（Vue3 CDN 版）
   流程：年级 → 目标 → 表格确认（速选+验证轮）→ 建计划 → 今日任务
   今日任务 = 热身轮（昨日词，不提交）→ 新词卡片连播 → 复习组 → 分关测验（刷新可恢复）
   音频原则：每题进题自动读一遍，答后不重播；zh_word/cloze 禁朗读（防听音猜答案） */
const app = Vue.createApp({
  data() {
    return {
      view: 'login', busy: false, locking: false,
      user: null, me: null,
      loginForm: { username: localStorage.getItem('wl_last_user') || '', password: '' },
      loginErr: '',
      toast: { show: false, msg: '', type: 'info' },
      planForm: { target_words: 900, days: 60 },
      today: null, todayErr: false,
      cal: { month: '', days: [] },
      audioCtx: null,
      // 词条弹层（复习答错看卡 / 词典查看）
      wordDetail: null, detailMode: 'view', detailLocked: false,
      // 跟读（junior）：idle → recording → done
      rec: { state: 'idle', url: '', err: '' },
      welcome: { step: 1, goal: 0, days: 60 },
      // iOS 每次页面重载都重置媒体解锁——解锁标记不能跨刷新保留，否则刷新后全部播放被静默吞掉
      audioReady: false,
      cardLocked: false,   // 新词卡片注视期（进卡 2.5s 内不可点「下一个」）
      scoreState: null,    // 积分：{today, total, detail}（完成事件结算后回填）
      // 表格确认（v4.1 摸底）：basic 快测 → table 速选 → verify 验证 → done
      confirm: { phase: 'basic', words: [], page: 0, known: {}, quiz: [], qidx: 0,
                 picked: null, pickedOk: null, result: null, knownCount: 0,
                 basicQuiz: [], basicIdx: 0, basicPicked: null, basicOk: null, basicResult: null },
      PAGE_SIZE: 100,
      GRADE_LABELS: { '1A': '一年级上', '1B': '一年级下', '2A': '二年级上', '2B': '二年级下',
                      '3A': '三年级上', '3B': '三年级下', '4A': '四年级上', '4B': '四年级下',
                      '5A': '五年级上', '5B': '五年级下', '6A': '六年级上', '6B': '六年级下',
                      '7A': '七年级上', '7B': '七年级下', '8A': '八年级上', '8B': '八年级下',
                      '9A': '九年级上', '9B': '九年级下', 'adult': '家长体验' },
    };
  },

  computed: {
    isJunior() { return this.user && this.user.mode === 'junior'; },
    confirmPages() { return Math.ceil(this.confirm.words.length / this.PAGE_SIZE); },
    confirmPageWords() {
      const s = this.confirm.page * this.PAGE_SIZE;
      return this.confirm.words.slice(s, s + this.PAGE_SIZE);
    },
    confirmKnownCount() { return Object.keys(this.confirm.known).length; },
    curConfirmQuiz() { return this.confirm.quiz[this.confirm.qidx]; },
    todayQueue() { return this.today ? this.today.queue : []; },
    curItem() { return (this.today && this.todayQueue[this.today.qIdx]) || null; },
    todayPct() {
      if (!this.today || !this.today.queue.length) return 0;
      // 分母用建队快照：错词重插只加长队列不改分母，进度条单调不回跳
      const total = this._len0 || this.today.queue.length;
      return Math.min(100, Math.round(this.today.qIdx / total * 100));
    },
    calOffset() {
      if (!this.cal.days.length) return 0;
      return new Date(this.cal.days[0].date + 'T00:00:00').getDay();
    },
    todayStr() {
      const d = new Date();
      d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
      return d.toISOString().slice(0, 10);
    },
    progressPct() {
      if (!this.me || !this.me.progress || !this.me.progress.plan) return 0;
      const p = this.me.progress;
      return Math.min(100, Math.round((p.dist.mastered + p.dist.learning) / p.plan.target_words * 100));
    },
    dailyNewPreview() {
      return Math.max(1, Math.ceil(this.planForm.target_words / Math.max(1, this.planForm.days - 12)));
    },
    goalOptions() {
      return this.isJunior
        ? [{ words: 60, name: '轻松起步', emoji: '🌱' }, { words: 120, name: '稳步前进', emoji: '🌿' }, { words: 200, name: '小小挑战', emoji: '🌳' }]
        : [{ words: 300, name: '轻松起步', emoji: '🌱' }, { words: 600, name: '稳步前进', emoji: '🌿' }, { words: 900, name: '挑战自我', emoji: '🌳' }];
    },
    // 声音解锁屏：每次页面加载出现一次（含中途刷新——iOS 重载即重置媒体授权）
    learnIntro() {
      return this.today && this.today.queue.length > 0 && !this.audioReady;
    },
  },

  watch: {
    curItem(w) {
      if (!w) return;
      // 新词卡片注视期：至少听完一遍自动朗读（2.5s）才能翻下一张。
      // 放在 learnIntro 判断之前——首张卡在解锁屏期间就已进入，同样要锁。
      if (w.type === 'newcard') {
        this.cardLocked = true;
        clearTimeout(this._cardTimer);
        this._cardTimer = setTimeout(() => { this.cardLocked = false; }, 2500);
      }
      // 拼写题：按答案字母数初始化格子（复习 d12 拼写可能在队首，不受解锁屏影响）
      if ((w.type === 'quiz' || w.type === 'review') && w.item.quiz &&
          w.item.quiz.quiz_type === 'spell') {
        const n = (w.item.quiz.answer.match(/[a-zA-Z]/g) || []).length;
        this.today.spellCells = Array(n).fill('');
      }
      if (this.learnIntro) return;   // intro 未解锁时不自动播
      this.$nextTick(() => {
        this.autoPlayItem(w);
        this.prefetchAudio();
        // 听写关：自动聚焦第一格（iPad 键盘不收起）
        if (w.item && w.item.quiz && w.item.quiz.quiz_type === 'spell') {
          const el = document.querySelector('.spell-cell');
          if (el) el.focus();
        }
      });
    },
  },

  async mounted() {
    try {
      const me = await this.api('/api/me');
      this.me = me; this.user = me.user; this.route();
    } catch (e) {
      if (e.status === 401) this.view = 'login';
    }
  },

  methods: {
    async api(path, opts = {}) {
      const init = { headers: {}, credentials: 'same-origin' };
      if (opts.method) init.method = opts.method;
      if (opts.body) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
      let r;
      try {
        r = await fetch(path, init);
      } catch (e) {
        this.showToast('网络异常，请检查网络后重试', 'bad');
        throw e;
      }
      if (r.status === 401 && path !== '/api/login') { this.view = 'login'; throw Object.assign(new Error('未登录'), { status: 401 }); }
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw Object.assign(new Error(data.detail || ('HTTP ' + r.status)), { status: r.status });
      return data;
    },

    showToast(msg, type = 'info') {
      this.toast = { show: true, msg, type };
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast.show = false; }, 2600);
    },

    // 正误音效：WebAudio 合成，零外部文件（短促：对=两声叮咚 0.16s，错=低音 0.16s）
    playSfx(ok) {
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        if (!this.audioCtx) this.audioCtx = new AC();
        if (this.audioCtx.state === 'suspended') this.audioCtx.resume();
        const ctx = this.audioCtx;
        const notes = ok ? [[880, 0, 0.08], [1175, 0.07, 0.09]] : [[240, 0, 0.16]];
        for (const [freq, delay, dur] of notes) {
          const o = ctx.createOscillator(), g = ctx.createGain();
          o.connect(g); g.connect(ctx.destination);
          o.type = ok ? 'sine' : 'triangle';
          o.frequency.value = freq;
          const t0 = ctx.currentTime + delay;
          g.gain.setValueAtTime(0.001, t0);
          g.gain.exponentialRampToValueAtTime(ok ? 0.12 : 0.1, t0 + 0.01);
          g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
          o.start(t0); o.stop(t0 + dur + 0.02);
        }
      } catch (e) { /* 音频不可用时静默 */ }
    },

    route() {
      if (this.me.progress.plan) { this.view = 'today'; this.loadToday(); return; }
      if (!this.user.grade) { this.welcome = { step: 1, goal: 0, days: 60 }; this.view = 'welcome'; return; }
      if (this.me.need_confirm) { this.view = 'confirm'; this.startConfirm(); return; }
      this.applyGoalDefault();
      this.view = 'setup';
    },
    applyGoalDefault() {
      const saved = parseInt(localStorage.getItem('wl_goal_' + this.user.username) || '0', 10);
      const days = parseInt(localStorage.getItem('wl_days_' + this.user.username) || '0', 10) || 60;
      const def = saved || (this.user.mode === 'junior' ? 150 : 900);
      this.planForm = { target_words: def, days };
    },
    async pickGrade(code) {
      this.busy = true;
      try {
        await this.api('/api/profile/grade', { method: 'POST', body: { grade: code } });
        await this.loadMe();
        this.welcome.step = 2;
      } catch (e) { this.showToast(e.message, 'bad'); }
      this.busy = false;
    },
    pickGoal() {
      localStorage.setItem('wl_goal_' + this.user.username, String(this.welcome.goal));
      localStorage.setItem('wl_days_' + this.user.username, String(this.welcome.days));
      if (this.me && this.me.need_confirm) { this.welcome.step = 3; }   // standard → 表格确认
      else { this.applyGoalDefault(); this.view = 'setup'; }            // junior/adult → 直接建计划
    },
    async skipScreening() {
      if (!confirm('跳过确认的话，已会的单词也会出现在学习计划里。确定跳过吗？')) return;
      this.busy = true;
      try {
        await this.api('/api/profile/skip_screening', { method: 'POST' });
        await this.loadMe();
        this.applyGoalDefault();
        this.view = 'setup';
      } catch (e) { this.showToast(e.message, 'bad'); }
      this.busy = false;
    },

    switchView(v) {
      this.view = v;
      if (v === 'calendar') this.loadCal();
      if (v === 'progress') this.loadMe();
    },

    async loadMe() { this.me = await this.api('/api/me'); this.user = this.me.user; },

    // ---------- 登录 ----------
    async doLogin() {
      this.loginErr = ''; this.busy = true;
      try {
        const r = await this.api('/api/login', { method: 'POST', body: this.loginForm });
        localStorage.setItem('wl_last_user', this.loginForm.username.trim());
        this.user = { username: r.username, mode: r.mode, display_name: r.username };
        this.me = await this.api('/api/me');
        this.user = this.me.user;
        this.route();
      } catch (e) {
        this.loginErr = (e.status === 401 || e.status === 429) ? e.message : '登录失败，请稍后再试';
      }
      this.busy = false;
    },
    async logout() {
      await this.api('/api/logout', { method: 'POST' });
      this.user = null; this.me = null; this.view = 'login';
      this.loginForm = { username: localStorage.getItem('wl_last_user') || '', password: '' };
    },

    // ---------- 表格确认（v4.1 摸底）----------
    async startConfirm() {
      this.confirm = { phase: 'basic', words: [], page: 0, known: {}, quiz: [], qidx: 0,
                       picked: null, pickedOk: null, result: null, knownCount: 0,
                       basicQuiz: [], basicIdx: 0, basicPicked: null, basicOk: null, basicResult: null };
      const st = await this.api('/api/confirm/status');
      if (st.verify > 0) {   // 旧版验证流程遗留：verify 词按免验证语义直接确认跳过
        await this.api('/api/confirm/finish', { method: 'POST' });
        await this.loadMe();
        this.applyGoalDefault();
        this.view = 'setup';
        this.showToast('挑选已生效，制定计划开始吧！', 'ok');
        return;
      }
      if (this.me && this.me.need_basic_quiz) {   // ① 基础词快测
        const r = await this.api('/api/confirm/basic_quiz');
        if (r.quiz && r.quiz.length) { this.confirm.basicQuiz = r.quiz; return; }
      }
      await this.confirmLoadTable();              // ② 或直接表格
    },
    async confirmLoadTable() {
      this.confirm.phase = 'table';
      const r = await this.api('/api/confirm/pool?size=' + Math.ceil(this.planPreviewTarget() * 1.3));
      this.confirm.words = r.words;
    },
    curBasicQuiz() { return this.confirm.basicQuiz[this.confirm.basicIdx]; },
    async basicAnswer(opt) {
      if (this.confirm.basicPicked != null || this.locking) return;
      this.locking = true;
      const q = this.curBasicQuiz();
      const ok = opt === q.answer;
      if (!this._basicAns) this._basicAns = {};      // 非响应式：快测答案回放
      this._basicAns[q.word_id] = ok;
      this.confirm.basicPicked = opt; this.confirm.basicOk = ok;
      this.playSfx(ok);
      await new Promise(res => setTimeout(res, ok ? 600 : 1200));
      this.confirm.basicIdx++;
      this.confirm.basicPicked = null; this.confirm.basicOk = null;
      if (this.confirm.basicIdx >= this.confirm.basicQuiz.length) await this.basicFinish();
      this.locking = false;
    },
    async basicFinish() {
      const answers = {};
      for (const q of this.confirm.basicQuiz) answers[q.word_id] = this._basicAns[q.word_id] === true;
      const r = await this.api('/api/confirm/basic_result', { method: 'POST', body: { answers } });
      this.confirm.basicResult = r;
      this.confirm.phase = 'basicDone';
      this.playSfx(r.passed);
      await this.loadMe();
    },
    basicOptClass(opt) {
      const q = this.curBasicQuiz();
      if (!q || this.confirm.basicPicked == null) return '';
      if (opt === q.answer) return 'correct';
      if (opt === this.confirm.basicPicked) return 'wrong';
      return '';
    },
    basicDoneAction() { this.confirmLoadTable(); },
    planPreviewTarget() {
      return parseInt(localStorage.getItem('wl_goal_' + this.user.username) || '0', 10) || 900;
    },
    toggleKnown(w) {
      if (this.confirm.known[w.id]) delete this.confirm.known[w.id];
      else this.confirm.known[w.id] = true;
      this.confirm.known = { ...this.confirm.known };   // 触发响应式
    },
    confirmPageShift(n) {
      const p = this.confirm.page + n;
      if (p < 0 || p >= this.confirmPages) return;
      this.confirm.page = p;
      window.scrollTo({ top: 0 });
    },
    async confirmSubmit() {
      if (this.confirm.page < this.confirmPages - 1) { this.confirm.page++; window.scrollTo({ top: 0 }); return; }
      const knownIds = Object.keys(this.confirm.known).map(Number);
      this.confirm.knownCount = knownIds.length;
      this.busy = true;
      try {
        if (knownIds.length) {
          await this.api('/api/confirm/submit', { method: 'POST', body: { known_ids: knownIds } });
        }
        await this.confirmFinishFlow(knownIds.length);   // 免验证：点认识直接排除
      } catch (e) { this.showToast(e.message, 'bad'); }
      this.busy = false;
    },
    async confirmNextQuizBatch() {
      this.confirm.phase = 'verify';
      this.confirm.picked = null; this.confirm.pickedOk = null;
      const r = await this.api('/api/confirm/quiz?size=10');
      if (r.quiz.length) { this.confirm.quiz = r.quiz; this.confirm.qidx = 0; }
      else { await this.confirmFinishFlow(this.confirm.knownCount); }
    },
    async confirmQuizAnswer(opt) {
      if (this.confirm.picked != null || this.locking) return;
      this.locking = true;
      const q = this.curConfirmQuiz;
      const ok = opt === q.answer;
      this.confirm.picked = opt; this.confirm.pickedOk = ok;
      this.playSfx(ok);
      try {
        await this.api('/api/confirm/verify', { method: 'POST', body: { word_id: q.word_id, correct: ok } });
      } catch (e) { this.locking = false; return; }
      if (!ok) this.confirm.knownCount = Math.max(0, this.confirm.knownCount - 1);   // 打脸回池
      setTimeout(async () => {
        this.confirm.qidx++; this.confirm.picked = null; this.confirm.pickedOk = null;
        if (this.confirm.qidx >= this.confirm.quiz.length) await this.confirmNextQuizBatch();
        this.locking = false;
      }, ok ? 700 : 1400);
    },
    confirmQuizOptClass(opt) {
      const q = this.curConfirmQuiz;
      if (!q || this.confirm.picked == null) return '';
      if (opt === q.answer) return 'correct';
      if (opt === this.confirm.picked) return 'wrong';
      return '';
    },
    async confirmFinishFlow(skipped) {
      const st = await this.api('/api/confirm/status');
      this.confirm.result = { skipped: st.skipped };
      this.confirm.phase = 'done';
      this.playSfx(true);
      await this.api('/api/confirm/finish', { method: 'POST' });
      await this.loadMe();
    },
    confirmDoneAction() {
      this.applyGoalDefault();
      this.view = 'setup';
    },

    // ---------- 计划 ----------
    async createPlan() {
      this.busy = true;
      try {
        await this.api('/api/plan', { method: 'POST', body: this.planForm });
        await this.loadMe();
        this.showToast('计划创建成功，开始今天的学习吧！', 'ok');
        this.view = 'today'; this.loadToday();
      } catch (e) { this.showToast(e.message, 'bad'); }
      this.busy = false;
    },

    // ---------- 今日任务 ----------
    async loadToday() {
      this.todayErr = false;
      let r;
      try {
        r = await this.api('/api/today');
      } catch (e) {
        this.todayErr = true;   // 加载失败≠任务完成：显示重试卡片，绝不假装"今日完成"
        return;
      }
      this.today = {
        plan: r.plan,
        warmup: r.warmup || [], new: r.new || [], learnedToday: r.learned_today || [],
        review: r.review || [],
        queue: [], qIdx: 0,
        answered: false, feedback: null, picked: null, spellCells: [],
        doneNew: 0, doneReview: 0, doneQuiz: 0, requeuedCount: 0,
        gateWrong: {},
      };
      this.buildQueue();
      if (this.todayPhaseDone()) {
        await this.loadMe();
        this.api('/api/report/daily', { method: 'POST' })   // 重开时发现当日已完成：补发日报（服务端幂等）
          .catch(() => {}).finally(() => this.loadScore());
      }
    },

    async loadScore() {   // 完成屏积分展示（服务端结算值，前端不可算分防刷）
      try { this.scoreState = await this.api('/api/score'); } catch (e) {}
    },
    buildQueue() {
      /* v2.1 结构：热身 → 新词卡片连播（纯呈现） → 复习组（沉淀间隔）→ 分关测验（题型分批，
      各关词序独立打乱）→ 关末结算（错词同题型重测）。
      关卡覆盖「今天待学 + 今天已学」（learnedToday）——中途刷新页面关卡不丢。
      standard 五关：认词/听音/回想/听写/用词(组词)；junior 三关：认词/听音/选词。 */
      const q = [];
      for (const w of this.today.warmup) q.push({ type: 'warmup', item: w });
      this.today.new.forEach((n, i) => q.push({ type: 'newcard', item: n, _i: i + 1 }));
      for (const rv of this.today.review) q.push({ type: 'review', item: rv });

      const todayAll = [...this.today.new, ...this.today.learnedToday];   // 今日全部新词
      this.today.newAll = todayAll;
      const junior = this.isJunior;
      const gates = junior
        ? [['word_zh', '认词'], ['listen_zh', '听音'], ['_pick', '选词']]
        : [['word_zh', '认词'], ['listen_zh', '听音'], ['zh_word', '回想'],
           ['spell', '听写'], ['_reinforce', '用词']];
      gates.forEach(([t, name], gi) => {
        const items = [];
        todayAll.forEach(n => {
          let quiz = null;
          if (t === '_reinforce') quiz = n.reinforce;
          else if (t === '_pick') quiz = (n.quizzes || []).find(x => x.quiz_type === 'emoji')
              || (n.quizzes || []).find(x => x.quiz_type === 'zh_word');
          else quiz = (n.quizzes || []).find(x => x.quiz_type === t);
          if (quiz) items.push({ type: 'quiz', item: { ...n, quiz }, _level: gi + 1 });
        });
        // 各关词序独立打乱（学习顺序≠测验顺序，逼出真提取）
        for (let i = items.length - 1; i > 0; i--) {
          const j = Math.floor(Math.random() * (i + 1));
          [items[i], items[j]] = [items[j], items[i]];
        }
        items.forEach((it, i) => { it._qi = i + 1; it._qn = items.length; });
        if (items.length) {           // 无新词日（纯复习）不出空关卡
          q.push(...items);
          q.push({ type: 'gate', _level: gi + 1, _gname: name });
        }
      });
      this.today.queue = q;
      this.today.gateWrong = {};      // level → Set(word_id)（本关未过关词）
      this.today.doneNew = this.today.learnedToday.length;   // 刷新恢复：已学卡计入统计
      this._len0 = q.length;          // 进度条分母快照（todayPct 用）
    },
    todayPhaseDone() { return this.today && this.today.qIdx >= this.today.queue.length; },
    autoPlayItem(w) {
      const word = w.item && w.item.word;
      if (w.type === 'warmup' && w.item.quiz && w.item.quiz.quiz_type === 'listen_zh') { this.playWord(word); return; }
      if ((w.type === 'review' || w.type === 'quiz') && w.item.quiz &&
          (w.item.quiz.quiz_type === 'listen_zh' || w.item.quiz.quiz_type === 'spell'
           || w.item.quiz.quiz_type === 'emoji')) {
        this.playWord(word);   // 听音/听写/选词关：进入即自动读题
        return;
      }
      if (w.type === 'newcard') {
        this.playWord(word);
        if (this.isJunior) {
          const d = w.item.senses && w.item.senses[0] && w.item.senses[0].definition_zh
            || w.item.definition_zh;
          if (d) setTimeout(() => this.speakZh(d), 1600);
        }
      }
    },
    // 题干/题型渲染辅助
    quizShowWord(w) {
      // listen_zh 不显示词（听音答题）；spell 只显示输入框；其余显示题干
      const q = w.item.quiz;
      if (!q) return true;
      return q.quiz_type !== 'listen_zh';
    },
    quizShowSpell(w) {
      return w.item.quiz && w.item.quiz.quiz_type === 'spell';
    },
    // 答案含目标词读音的题型（看中文选英文/句子挖空）：朗读=报答案，禁 🔊
    quizMuted(w) {
      const t = w && w.item && w.item.quiz && w.item.quiz.quiz_type;
      return t === 'zh_word' || t === 'cloze';
    },
    quizStemText(w) {
      const q = w.item.quiz;
      if (!q) return '';
      if (q.quiz_type === 'zh_word') return q.stem;             // 中文义 → 选英文词
      if (q.quiz_type === 'cloze') return q.stem;               // 挖空句
      if (q.quiz_type === 'sense') return w.item.word;          // 词 → 选正确义项
      if (q.quiz_type === 'word_zh') return w.item.word;        // 词 → 选释义
      if (q.quiz_type === 'emoji') return '';                   // emoji 大图 + 听音（模板单独渲染）
      return '';
    },
    quizLabel(w) {
      const t = w.item.quiz && w.item.quiz.quiz_type;
      return { 'word_zh': '选出正确意思', 'listen_zh': '听一听，选出意思', 'zh_word': '哪个单词是这个意思？',
               'cloze': '选出句子中缺少的词', 'sense': '选出这个词在这句话里的意思', 'spell': '听一听，拼写这个单词',
               'emoji': '听一听，选出这个单词' }[t] || '选出正确答案';
    },
    // ---------- v2.1 关卡测验：统一答题（答对自动过 900ms；答错弹词条卡后继续）----------
    gateName(level) {
      const junior = this.isJunior;
      const names = junior ? ['认词', '听音', '选词'] : ['认词', '听音', '回想', '听写', '用词'];
      return names[level - 1] || '闯关';
    },
    isGateItem(w) { return w && w.type === 'quiz'; },

    autoNext(delay) {   // 答对自动下一题（统一只读一遍：进题自动播，答后不重播）
      clearTimeout(this._autoTimer);
      this._autoTimer = setTimeout(() => {
        if (this.today && this.today.answered) this.advance();
      }, delay);
    },
    async advance() {   // 队列推进（自动/手动共用；防重由调用方的 locking 负责）
      if (!this.today) return;
      const w = this.curItem;
      // 硬闸：题目未作答不得推进（热身带题/复习/关卡题）；gate/无题热身/卡片不受限
      if (w && !this.today.answered &&
          (w.type === 'quiz' || w.type === 'review' ||
           (w.type === 'warmup' && w.item.quiz))) return;
      this.today.qIdx++;
      this.today.answered = false; this.today.feedback = null; this.today.picked = null;
      this.today.spellCells = [];
      if (this.todayPhaseDone()) {
        await this.loadMe(); this.playSfx(true);
        this.api('/api/report/daily', { method: 'POST' })   // 完成事件：结算积分 + 学习日报
          .catch(() => {}).finally(() => this.loadScore());
      }
    },

    // 新词卡片：纯呈现，手动推进（孩子自己控节奏）；learn 提交在卡片完成时
    // cardLocked 注视期：进卡 2.5s 内「下一个」不可点——单词自动朗读一遍的时间，杜绝秒过连点
    async cardNext() {
      if (this.locking || this.cardLocked) return;
      this.locking = true;
      try {
        await this.api('/api/answer', { method: 'POST', body: { word_id: this.curItem.item.id, phase: 'learn', correct: true, quiz_type: 'card' } });
        this.today.doneNew++;
        await this.advance();
      } finally { this.locking = false; }
    },
    isLastCard() {
      return this.curItem && this.curItem.type === 'newcard' && this.curItem._i === this.today.new.length;
    },

    // 热身轮：对自动过；错停手动（低压但要看到正确答案）
    warmupAnswer(opt) {
      if (this.today.answered) return;
      const q = this.curItem.item.quiz;
      if (!q) { this.advance(); return; }
      const ok = opt === q.answer;
      this.today.answered = true; this.today.picked = opt;
      this.today.feedback = { ok, warmup: true };
      this.playSfx(ok);
      this.postQuizLog({ phase: 'warmup', correct: ok });
      if (ok) this.autoNext(900);
    },

    // 复习（调度轮）：首答提交；答对自动过；答错弹卡后点继续推进
    reviewAnswer(opt) {
      if (this.today.answered) return;
      const w = this.curItem;
      const q = w.item.quiz;
      this.afterReviewAnswer(opt === q.answer, opt, null);
    },
    // 拼写逐格输入：字母格待填；空格/连字符/撇号显示为固定分隔（同纸面听写画横线）
    spellSlots(w) {
      const q = w && w.item && w.item.quiz;
      if (!q) return [];
      const slots = []; let li = 0;
      for (const ch of q.answer) {
        if (/[a-zA-Z]/.test(ch)) slots.push({ t: 'L', i: li++ });
        else slots.push({ t: 'S', ch });
      }
      return slots;
    },
    spellCellInput(e, i) {
      // 单格只收字母（过滤输入法上屏的汉字/标点/联想词），满格自动跳下一格
      const el = e.target;
      const v = (el.value || '').replace(/[^a-zA-Z]/g, '');
      this.today.spellCells[i] = v.slice(-1) || '';
      el.value = this.today.spellCells[i];
      if (this.today.spellCells[i]) {
        const cells = [...document.querySelectorAll('.spell-cell')];
        if (cells[i + 1]) cells[i + 1].focus();
        else el.blur();   // 填到最后一格：收起键盘露出提交按钮
      }
    },
    spellCellKey(e, i) {
      // 空格上按退格 → 回上一格（继续删）
      if (e.key === 'Backspace' && !this.today.spellCells[i]) {
        const cells = [...document.querySelectorAll('.spell-cell')];
        if (cells[i - 1]) { cells[i - 1].focus(); e.preventDefault(); }
      }
    },
    spellType(text) {   // 测试钩子：直接填充拼写格（绕过键盘事件）
      const cells = this.today.spellCells || [];
      const t = (text || '').replace(/[^a-zA-Z]/g, '').slice(0, cells.length);
      for (let i = 0; i < cells.length; i++) cells[i] = t[i] || '';
      this.today.spellCells = [...cells];
    },
    spellSubmit() {
      if (this.today.answered || this.locking) return;
      const w = this.curItem;
      if (!w || !w.item.quiz) return;
      const typed = (this.today.spellCells || []).join('').trim();
      if (!typed) {   // 空提交不判错：防误触回车把没答的题记成错词
        this.showToast('先听一听，把单词拼出来吧', 'info');
        return;
      }
      this.locking = true;
      try {
        const letters = (w.item.quiz.answer.match(/[a-zA-Z]/g) || []).join('');
        const ok = typed.toLowerCase() === letters.toLowerCase();
        if (w.type === 'quiz') { this.afterQuizAnswer(ok, typed, true); }
        else { this.afterReviewAnswer(ok, typed, true); }
      } finally { this.locking = false; }
    },
    afterReviewAnswer(ok, picked, isSpell) {
      const w = this.curItem;
      this.today.answered = true; this.today.picked = picked;
      this.today.feedback = { ok, spell: !!isSpell };
      this.playSfx(ok);
      if (w._requeued) {
        if (!ok) this.today.queue.push({ ...w });
        else this.today.doneReview++;
        this.autoNext(ok ? 900 : 2200);
        return;
      }
      if (ok) {
        this.today.doneReview++;
        this.autoNext(900);
      } else {
        this.today.requeuedCount++;
        this.today.queue.push({ ...w, _requeued: true });
        this.openWordDetail(w.item.id, 'wrong');   // 弹卡：点「记住了，继续」即推进
      }
      this.api('/api/answer', { method: 'POST', body: { word_id: w.item.id, phase: w.item.audit ? 'audit' : 'review',
        correct: ok, quiz_type: (w.item.quiz && w.item.quiz.quiz_type) || '' } });
    },

    // 关卡测验题：不进调度；答错记入关末重测
    quizAnswer(opt) {
      if (this.today.answered) return;
      const w = this.curItem;
      this.afterQuizAnswer(opt === w.item.quiz.answer, opt, false);
    },
    afterQuizAnswer(ok, picked, isSpell) {
      const w = this.curItem;
      this.today.answered = true; this.today.picked = picked;
      this.today.feedback = { ok, spell: !!isSpell };
      this.playSfx(ok);
      const level = w._level;
      if (!this.today.gateWrong[level]) this.today.gateWrong[level] = new Set();
      this.postQuizLog({ phase: 'gate', level, correct: ok, retry: !!w._retry });
      if (ok) {
        this.today.gateWrong[level].delete(w.item.id);
        this.today.doneQuiz++;
        this.autoNext(900);
      } else {
        if (!w._retry) this.today.gateWrong[level].add(w.item.id);
        this.openWordDetail(w.item.id, 'wrong');
      }
    },

    // 学习行为埋点（热身/关卡不进调度，仅入 quiz_log 供日报）；失败静默不打断答题
    postQuizLog(extra) {
      const w = this.curItem;
      if (!w || !w.item) return;
      this.api('/api/quiz_log', { method: 'POST', body: { entries: [Object.assign({
        word_id: w.item.id, quiz_type: (w.item.quiz && w.item.quiz.quiz_type) || '',
      }, extra)] } }).catch(() => {});
    },

    // 关末结算：错词同题型重测（测到对为止）
    gateWrongWords(level) {
      const ids = this.today.gateWrong[level] || new Set();
      return [...ids];
    },
    gateRetry() {
      if (this.locking) return;   // 防双击 splice 两次
      this.locking = true;
      try {
        const g = this.curItem;
        const level = g._level;
        const wrongIds = this.gateWrongWords(level);
        // 用今日全部新词的原题重建（同关同题型；learnedToday 刷新恢复场景同覆盖）
        const retry = [];
        for (const n of (this.today.newAll || this.today.new)) {
          if (!wrongIds.includes(n.id)) continue;
          let quiz = null;
          const t = this.isJunior
            ? ['word_zh', 'listen_zh', '_pick'][level - 1]
            : ['word_zh', 'listen_zh', 'zh_word', 'spell', '_reinforce'][level - 1];
          if (t === '_reinforce') quiz = n.reinforce;
          else if (t === '_pick') quiz = (n.quizzes || []).find(x => x.quiz_type === 'emoji')
              || (n.quizzes || []).find(x => x.quiz_type === 'zh_word');
          else quiz = (n.quizzes || []).find(x => x.quiz_type === t);
          if (quiz) retry.push({ type: 'quiz', item: { ...n, quiz }, _level: level, _retry: true });
        }
        // 移除当前 gate，重测题紧随其后；重测完会再遇 gate（此时若清空则放行）
        this.today.queue.splice(this.today.qIdx, 1, ...retry, g);
      } finally { this.locking = false; }
    },

    async itemNext() {
      // 通用「继续」：gate/热身错题/其他停顿态（locking 防双击连跳两题）
      if (this.locking) return;
      this.locking = true;
      try { await this.advance(); } finally { this.locking = false; }
    },
    optClass(opt) {
      if (!this.today || !this.today.answered) return '';
      const w = this.curItem;
      const q = w.item.quiz;
      if (opt === (q || {}).answer) return 'correct';
      if (opt === this.today.picked) return 'wrong';
      return '';
    },

    // ---------- 日历 ----------
    async loadCal() {
      if (!this.cal.month) this.cal.month = this.todayStr.slice(0, 7);
      const r = await this.api('/api/calendar?month=' + this.cal.month);
      this.cal.days = r.days;
    },
    calShift(n) {
      const [y, m] = this.cal.month.split('-').map(Number);
      const d = new Date(y, m - 1 + n, 1);
      this.cal.month = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
      this.loadCal();
    },

    // ---------- 学习前手势解锁（iOS 声音）----------
    startLearning() {
      this.audioReady = true;
      // iOS 两道独立锁都要在手势栈内解锁：Audio 元素 + speechSynthesis（TTS 兜底路径）
      try {
        const u = new SpeechSynthesisUtterance('ok');
        u.volume = 0.1; u.lang = 'en-US';
        speechSynthesis.cancel(); speechSynthesis.speak(u);
      } catch (e) { /* 无 TTS 时静默 */ }
      const w = this.curItem;
      if (w) this.autoPlayItem(w);   // 手势调用栈内，iOS 放行
    },

    // ---------- 音频 ----------
    playWord(word) {
      // 单音频管理：播新的先停旧的（杜绝重叠双读）；远程加载悬挂 2.5s 超时转 TTS（杜绝无声卡死）
      if (this._curAudio) { try { this._curAudio.pause(); } catch (e) {} this._curAudio = null; }
      const a = new Audio('/audio/' + encodeURIComponent(word) + '.mp3');
      this._curAudio = a;
      let settled = false;
      const fallback = () => {
        if (settled) return;
        settled = true;
        try { a.pause(); } catch (e) {}
        this.speakTTS(word);
      };
      const timer = setTimeout(fallback, 2500);
      a.onerror = fallback;
      a.play()
        .then(() => { if (!settled) { settled = true; clearTimeout(timer); } })
        .catch(fallback);
      return a;   // 调用方（跟读）可监听 ended
    },
    prefetchAudio() {
      // 预取后续 2 题的单词音频：队列在前端已知，弱网下进题即播
      if (!this.today) return;
      this._pf = this._pf || {};
      for (let k = 1; k <= 2; k++) {
        const it = this.todayQueue[this.today.qIdx + k];
        const w = it && it.item && it.item.word;
        if (w && !this._pf[w]) {
          this._pf[w] = 1;
          try {
            const p = new Audio('/audio/' + encodeURIComponent(w) + '.mp3');
            p.preload = 'auto';
            p.load();
          } catch (e) {}
        }
      }
    },
    speakTTS(text) {
      if (!('speechSynthesis' in window)) return;
      const u = new SpeechSynthesisUtterance(text);
      u.lang = 'en-US'; u.rate = 0.85;
      // iOS：cancel 后同步 speak 会静默丢句；resume + 短延迟是标准 workaround
      try { speechSynthesis.resume(); } catch (e) {}
      speechSynthesis.cancel();
      setTimeout(() => { try { speechSynthesis.speak(u); } catch (e) {} }, 50);
    },
    speakZh(text) {
      if (!('speechSynthesis' in window)) return;
      const u = new SpeechSynthesisUtterance((text || '').replace(/\|/g, '；'));
      u.lang = 'zh-CN'; u.rate = 0.9;
      try { speechSynthesis.resume(); } catch (e) {}
      speechSynthesis.cancel();
      setTimeout(() => { try { speechSynthesis.speak(u); } catch (e) {} }, 50);
    },
    playSentence(word, order, text) {   // 例句朗读：edge-tts 音频优先（AriaNeural），无文件回退浏览器 TTS
      const a = new Audio('/audio/sent/' + encodeURIComponent(word) + '__' + order + '.mp3');
      a.onerror = () => this.speakTTS(text);
      a.play().catch(() => this.speakTTS(text));
    },

    // ---------- 词条弹层（答错看卡 / 词典查看）----------
    async openWordDetail(wordId, mode) {
      try {
        const d = await this.api('/api/word_detail?id=' + wordId);
        this.wordDetail = d; this.detailMode = mode || 'view';
        // 答错场景：按钮 1s 强制注视期（防肌肉记忆秒点，纠错失效）
        this.detailLocked = mode === 'wrong';
        if (this.detailLocked) setTimeout(() => { this.detailLocked = false; }, 1000);
      } catch (e) { /* 拉取失败静默：不打断答题流 */ }
    },
    closeWordDetail() {
      const wasWrong = this.detailMode === 'wrong';
      this.wordDetail = null; this.detailMode = 'view';
      // 答错场景点「记住了，继续」= 关闭 + 推进下一题（两次点击合并为一次）
      if (wasWrong && this.today && this.today.answered) {
        clearTimeout(this._autoTimer);
        setTimeout(() => this.advance(), 250);
      }
    },
    wordFormsText(w) {
      // words.forms JSON（ECDICT exchange：p过去式/d过去分词/i现在分词/3三单/s复数/r比较级/t最高级）
      if (!w || !w.forms) return '';
      const L = { p: '过去式', d: '过去分词', i: '现在分词', 3: '第三人称单数', s: '复数',
                  r: '比较级', t: '最高级', 0: '原形' };
      try {
        const o = JSON.parse(w.forms);
        return Object.entries(o).map(([k, v]) => `${L[k] || k} ${v}`).join(' · ');
      } catch (e) { return ''; }
    },

    // ---------- 跟读（junior，录音回放自比，不打分）----------
    async startRecord() {
      this.rec = { state: 'recording', url: '', err: '' };
      const a = this.playWord(this.curItem.item.word);   // 先听标准音，播完再录
      try {
        if (!navigator.mediaDevices || !window.MediaRecorder) throw new Error('unsupported');
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const rec = new MediaRecorder(stream);
        const chunks = [];
        rec.ondataavailable = e => chunks.push(e.data);
        rec.onstop = () => {
          stream.getTracks().forEach(t => t.stop());
          this.rec = { state: 'done', url: URL.createObjectURL(new Blob(chunks, { type: 'audio/webm' })), err: '' };
          setTimeout(() => new Audio(this.rec.url).play().catch(() => {}), 300);   // 录完自动回放
        };
        this._recorder = rec;
        let started = false;
        const go = () => {   // 标准音 ended 后开录；音频被 TTS 兜底/失败时靠超时兜底
          if (started || this._recorder !== rec) return;
          started = true;
          try { rec.start(); } catch (e) {}
          setTimeout(() => this.stopRecord(), 7000);   // 最长 7s 自动停（从开录起算）
        };
        if (a) {
          a.addEventListener('ended', go, { once: true });
          setTimeout(go, 3500);
        } else {
          setTimeout(go, 900);
        }
      } catch (e) {
        this.rec = { state: 'idle', url: '', err: '麦克风用不了，先跟读给爸爸妈妈听吧～' };
      }
    },
    stopRecord() {
      if (this._recorder && this._recorder.state === 'recording') this._recorder.stop();
      else if (this.rec.state === 'recording') this.rec.state = 'idle';
    },
    playRecording() {
      if (this.rec.url) new Audio(this.rec.url).play().catch(() => {});
    },
  },
});
const vm = app.mount("#app"); window.__vm = vm;   // e2e测试钩子

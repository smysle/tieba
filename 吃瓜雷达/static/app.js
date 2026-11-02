/* 瓜田雷达 · 前端（无构建、无依赖） */
(function () {
  'use strict';

  var S = {
    state: null,
    tid: null,
    thread: null,
    stats: null,
    mode: 'all',
    q: '',
    offset: 0,
    floors: [],
    mediaByFloor: {},
    mediaIndex: {},
    stories: [],
    story: null,
    hasMore: false,
    reportMode: 'daily',
    neutral: false,
    jobId: null,
    jobKind: 'summary',
    jobTimer: null,
    lastCount: 0,
    kind: 'thread',
    settingsOpen: false,
    retry: 0
  };

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmtTs(ts) {
    if (!ts) { return ''; }
    var d = new Date(ts * 1000);
    return pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function ago(ts) {
    if (!ts) { return ''; }
    var delta = Math.max(0, Date.now() / 1000 - ts);
    if (delta < 45) { return '刚刚'; }
    if (delta < 3600) { return Math.floor(delta / 60) + ' 分钟前'; }
    if (delta < 86400) { return Math.floor(delta / 3600) + ' 小时前'; }
    return Math.floor(delta / 86400) + ' 天前';
  }
  function spanText(seconds) {
    seconds = Math.max(0, seconds || 0);
    if (seconds < 60) { return Math.round(seconds) + ' 秒'; }
    if (seconds < 3600) { return Math.round(seconds / 60) + ' 分钟'; }
    if (seconds < 86400) { return Math.floor(seconds / 3600) + ' 小时'; }
    return Math.floor(seconds / 86400) + ' 天 ' + Math.floor((seconds % 86400) / 3600) + ' 小时';
  }
  function parseTid(raw) {
    raw = String(raw || '').trim();
    if (/^\d+$/.test(raw)) { return parseInt(raw, 10); }
    var m = raw.match(/(?:\/p\/|tid=|kz=)(\d{5,})/);
    if (m) { return parseInt(m[1], 10); }
    m = raw.match(/\b(\d{8,})\b/);
    return m ? parseInt(m[1], 10) : null;
  }
  var toastTimer = null;
  function toast(message, isError) {
    var node = $('#toast');
    node.textContent = message;
    node.classList.toggle('err', !!isError);
    node.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.classList.remove('show'); }, 2600);
  }

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || 'GET', headers: { 'Content-Type': 'application/json' } };
    if (options.body) { init.body = options.body; }
    return fetch(path, init).then(function (res) {
      return res.json().catch(function () { return { ok: false, error: 'HTTP ' + res.status }; })
        .then(function (data) {
          if (!res.ok || data.ok === false) {
            throw new Error(data.error || ('HTTP ' + res.status));
          }
          return data;
        });
    });
  }

  /* ---------------------------------------------------------- 渲染：顶栏 */
  function renderMonitor() {
    var m = (S.state && S.state.monitor) || {};
    var pill = $('#monitor-pill');
    var running = !!m.running;
    pill.className = 'pill ' + (running ? 'online' : 'offline');
    pill.innerHTML = '<i></i><span>' + (running ? '监控中' : '已暂停') + '</span>';
    $('#uptime').textContent = running
      ? ('已跑 ' + spanText(m.uptime) + (m.inflight && m.inflight.length ? ' · 抓取中 ' + m.inflight.length : ''))
      : '监控线程未启动';
  }

  function renderStats() {
    var s = (S.state && S.state.stats) || {};
    var items = [
      ['监控源', s.sources || 0],
      ['帖', s.threads || 0],
      ['楼层', s.floors || 0],
      ['瓜', s.stories || 0],
      ['报告', s.reports || 0]
    ];
    $('#stats').innerHTML = items.map(function (pair) {
      return '<span class="stat">' + pair[0] + ' <b>' + pair[1] + '</b></span>';
    }).join('');
  }

  function renderLlmBadge() {
    var el = $('#llm-badge');
    if (!el) { return; }
    var info = (S.state && S.state.llm) || {};
    el.classList.toggle('llm-on', !!info.configured);
    el.textContent = info.configured
      ? ((info.type === 'cli' ? 'CLI · ' : 'AI · ') + (info.model || '已配置'))
      : '本地规则';
  }

  /* ---------------------------------------------------------- 渲染：监控源 */
  function renderSources() {
    var sources = (S.state && S.state.sources) || [];
    $('#source-count').textContent = sources.length;
    $('#sources').innerHTML = sources.map(function (src) {
      var kindLabel = { thread: '帖', forum: '吧', hot: '榜' }[src.kind] || '源';
      var title = (src.thread && src.thread.title) || src.title || src.key;
      var dotClass = !src.enabled ? 'paused' : (src.status === 'error' ? 'err' : '');
      var sparkValues = src.spark || [];
      var hasSpark = sparkValues.some(function (value) { return value > 0; });
      var spark = hasSpark ? sparkValues.map(function (value) {
        var h = Math.min(18, 2 + value * 2.2);
        return '<i class="' + (value ? 'hot' : '') + '" style="height:' + h + 'px"></i>';
      }).join('') : '';
      var sub = [];
      if (src.kind === 'thread') {
        sub.push((src.thread && src.thread.floors_now ? src.thread.floors_now : 0) + ' 楼');
        if (src.new_recent) { sub.push('<span class="src-new">+' + src.new_recent + ' 新</span>'); }
      } else if (src.kind === 'forum') {
        sub.push('每 ' + Math.round((src.interval || 300) / 60) + ' 分钟扫新帖');
      } else {
        sub.push('热榜变化提醒');
      }
      if (src.last_poll) { sub.push(ago(src.last_poll)); }
      if (src.status === 'fetching') { sub.push('<span class="src-fetching">抓取中…</span>'); }
      if (src.status === 'error') {
        sub.push('<span class="src-error" title="' + esc(src.last_error) + '">上次失败</span>');
      }
      return '<div class="source' + (String(S.tid) === String(src.key) ? ' active' : '') + '"' +
        ' data-id="' + src.id + '" data-kind="' + src.kind + '" data-key="' + esc(src.key) + '">' +
        '<div class="source-top"><span class="source-kind ' + src.kind + '">' + kindLabel + '</span>' +
        '<span class="source-title">' + esc(title) + '</span></div>' +
        '<div class="source-sub"><span class="dot ' + dotClass + '"></span>' + sub.join(' · ') + '</div>' +
        (hasSpark ? '<div class="spark">' + spark + '</div>' : '') +
        '<div class="source-actions">' +
        '<button class="icon-btn" data-act="poll">抓</button>' +
        '<button class="icon-btn" data-act="toggle">' + (src.enabled ? '停' : '开') + '</button>' +
        '<button class="icon-btn" data-act="del">删</button>' +
        '</div></div>';
    }).join('');
    $('#sources-empty').classList.toggle('hidden', sources.length > 0);
  }

  function bindSources() {
    $('#sources').addEventListener('click', function (event) {
      var card = event.target.closest('.source');
      if (!card) { return; }
      var id = card.dataset.id;
      var kind = card.dataset.kind;
      var key = card.dataset.key;
      var actBtn = event.target.closest('[data-act]');
      if (actBtn) {
        event.stopPropagation();
        var act = actBtn.dataset.act;
        if (act === 'del') {
          if (!confirm('不再监控这个源？已抓的楼层会保留。')) { return; }
          api('/api/sources/' + id, { method: 'DELETE' }).then(function () {
            toast('已移除');
            if (String(S.tid) === String(key)) { S.tid = null; $('#thread-view').classList.add('hidden'); $('#thread-empty').classList.remove('hidden'); }
            refreshState();
          }).catch(function (err) { toast(err.message, true); });
        } else if (act === 'toggle') {
          api('/api/sources/' + id + '/toggle', { method: 'POST', body: '{}' })
            .then(function () { refreshState(); }).catch(function (err) { toast(err.message, true); });
        } else if (act === 'poll') {
          api('/api/sources/' + id + '/poll', { method: 'POST', body: '{}' })
            .then(function () { toast('已排队，马上抓'); setTimeout(refreshState, 1200); })
            .catch(function (err) { toast(err.message, true); });
        }
        return;
      }
      if (kind === 'thread') {
        selectThread(parseTid(key));
      } else if (kind === 'hot') {
        openHot();
      } else {
        toast('这个吧的新帖会自动进监控并转入事件流');
      }
    });
  }

  /* ---------------------------------------------------------- 渲染：帖子 */
  function renderThreadHead(thread, stats) {
    $('#thread-empty').classList.add('hidden');
    $('#thread-view').classList.remove('hidden');
    $('#thread-title').textContent = thread.title || ('帖 ' + thread.tid);
    $('#thread-link').href = 'https://tieba.baidu.com/p/' + thread.tid;
    var chips = [
      ['tid', thread.tid],
      ['吧', thread.forum || '—'],
      ['楼主', thread.author || '—'],
      ['楼层', (stats && stats.floors) || thread.floors_now || 0],
      ['参与', (stats && stats.participants) || thread.participants || 0],
      ['跨度', spanText((stats && stats.span) || 0)],
      ['最近', ago(thread.last_seen)]
    ];
    $('#thread-meta').innerHTML = chips.map(function (pair) {
      return '<span class="meta-chip">' + esc(pair[0]) + ' <b>' + esc(pair[1]) + '</b></span>';
    }).join('');
    document.title = (thread.title || '瓜田雷达') + ' · 瓜田雷达';
  }

  function highlight(text, keywords) {
    var out = esc(text || '');
    var words = (keywords || []).filter(Boolean).map(function (word) {
      return esc(word).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).sort(function (a, b) { return b.length - a.length; });
    if (words.length) {
      try {
        out = out.replace(new RegExp('(' + words.join('|') + ')', 'g'), '<mark>$1</mark>');
      } catch (err) { /* 关键词里有奇怪字符就跳过高亮 */ }
    }
    return out.replace(/\n/g, '<br>');
  }

  function floorCard(floor, keywords) {
    var subs = (floor.subs || []).map(function (sub) {
      return '<div class="sub"><b>@' + esc(sub.user_name || '匿名') + '</b>：' +
        highlight(sub.content, keywords) + '</div>';
    }).join('');
    var media = S.mediaByFloor[(floor.floor || 0) + ':0'] || [];
    var mediaHtml = media.length
      ? '<div class="floor-media">' + media.map(function (item, index) {
          var src = item.thumb_url || item.url;
          return src ? '<img src="' + esc(src) + '" data-media="' + item.id +
            '" alt="图' + (index + 1) + '" loading="lazy">' : '';
        }).join('') + '</div>'
      : '';
    var foot = [];
    if (floor.likes) { foot.push('赞 ' + floor.likes); }
    if (floor.sub_count) { foot.push('楼中楼 ' + floor.sub_count); }
    if (floor.images) { foot.push('图 ' + floor.images); }
    return '<article class="floor ' + (floor.is_op ? 'op ' : '') + (floor.starred ? 'starred' : '') +
      '" data-floor="' + floor.floor + '">' +
      '<div class="floor-head">' +
      '<span class="floor-no">#' + floor.floor + '</span>' +
      '<span class="floor-user">@' + esc(floor.user_name || '匿名') + '</span>' +
      (floor.is_op ? '<span class="op-tag">楼主</span>' : '') +
      '<span class="floor-time">' + fmtTs(floor.ts) + '</span>' +
      '<button class="star-btn ' + (floor.starred ? 'on' : '') + '" data-star="' + floor.floor +
      '" title="标记为瓜点">' + (floor.starred ? '★' : '☆') + '</button>' +
      '</div>' +
      '<div class="floor-body">' + highlight(floor.content || '（空）', keywords) + '</div>' +
      mediaHtml +
      (foot.length ? '<div class="floor-foot">' + foot.map(function (f) { return '<span>' + f + '</span>'; }).join('') + '</div>' : '') +
      (subs ? '<div class="subs">' + subs + '</div>' : '') +
      '</article>';
  }

  function renderFloors() {
    var keywords = (S.state && S.state.config && S.state.config.monitor && S.state.config.monitor.keywords) || [];
    var box = $('#floors');
    if (!S.floors.length) {
      box.innerHTML = '<div class="empty"><div class="empty-emoji">🫧</div><p>这个筛选下没有楼层。</p></div>';
    } else {
      box.innerHTML = S.floors.map(function (floor) { return floorCard(floor, keywords); }).join('');
    }
    $('#floor-count').textContent = '已加载 ' + S.floors.length + ' 楼' + (S.hasMore ? '（还有更多）' : '');
    $('#load-more-wrap').classList.toggle('hidden', !S.hasMore);
  }

  function bindFloors() {
    $('#floors').addEventListener('click', function (event) {
      var media = event.target.closest('[data-media]');
      if (media) { openMedia(media.dataset.media); return; }
      var star = event.target.closest('[data-star]');
      if (!star || !S.tid) { return; }
      var floorNo = parseInt(star.dataset.star, 10);
      var floor = S.floors.filter(function (f) { return f.floor === floorNo; })[0];
      if (!floor) { return; }
      var next = !floor.starred;
      api('/api/floors/' + S.tid + '/' + floorNo + '/star', {
        method: 'POST', body: JSON.stringify({ starred: next })
      }).then(function () {
        floor.starred = next;
        star.classList.toggle('on', next);
        star.textContent = next ? '★' : '☆';
        star.closest('.floor').classList.toggle('starred', next);
        toast(next ? '已标为瓜点，切瓜时会优先带上' : '已取消瓜点');
      }).catch(function (err) { toast(err.message, true); });
    });
  }

  function loadThread(reset) {
    if (!S.tid) { return Promise.resolve(); }
    var params = new URLSearchParams({
      mode: S.mode, q: S.q, limit: '60',
      offset: String(reset ? 0 : S.offset)
    });
    return Promise.all([
      api('/api/threads/' + S.tid + '?' + params.toString()),
      api('/api/threads/' + S.tid + '/media').catch(function () { return { media: [] }; })
    ]).then(function (results) {
      var data = results[0];
      var mediaData = results[1] || { media: [] };
      S.thread = data.thread;
      S.stats = data.stats;
      S.mediaByFloor = {};
      S.mediaIndex = {};
      (mediaData.media || []).forEach(function (item) {
        var key = (item.floor || 0) + ':' + (item.sub_id || 0);
        (S.mediaByFloor[key] = S.mediaByFloor[key] || []).push(item);
        S.mediaIndex[item.id] = item;
      });
      renderThreadHead(data.thread, data.stats);
      if (reset) {
        S.floors = data.floors || [];
        S.offset = S.floors.length;
      } else {
        S.floors = S.floors.concat(data.floors || []);
        S.offset = S.floors.length;
      }
      S.hasMore = !!data.has_more;
      S.lastCount = data.thread.floors_now || 0;
      renderFloors();
      renderReports(data.reports || []);
      $('#btn-new-floors').classList.add('hidden');
      S.retry = 0;
    });
  }

  function selectThread(tid) {
    if (!tid) { return; }
    S.tid = tid;
    S.mode = 'all';
    S.q = '';
    S.offset = 0;
    S.floors = [];
    $('#search').value = '';
    $$('#tabs .tab').forEach(function (tab) { tab.classList.toggle('active', tab.dataset.mode === 'all'); });
    $('#thread-view').classList.remove('hidden');
    $('#thread-empty').classList.add('hidden');
    renderSources();
    loadThread(true).catch(function (err) {
      if (S.retry < 4) {
        S.retry += 1;
        toast('首次抓取中，稍等…');
        setTimeout(function () { selectThread(tid); }, 2200);
      } else {
        toast(err.message, true);
        S.retry = 0;
      }
    });
  }

  function openHot() {
    var box = $('#hot-list');
    box.innerHTML = '<div class="empty small-empty"><span class="spinner"></span> 正在拉热榜…</div>';
    $('#modal-hot').classList.remove('hidden');
    api('/api/hot').then(function (data) {
      var topics = data.topics || [];
      $('#hot-updated').textContent = data.updated
        ? ('更新于 ' + ago(data.updated) + ' · ' + topics.length + ' 条')
        : '还没抓到数据，等一轮监控';
      if (!topics.length) {
        box.innerHTML = '<div class="empty small-empty"><div class="empty-emoji">🔥</div>' +
          '<p>热榜还没抓。点左侧「榜」源的「抓」按钮，或等监控跑一轮。</p></div>';
        return;
      }
      box.innerHTML = topics.map(function (topic, index) {
        var url = 'https://tieba.baidu.com/hottopic/browse/hottopic?topic_id=' +
          encodeURIComponent(topic.tid) + '&topic_name=' + encodeURIComponent(topic.title);
        return '<div class="hot-item"><div class="hot-rank">' + (index + 1) + '</div>' +
          '<div class="hot-main"><a class="hot-title" href="' + url +
          '" target="_blank" rel="noreferrer">' + esc(topic.title) + '</a>' +
          (topic.desc ? '<div class="hot-desc">' + esc(topic.desc) + '</div>' : '') +
          '</div><div class="hot-num">' + esc(topic.hot || '') + '</div></div>';
      }).join('');
    }).catch(function (err) {
      box.innerHTML = '<div class="empty small-empty"><p>' + esc(err.message) + '</p></div>';
    });
  }

  function openMedia(mediaId) {
    var item = S.mediaIndex[mediaId];
    if (!item) { return; }
    var analysis = item.analysis || {};
    $('#media-title').textContent = '#' + (item.floor || 0) + ' 的图';
    $('#media-sub').textContent = analysis.engine ? ('· ' + analysis.engine) : '· 还没识图';
    $('#media-img').src = item.url || item.thumb_url || '';
    var rows = [
      ['类型', analysis.type || '—'],
      ['置信度', analysis.confidence === undefined ? '—' : analysis.confidence],
      ['描述', analysis.description || '—'],
      ['实体', (analysis.entities || []).join('、') || '—'],
      ['金额', (analysis.amounts || []).join('、') || '—']
    ];
    $('#media-analysis').innerHTML = rows.map(function (pair) {
      return '<div class="m-row"><div class="m-label">' + esc(pair[0]) + '</div>' +
        '<div>' + esc(String(pair[1])) + '</div></div>';
    }).join('') + '<div class="m-row"><div class="m-label">OCR</div><pre>' +
      esc(analysis.ocr_text || '（还没有识别结果，点帖子上的「识图」）') + '</pre></div>';
    $('#modal-media').classList.remove('hidden');
  }

  function runVision() {
    if (!S.tid) { toast('先选一个帖', true); return; }
    var configured = S.state && S.state.config && S.state.config.vision &&
      S.state.config.vision.configured;
    var isCli = S.state && S.state.config && S.state.config.vision &&
      S.state.config.vision.type === 'cli';
    var limit = configured ? (isCli ? 3 : 12) : 8;
    var button = $('#btn-vision');
    button.disabled = true;
    S.jobKind = 'vision';
    api('/api/threads/' + S.tid + '/vision', {
      method: 'POST', body: JSON.stringify({ limit: limit })
    }).then(function (data) {
      S.jobId = data.job_id;
      $('#job-box').classList.remove('hidden');
      $('#job-status').textContent = '识图中…';
      $('#job-logs').innerHTML = '';
      pollJob();
    }).catch(function (err) {
      button.disabled = false;
      toast(err.message, true);
    });
  }

  function renderStories() {
    var stories = (S.state && S.state.stories) || [];
    $('#story-empty').classList.toggle('hidden', stories.length > 0);
    $('#story-list').innerHTML = stories.map(function (story) {
      return '<div class="story-item" data-story="' + story.id + '">' +
        '<div class="s-title">' + esc(story.title || '多贴事件') + '</div>' +
        '<div class="s-meta"><span>' + (story.thread_count || 0) + ' 贴</span>' +
        '<span>' + esc(story.forum || '') + '</span>' +
        '<span>' + ago(story.updated_at) + '</span></div></div>';
    }).join('');
  }

  function buildStories() {
    var button = $('#btn-build-stories');
    button.disabled = true;
    api('/api/stories/build', { method: 'POST', body: JSON.stringify({ threshold: 0.55 }) })
      .then(function (data) {
        button.disabled = false;
        toast('聚类完成：' + (data.stories || []).length + ' 个事件');
        refreshState();
      }).catch(function (err) {
        button.disabled = false;
        toast(err.message, true);
      });
  }

  function openStory(storyId) {
    api('/api/stories/' + storyId).then(function (data) {
      var story = data.story;
      S.story = story;
      $('#story-title').textContent = story.title || '多贴事件';
      var stats = story.stats || {};
      $('#story-sub').textContent = [
        (story.threads || []).length + ' 贴',
        (stats.floors || 0) + ' 楼',
        (stats.participants || 0) + ' 人',
        spanText(stats.span || 0)
      ].join(' · ');
      $('#story-threads').innerHTML = (story.threads || []).map(function (thread, index) {
        return '<div class="story-thread-row" data-tid="' + thread.tid + '">' +
          '<span class="st-label">T' + (index + 1) + '</span>' +
          '<span class="st-title">' + esc(thread.title || ('帖 ' + thread.tid)) + '</span>' +
          '<span class="st-role">' + esc(thread.role || '相关帖') + '</span></div>';
      }).join('');
      $('#story-timeline').innerHTML = (story.timeline || []).map(function (row) {
        return '<div class="tl-item"><span class="tl-time">' + fmtTs(row.ts) + '</span>' +
          '<span class="tl-label">' + esc(row.label) + (row.floor ? ('#' + row.floor) : ' 开贴') + '</span>' +
          esc(row.text || '') +
          '<span class="tl-user"> @' + esc(row.user || '匿名') + '</span></div>';
      }).join('') || '<p class="hint">没有时间线数据。</p>';
      var reports = story.reports || [];
      $('#story-report-empty').classList.toggle('hidden', reports.length > 0);
      $('#story-report-list').innerHTML = reports.map(function (report) {
        return '<div class="report-item" data-report="' + report.id + '">' +
          '<span class="r-name">' + esc(report.title || report.mode) + '</span>' +
          '<span class="r-ai ' + (report.provider === 'llm' ? 'llm' : '') + '">' +
          (report.provider === 'llm' ? 'AI' : '规则') + '</span>' +
          '<span class="r-time">' + ago(report.created_at) + '</span></div>';
      }).join('');
      $('#modal-story').classList.remove('hidden');
    }).catch(function (err) { toast(err.message, true); });
  }

  function storySummarize() {
    if (!S.story) { return; }
    var button = $('#story-summarize');
    button.disabled = true;
    S.jobKind = 'story';
    api('/api/stories/' + S.story.id + '/summarize', {
      method: 'POST',
      body: JSON.stringify({ mode: S.reportMode, provider: 'auto', neutral: S.neutral })
    }).then(function (data) {
      S.jobId = data.job_id;
      $('#job-box').classList.remove('hidden');
      $('#job-status').textContent = '合并多贴中…';
      $('#job-logs').innerHTML = '';
      pollJob();
    }).catch(function (err) {
      button.disabled = false;
      toast(err.message, true);
    });
  }

  function openForum() {
    $('#modal-forum').classList.remove('hidden');
    $('#forum-log').innerHTML = '';
  }

  function startForum() {
    var payload = {
      kw: $('#forum-kw').value.trim(),
      pages: parseInt($('#forum-pages').value, 10) || 2,
      keyword: $('#forum-keyword').value.trim(),
      min_replies: parseInt($('#forum-min').value, 10) || 20,
      limit: parseInt($('#forum-limit').value, 10) || 3
    };
    if (!payload.kw) { toast('先填吧名', true); return; }
    var button = $('#forum-start');
    button.disabled = true;
    $('#forum-log').innerHTML = '<div>已排队，开始翻吧列表…</div>';
    api('/api/forum/crawl', { method: 'POST', body: JSON.stringify(payload) })
      .then(function (data) { pollForum(data.job_id, button); })
      .catch(function (err) {
        button.disabled = false;
        toast(err.message, true);
      });
  }

  function pollForum(jobId, button) {
    api('/api/jobs/' + jobId).then(function (data) {
      var job = data.job;
      $('#forum-log').innerHTML = (job.logs || []).map(function (line) {
        return '<div>' + esc(line.text) + '</div>';
      }).join('');
      if (job.status === 'running') {
        setTimeout(function () { pollForum(jobId, button); }, 1500);
        return;
      }
      if (button) { button.disabled = false; }
      if (job.status === 'done') {
        toast('扒吧完成：抓到 ' + ((job.result || {}).threads || []).length + ' 贴');
        refreshState();
      } else {
        toast(job.error || '扒吧失败', true);
      }
    }).catch(function () {
      setTimeout(function () { pollForum(jobId, button); }, 2500);
    });
  }

  /* ---------------------------------------------------------- 渲染：报告 */
  function renderReports(list) {
    var box = $('#report-list');
    if (!list.length) {
      box.innerHTML = '';
      $('#report-empty').classList.remove('hidden');
      return;
    }
    $('#report-empty').classList.add('hidden');
    box.innerHTML = list.map(function (report) {
      var isLlm = report.provider === 'llm';
      return '<div class="report-item" data-report="' + report.id + '">' +
        '<span class="r-name">' + esc(report.title || report.mode) + '</span>' +
        '<span class="r-ai ' + (isLlm ? 'llm' : '') + '">' + (isLlm ? 'AI' : '规则') + '</span>' +
        '<span class="r-time">' + ago(report.created_at) + '</span></div>';
    }).join('');
  }

  function bindReports() {
    $('#report-list').addEventListener('click', function (event) {
      var item = event.target.closest('[data-report]');
      if (item) { openReport(item.dataset.report); }
    });
  }

  function openReport(reportId) {
    $('#report-frame').src = '/api/reports/' + reportId + '/html';
    $('#report-open').href = '/api/reports/' + reportId + '/html';
    $('#report-download').href = '/api/reports/' + reportId + '/md';
    $('#modal-report').classList.remove('hidden');
    api('/api/reports/' + reportId).then(function (data) {
      var report = data.report || {};
      var meta = report.meta || {};
      $('#report-title').textContent = report.title || '吃瓜报告';
      $('#report-sub').textContent = [meta.mode_name, meta.engine, (meta.seconds || 0) + 's']
        .filter(Boolean).join(' · ');
    }).catch(function () {});
  }

  /* ---------------------------------------------------------- 渲染：事件 */
  var EVENT_ICON = {
    new_floors: '🔺', keyword: '💥', op: '📢', error: '⚠️', import: '📥', watch: '👀',
    hot: '🔥', forum: '🏠', auto_watch: '🎯', title: '✏️', system: '🍉'
  };
  function renderEvents() {
    var events = (S.state && S.state.events) || [];
    $('#events-empty').classList.toggle('hidden', events.length > 0);
    $('#events').innerHTML = events.map(function (event) {
      var icon = EVENT_ICON[event.kind] || '•';
      return '<div class="event-item" data-tid="' + (event.tid || '') + '">' +
        '<span class="event-icon">' + icon + '</span>' +
        '<span class="event-text">' + esc(event.text) + '</span>' +
        '<span class="event-time">' + ago(event.ts) + '</span></div>';
    }).join('');
  }

  function bindEvents() {
    $('#events').addEventListener('click', function (event) {
      var item = event.target.closest('[data-tid]');
      if (item && item.dataset.tid) { selectThread(parseInt(item.dataset.tid, 10)); }
    });
  }

  /* ---------------------------------------------------------- 模式选择 */
  function renderModes() {
    var modes = (S.state && S.state.modes) || [];
    if (!modes.length) { return; }
    if (!modes.some(function (m) { return m.key === S.reportMode; })) {
      S.reportMode = modes[0].key;
    }
    $('#modes').innerHTML = modes.map(function (mode) {
      return '<div class="mode-card' + (mode.key === S.reportMode ? ' active' : '') +
        '" data-mode="' + mode.key + '">' +
        '<div class="m-icon">' + mode.icon + '</div>' +
        '<div class="m-name">' + esc(mode.name) + '</div>' +
        '<div class="m-tag">' + esc(mode.tagline) + '</div></div>';
    }).join('');
  }

  function bindModes() {
    $('#modes').addEventListener('click', function (event) {
      var card = event.target.closest('[data-mode]');
      if (!card) { return; }
      S.reportMode = card.dataset.mode;
      $$('#modes .mode-card').forEach(function (node) {
        node.classList.toggle('active', node === card);
      });
    });
  }

  function renderJob(job) {
    $('#job-status').textContent = job.status === 'done'
      ? '报告好了'
      : (job.status === 'error' ? '失败：' + job.error : '生成中…');
    $('#job-logs').innerHTML = (job.logs || []).slice(-8).map(function (line) {
      return '<div>' + esc(line.text) + '</div>';
    }).join('');
  }

  function pollJob() {
    if (!S.jobId) { return; }
    clearTimeout(S.jobTimer);
    api('/api/jobs/' + S.jobId).then(function (data) {
      var job = data.job;
      renderJob(job);
      if (job.status === 'running') {
        S.jobTimer = setTimeout(pollJob, 1200);
      } else {
        $('#btn-summarize').disabled = false;
        $('#btn-vision').disabled = false;
        $('#story-summarize').disabled = false;
        if (job.status === 'done') {
          if (job.result && job.result.report_id) {
            toast('报告出锅了');
            openReport(job.result.report_id);
            loadThread(true);
          } else if (S.jobKind === 'vision') {
            toast('识图完成：' + ((job.result || {}).done || 0) + ' 张');
            loadThread(true);
            refreshState();
          } else {
            toast('任务完成');
            refreshState();
          }
        } else {
          toast(job.error || '任务失败', true);
        }
      }
    }).catch(function () { S.jobTimer = setTimeout(pollJob, 2500); });
  }

  function summarize() {
    if (!S.tid) { toast('先在左边选一个帖子', true); return; }
    S.jobKind = 'summary';
    var button = $('#btn-summarize');
    button.disabled = true;
    $('#job-box').classList.remove('hidden');
    $('#job-status').textContent = '排队中…';
    $('#job-logs').innerHTML = '';
    var payload = {
      tid: S.tid,
      mode: S.reportMode,
      provider: 'auto',
      neutral: S.neutral,
      extra: $('#extra-note').value || ''
    };
    api('/api/summarize', { method: 'POST', body: JSON.stringify(payload) }).then(function (data) {
      S.jobId = data.job_id;
      pollJob();
    }).catch(function (err) {
      button.disabled = false;
      toast(err.message, true);
    });
  }

  /* ---------------------------------------------------------- 状态同步 */
  function refreshState() {
    return api('/api/state').then(function (data) {
      S.state = data;
      renderMonitor();
      renderStats();
      renderLlmBadge();
      renderSources();
      renderStories();
      renderEvents();
      renderModes();
      if (!S.tid) {
        var watched = (data.sources || []).filter(function (src) {
          return src.kind === 'thread' && src.enabled && src.thread;
        })[0];
        var first = watched ? watched.thread : (data.threads || [])[0];
        if (first) { selectThread(first.tid); }
      } else {
        syncThreadFromState();
      }
      return data;
    }).catch(function (err) {
      var pill = $('#monitor-pill');
      pill.className = 'pill offline';
      pill.innerHTML = '<i></i><span>连接断开</span>';
      console.warn(err);
    });
  }

  function syncThreadFromState() {
    var source = (S.state.sources || []).filter(function (src) {
      return src.kind === 'thread' && String(src.key) === String(S.tid);
    })[0];
    if (!source || !source.thread) { return; }
    var thread = source.thread;
    var now = thread.floors_now || 0;
    if (now > S.lastCount && S.lastCount > 0) {
      var pill = $('#btn-new-floors');
      pill.textContent = '有 ' + (now - S.lastCount) + ' 条新楼层 · 刷新';
      pill.classList.remove('hidden');
    }
    if (S.stats) {
      S.stats.participants = S.stats.participants || thread.participants;
    }
  }

  /* ---------------------------------------------------------- 设置 */
  function openSettings() {
    var cfg = (S.state && S.state.config) || {};
    var llmCfg = cfg.llm || {};
    var imgCfg = cfg.image || {};
    var tiebaCfg = cfg.tieba || {};
    var monCfg = cfg.monitor || {};

    var preset = $('#cfg-llm-preset');
    preset.innerHTML = '<option value="">— 手动填写 —</option>' +
      Object.keys(llmCfg.presets || {}).sort().map(function (name) {
        return '<option value="' + name + '">' + name + '</option>';
      }).join('');

    $('#cfg-llm-type').value = llmCfg.type || 'openai';
    $('#cfg-llm-base').value = llmCfg.base_url || '';
    $('#cfg-llm-model').value = llmCfg.model || '';
    $('#cfg-llm-key').value = '';
    $('#cfg-llm-key').placeholder = llmCfg.key_hint ? ('已配置 ' + llmCfg.key_hint + '，留空不改') : '留空 = 不修改';
    $('#cfg-llm-cmd').value = (llmCfg.command || []).join(' ') || 'claude -p';
    $('#llm-test').textContent = '';

    $('#cfg-cookie').value = '';
    $('#cfg-cookie').placeholder = tiebaCfg.has_cookie ? ('已配置 ' + tiebaCfg.cookie_hint + '，留空不改') : '留空 = 匿名抓取';
    $('#cfg-delay').value = tiebaCfg.delay || 0.35;
    $('#cookie-state').textContent = tiebaCfg.has_cookie
      ? '当前带登录态抓取（更稳，能补回被截断的页）'
      : '当前是匿名抓取（r=1 倒序通常够用）';

    $('#cfg-max-pages').value = monCfg.max_pages || 40;
    $('#cfg-keywords').value = (monCfg.keywords || []).join(', ');

    var vCfg = cfg.vision || {};
    $('#cfg-vision-type').value = vCfg.type || 'openai';
    $('#cfg-vision-base').value = vCfg.base_url || '';
    $('#cfg-vision-model').value = vCfg.model || '';
    $('#cfg-vision-key').value = '';
    $('#cfg-vision-key').placeholder = vCfg.key_hint
      ? ('已配置 ' + vCfg.key_hint + '，留空不改') : '留空 = 走本地 OCR';
    $('#cfg-vision-cmd').value = (vCfg.command || []).join(' ') || 'claude -p';
    $('#cfg-vision-enabled').checked = !!vCfg.enabled;

    $('#cfg-img-base').value = imgCfg.base_url || '';
    $('#cfg-img-model').value = imgCfg.model || '';
    $('#cfg-img-key').value = '';
    $('#cfg-img-key').placeholder = imgCfg.key_hint ? ('已配置 ' + imgCfg.key_hint + '，留空不改') : '留空 = 不生图';
    $('#cfg-img-size').value = imgCfg.size || '1024x1024';
    $('#cfg-img-enabled').checked = !!imgCfg.enabled;

    $('#modal-settings').classList.remove('hidden');
    S.settingsOpen = true;
  }

  function bindSettings() {
    $('#cfg-llm-preset').addEventListener('change', function () {
      var name = this.value;
      var cfg = (S.state && S.state.config && S.state.config.llm) || {};
      var preset = (cfg.presets || {})[name];
      if (!preset) { return; }
      $('#cfg-llm-type').value = preset.type || 'openai';
      $('#cfg-llm-base').value = preset.base_url || '';
      $('#cfg-llm-model').value = preset.model || '';
    });

    $('#btn-test-llm').addEventListener('click', function () {
      var label = $('#llm-test');
      label.textContent = '测试中…';
      api('/api/config/test', { method: 'POST', body: '{}' }).then(function (data) {
        var result = data.result || {};
        label.textContent = result.ok ? ('通了：' + (result.reply || 'OK')) : ('没通：' + result.error);
        label.className = 'small ' + (result.ok ? 'ok-text' : 'err-text');
      }).catch(function (err) {
        label.textContent = '没通：' + err.message;
        label.className = 'small err-text';
      });
    });

    $('#btn-save-config').addEventListener('click', function () {
      var payload = {
        llm: {
          type: $('#cfg-llm-type').value,
          base_url: $('#cfg-llm-base').value.trim(),
          model: $('#cfg-llm-model').value.trim(),
          command: $('#cfg-llm-cmd').value.trim()
        },
        tieba: { delay: parseFloat($('#cfg-delay').value) || 0.35 },
        monitor: {
          max_pages: parseInt($('#cfg-max-pages').value, 10) || 40,
          keywords: $('#cfg-keywords').value.split(/[,，\s]+/).filter(Boolean)
        },
        vision: {
          type: $('#cfg-vision-type').value,
          base_url: $('#cfg-vision-base').value.trim(),
          model: $('#cfg-vision-model').value.trim(),
          command: $('#cfg-vision-cmd').value.trim(),
          enabled: $('#cfg-vision-enabled').checked
        },
        image: {
          enabled: $('#cfg-img-enabled').checked,
          base_url: $('#cfg-img-base').value.trim(),
          model: $('#cfg-img-model').value.trim(),
          size: $('#cfg-img-size').value.trim()
        }
      };
      var llmKey = $('#cfg-llm-key').value.trim();
      if (llmKey) { payload.llm.api_key = llmKey; }
      var cookie = $('#cfg-cookie').value.trim();
      if (cookie) { payload.tieba.cookie = cookie; }
      var imgKey = $('#cfg-img-key').value.trim();
      if (imgKey) { payload.image.api_key = imgKey; }
      var visionKey = $('#cfg-vision-key').value.trim();
      if (visionKey) { payload.vision.api_key = visionKey; }

      api('/api/config', { method: 'POST', body: JSON.stringify(payload) }).then(function () {
        toast('设置已保存');
        $('#modal-settings').classList.add('hidden');
        S.settingsOpen = false;
        refreshState();
      }).catch(function (err) { toast(err.message, true); });
    });
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bind() {
    $('#btn-refresh').addEventListener('click', function () {
      refreshState().then(function () { toast('已刷新'); });
    });
    $('#btn-settings').addEventListener('click', openSettings);
    $('#settings-close').addEventListener('click', function () {
      $('#modal-settings').classList.add('hidden');
      S.settingsOpen = false;
    });
    $('#hot-close').addEventListener('click', function () {
      $('#modal-hot').classList.add('hidden');
    });
    $('#report-close').addEventListener('click', function () {
      $('#modal-report').classList.add('hidden');
      $('#report-frame').src = 'about:blank';
    });
    $$('.modal').forEach(function (modal) {
      modal.addEventListener('click', function (event) {
        if (event.target === modal) {
          modal.classList.add('hidden');
          if (modal.id === 'modal-report') { $('#report-frame').src = 'about:blank'; }
        }
      });
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        $$('.modal').forEach(function (modal) { modal.classList.add('hidden'); });
        $('#report-frame').src = 'about:blank';
      }
    });

    $('#kind-seg').addEventListener('click', function (event) {
      var button = event.target.closest('[data-kind]');
      if (!button) { return; }
      S.kind = button.dataset.kind;
      $$('#kind-seg .seg-btn').forEach(function (node) {
        node.classList.toggle('active', node === button);
      });
      $('#add-key').placeholder = {
        thread: '贴吧链接 / tid', forum: '吧名，比如 nba', hot: '点添加即可'
      }[S.kind];
    });

    $('#btn-add').addEventListener('click', function () {
      var key = $('#add-key').value.trim();
      if (S.kind !== 'hot' && !key) { toast('先填链接或吧名', true); return; }
      var payload = { kind: S.kind, key: key || 'hot', interval: parseInt($('#add-interval').value, 10) };
      var button = this;
      button.disabled = true;
      api('/api/sources', { method: 'POST', body: JSON.stringify(payload) }).then(function (data) {
        button.disabled = false;
        $('#add-key').value = '';
        toast('已加入监控');
        refreshState();
        if (S.kind === 'thread') {
          var tid = parseTid(key);
          if (tid) { setTimeout(function () { selectThread(tid); }, 1800); }
        }
      }).catch(function (err) {
        button.disabled = false;
        toast(err.message, true);
      });
    });

    $('#add-key').addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { $('#btn-add').click(); }
    });

    $('#tabs').addEventListener('click', function (event) {
      var tab = event.target.closest('[data-mode]');
      if (!tab) { return; }
      S.mode = tab.dataset.mode;
      $$('#tabs .tab').forEach(function (node) { node.classList.toggle('active', node === tab); });
      loadThread(true).catch(function (err) { toast(err.message, true); });
    });

    $('#search').addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') { return; }
      S.q = this.value.trim();
      loadThread(true).catch(function (err) { toast(err.message, true); });
    });

    $('#btn-load-more').addEventListener('click', function () {
      loadThread(false).catch(function (err) { toast(err.message, true); });
    });

    $('#btn-new-floors').addEventListener('click', function () {
      loadThread(true).catch(function (err) { toast(err.message, true); });
    });

    $('#btn-poll').addEventListener('click', function () {
      var source = (S.state.sources || []).filter(function (src) {
        return src.kind === 'thread' && String(src.key) === String(S.tid);
      })[0];
      if (!source) { toast('这个帖没有监控源', true); return; }
      api('/api/sources/' + source.id + '/poll', { method: 'POST', body: '{}' })
        .then(function () { toast('已排队，马上抓'); setTimeout(function () { loadThread(true); }, 2500); });
    });

    $('#neutral').addEventListener('change', function () { S.neutral = this.checked; });
    $('#btn-summarize').addEventListener('click', summarize);
    $('#btn-vision').addEventListener('click', runVision);
    $('#btn-build-stories').addEventListener('click', buildStories);
    $('#story-close').addEventListener('click', function () {
      $('#modal-story').classList.add('hidden');
    });
    $('#story-summarize').addEventListener('click', storySummarize);
    $('#btn-forum').addEventListener('click', openForum);
    $('#forum-close').addEventListener('click', function () {
      $('#modal-forum').classList.add('hidden');
    });
    $('#forum-start').addEventListener('click', startForum);
    $('#media-close').addEventListener('click', function () {
      $('#modal-media').classList.add('hidden');
    });
    $('#story-list').addEventListener('click', function (event) {
      var item = event.target.closest('[data-story]');
      if (item) { openStory(item.dataset.story); }
    });
    $('#story-report-list').addEventListener('click', function (event) {
      var item = event.target.closest('[data-report]');
      if (item) { openReport(item.dataset.report); }
    });
    $('#story-threads').addEventListener('click', function (event) {
      var row = event.target.closest('[data-tid]');
      if (!row) { return; }
      $('#modal-story').classList.add('hidden');
      selectThread(parseInt(row.dataset.tid, 10));
    });

    bindSources();
    bindFloors();
    bindReports();
    bindEvents();
    bindModes();
    bindSettings();
  }

  function boot() {
    bind();
    refreshState().then(function () {
      var params = new URLSearchParams(location.search);
      if (params.has('settings')) { openSettings(); }
      if (params.has('hot')) { openHot(); }
      if (params.has('story')) { openStory(params.get('story')); }
      if (params.has('media')) {
        setTimeout(function () { openMedia(params.get('media')); }, 2600);
      }
    });
    setInterval(refreshState, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

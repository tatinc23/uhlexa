/* Uhlexa web simulator. Plain JS, no build, no external requests.
   Talks only to the documented HTTP API (spec section 14). Every piece of data is put on the page with
   textContent / createElement (see h() below); nothing is ever parsed as markup. */
(function () {
  'use strict';

  var POLL_MS = 2000;
  var USER_KEY = 'uhlexa.simulator.user';
  var DEMO_SENTENCE = 'Ask Uhlexa to add milk and eggs to Sunday groceries.';
  var QUICK_PROMPTS = [DEMO_SENTENCE, 'Add oat milk to Sunday groceries.'];
  var PROVIDER_LABELS = { primitive: 'Transcripts', subq: 'Decisions', maritime: 'Browser' };
  var PROVIDER_ORDER = ['primitive', 'subq', 'maritime'];
  var ACTIVE_BT = ['queued', 'starting', 'running', 'needs_takeover'];

  var state = {
    users: [],
    handle: null,
    epoch: 0,
    board: null,
    providers: null,
    providerTick: 0,
    providerOpen: null,
    conversations: [],
    activity: [],
    convId: null,
    conv: null,
    routing: null,
    btDetail: {},
    busy: {},
    sending: false,
    online: true,
    loadError: '',
    seenApprovals: null,
    resetArmed: false,
  };
  var sigs = {};
  var polling = false;
  var again = false;
  var timer = null;
  var resetTimer = null;

  var $ = function (id) { return document.getElementById(id); };
  var el = {
    household: $('household'), select: $('user-select'), role: $('role-chip'), reset: $('reset-btn'),
    waiting: $('waiting-link'), providers: $('providers'), providerDetail: $('provider-detail'), conn: $('conn'),
    main: $('main'), threads: $('threads'), transcript: $('transcript'), composer: $('composer'),
    chips: $('quick-chips'), msg: $('msg'), send: $('send-btn'),
    approvals: $('approvals-body'), approvalsTitle: $('approvals-h'),
    browser: $('browser-body'), tasks: $('tasks-body'), tasksTitle: $('tasks-h'),
    activity: $('activity-body'), toasts: $('toasts'),
  };

  /* ---------------------------------------------------------------- tiny helpers */

  // Build DOM safely. Strings become text nodes, never markup.
  function h(tag, props) {
    var node = document.createElement(tag);
    var keys = props ? Object.keys(props) : [];
    keys.forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.slice(0, 2) === 'on') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    });
    for (var i = 2; i < arguments.length; i++) append(node, arguments[i]);
    return node;
  }
  function append(node, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(node, k); }); return; }
    node.appendChild(typeof kid === 'object' && kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  function fill(node) {
    var kids = [];
    for (var i = 1; i < arguments.length; i++) kids.push(arguments[i]);
    node.replaceChildren();
    append(node, kids);
  }

  function storeGet(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function storeSet(key, value) { try { window.localStorage.setItem(key, value); } catch (e) { /* private mode */ } }

  function changed(key, value) {
    var s = JSON.stringify(value);
    if (sigs[key] === s) return false;
    sigs[key] = s;
    return true;
  }

  function human(s) { return String(s || '').replace(/_/g, ' '); }
  function money(cents) { return '$' + (cents / 100).toFixed(2); }

  function parseTs(s) {
    if (!s) return NaN;
    var t = String(s);
    // Naive timestamps from the API are UTC.
    if (/^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(t) && !/(Z|[+-]\d\d:?\d\d)$/.test(t)) t = t.replace(' ', 'T') + 'Z';
    return Date.parse(t);
  }
  function rel(s) {
    var ms = Date.now() - parseTs(s);
    if (isNaN(ms)) return '';
    var sec = Math.round(ms / 1000);
    if (sec < 10) return 'just now';
    if (sec < 60) return sec + 's ago';
    var min = Math.round(sec / 60);
    if (min < 60) return min + ' min ago';
    var hr = Math.round(min / 60);
    if (hr < 24) return hr + ' hr ago';
    var d = Math.round(hr / 24);
    if (d < 7) return d + 'd ago';
    return new Date(parseTs(s)).toLocaleDateString();
  }
  function timeEl(iso) {
    if (!iso) return null;
    var t = parseTs(iso);
    return h('time', {
      'data-ts': iso,
      datetime: isNaN(t) ? null : new Date(t).toISOString(),
      title: isNaN(t) ? null : new Date(t).toLocaleString(),
      text: rel(iso),
    });
  }
  function refreshTimes() {
    var nodes = document.querySelectorAll('time[data-ts]');
    for (var i = 0; i < nodes.length; i++) {
      var next = rel(nodes[i].getAttribute('data-ts'));
      if (nodes[i].textContent !== next) nodes[i].textContent = next;
    }
  }

  function safeHref(u) {
    try {
      var x = new URL(u, window.location.href);
      return x.protocol === 'https:' || x.protocol === 'http:' ? x.href : null;
    } catch (e) { return null; }
  }

  function chip(text, kind, title) {
    return h('span', { class: 'chip' + (kind ? ' chip-' + kind : ''), text: text, title: title || null });
  }
  function aiChip(title) { return chip('AI-assisted', 'ai', title || 'A model helped make this decision. Rules still have the final say.'); }

  var TASK_KIND = { pending_approval: 'amber', approved: 'violet', running: 'violet', needs_takeover: 'amber', completed: 'ok', failed: 'bad', denied: 'bad', cancelled: '' };
  var BT_KIND = { queued: '', starting: 'violet', running: 'violet', needs_takeover: 'amber', succeeded: 'ok', failed: 'bad', cancelled: '', unavailable: 'bad' };

  /* ---------------------------------------------------------------- API */

  function ApiError(message, status, data) {
    this.message = message; this.status = status; this.data = data;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function detailOf(data) {
    if (!data) return '';
    var d = data.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map(function (x) { return x && x.msg ? x.msg : ''; }).filter(Boolean).join('. ');
    }
    return '';
  }

  function api(path, opts) {
    opts = opts || {};
    var headers = { Accept: 'application/json' };
    if (state.handle) headers['X-Uhlexa-User'] = state.handle;
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    if (opts.headers) Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; });
    return fetch(path, {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    }).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        if (!res.ok) throw new ApiError(detailOf(data) || 'Request failed (' + res.status + ')', res.status, data);
        return data;
      });
    }, function () {
      throw new ApiError('Can\u2019t reach the Uhlexa server.', 0, null);
    });
  }

  /* ---------------------------------------------------------------- toast */

  function toast(message, kind) {
    var node = h('div', { class: 'toast ' + (kind || 'info'), role: kind === 'error' ? 'alert' : null, text: message });
    el.toasts.appendChild(node);
    while (el.toasts.children.length > 3) el.toasts.removeChild(el.toasts.firstChild);
    setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, kind === 'error' ? 8000 : 4500);
  }

  /* ---------------------------------------------------------------- derived data */

  function me() {
    for (var i = 0; i < state.users.length; i++) if (state.users[i].user.handle === state.handle) return state.users[i];
    return null;
  }
  function myRole() { var m = me(); return m ? m.role : (state.board && state.board.viewer ? state.board.viewer.role : ''); }
  function myUserId() { var m = me(); return m ? m.user.id : (state.board && state.board.viewer ? state.board.viewer.user_id : null); }
  function memberName(userId) {
    var members = state.board && state.board.members ? state.board.members : [];
    for (var i = 0; i < members.length; i++) if (members[i].user_id === userId) return members[i].display_name;
    return null;
  }
  function taskById(id) {
    var tasks = state.board ? state.board.tasks : [];
    for (var i = 0; i < tasks.length; i++) if (tasks[i].id === id) return tasks[i];
    return null;
  }
  function browserTasks() {
    if (!state.board) return [];
    return state.board.browser_tasks.map(function (bt) {
      var d = state.btDetail[bt.id];
      return d && parseTs(d.updated_at) >= parseTs(bt.updated_at) ? d : bt;
    });
  }
  function isBusy(key) { return !!state.busy[key]; }

  function withBusy(key, fn) {
    if (state.busy[key]) return Promise.resolve();
    state.busy[key] = true;
    renderAll();
    return Promise.resolve().then(fn).catch(function (e) {
      toast(e && e.message ? e.message : 'Something went wrong.', 'error');
    }).then(function () {
      delete state.busy[key];
      return refresh();
    });
  }

  /* ---------------------------------------------------------------- polling */

  function refresh() {
    if (polling) { again = true; return Promise.resolve(); }
    polling = true;
    clearTimeout(timer);
    var epoch = state.epoch;
    var convId = state.convId;
    var wantProviders = !state.providers || state.providerTick % 5 === 0;
    state.providerTick++;

    var calls = [
      api('/api/board'),
      api('/api/conversations'),
      api('/api/activity?limit=30'),
      convId ? api('/api/conversations/' + encodeURIComponent(convId)) : Promise.resolve(null),
      wantProviders ? api('/api/providers') : Promise.resolve(null),
    ].map(function (p) { return p.then(function (v) { return { ok: true, v: v }; }, function (e) { return { ok: false, e: e }; }); });

    return Promise.all(calls).then(function (r) {
      if (epoch !== state.epoch) return;
      var board = r[0];
      if (!board.ok) {
        state.online = false;
        state.loadError = board.e.message;
        if (board.e.status === 401 && state.handle) loginQuiet();
        return;
      }
      state.online = true;
      state.loadError = '';
      state.board = board.v;
      if (r[1].ok) state.conversations = r[1].v || [];
      if (r[2].ok) state.activity = r[2].v || [];
      if (r[4].ok && r[4].v) state.providers = r[4].v;
      else if (!state.providers && board.v.providers) state.providers = board.v.providers;
      if (convId && convId === state.convId) {
        if (r[3].ok) state.conv = r[3].v;
        else if (r[3].e.status === 404) { state.convId = null; state.conv = null; state.routing = null; }
      }
      var active = browserTasks().filter(function (bt) { return ACTIVE_BT.indexOf(bt.status) >= 0; });
      return Promise.all(active.map(function (bt) {
        return api('/api/browser-tasks/' + encodeURIComponent(bt.id)).then(function (d) {
          if (epoch === state.epoch) state.btDetail[bt.id] = d;
        }, function () { /* the board copy is still shown */ });
      }));
    }).then(function () {
      if (epoch !== state.epoch) return;
      renderAll();
    }).catch(function () {
      state.online = false;
      try { renderAll(); } catch (e) { /* keep polling */ }
    }).then(finish, finish);

    function finish() {
      polling = false;
      if (again) { again = false; return refresh(); }
      timer = setTimeout(refresh, POLL_MS);
    }
  }

  function loginQuiet() {
    return api('/demo/login', { method: 'POST', body: { handle: state.handle } }).catch(function () { /* header auth still applies */ });
  }

  /* ---------------------------------------------------------------- rendering */

  function renderAll() {
    renderHeader();
    renderThreads();
    renderTranscript();
    renderComposer();
    renderApprovals();
    renderBrowser();
    renderTasks();
    renderActivity();
    refreshTimes();
  }

  function renderHeader() {
    var m = me();
    var hh = state.board ? state.board.household : (m ? m.household : null);
    var online = state.online;
    if (changed('hdr', [hh && hh.name, state.users, state.handle, online, state.loadError, state.resetArmed, isBusy('reset')])) {
      el.household.textContent = hh ? hh.name : '';

      fill(el.select, state.users.map(function (u) {
        return h('option', { value: u.user.handle, text: u.user.display_name + ' (' + u.role + ')' });
      }));
      if (!state.users.length) fill(el.select, h('option', { text: 'No demo people found' }));
      el.select.disabled = !state.users.length;
      if (state.handle) el.select.value = state.handle;

      if (m) { el.role.hidden = false; el.role.textContent = m.role + ' with ' + m.agent.name; }
      else el.role.hidden = true;

      el.reset.textContent = isBusy('reset') ? 'Resetting' : (state.resetArmed ? 'Click again to reset' : 'Reset demo');
      el.reset.classList.toggle('is-armed', state.resetArmed);
      el.reset.disabled = isBusy('reset');

      el.conn.hidden = online;
      if (!online) el.conn.textContent = (state.loadError || 'Can\u2019t reach Uhlexa.') + ' Retrying.';
    }

    var pending = state.board ? state.board.approvals.pending : [];
    var mine = pending.filter(function (a) { return a.can_decide; }).length;
    if (changed('wait', [pending.length, mine])) {
      el.waiting.hidden = !pending.length;
      el.waiting.textContent = mine ? mine + ' waiting for you' : pending.length + ' waiting for a grown-up';
      el.main.classList.toggle('has-pending', pending.length > 0);
    }

    if (changed('prov', [state.providers, state.providerOpen])) {
      var names = state.providers ? Object.keys(state.providers) : [];
      names.sort(function (a, b) {
        var ia = PROVIDER_ORDER.indexOf(a), ib = PROVIDER_ORDER.indexOf(b);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
      });
      fill(el.providers, names.map(function (name) {
        var p = state.providers[name];
        var label = PROVIDER_LABELS[name] || name;
        var text = p.status;
        if (name === 'subq' && p.status === 'ok') text = 'ok, AI-assisted';
        return h('li', null, h('button', {
          type: 'button', class: 'prov', 'data-state': p.status, title: p.detail || null,
          'aria-expanded': state.providerOpen === name ? 'true' : 'false',
          onclick: function () { state.providerOpen = state.providerOpen === name ? null : name; renderHeader(); },
        }, h('span', { class: 'dot', 'aria-hidden': 'true' }), label, h('span', { class: 'state', text: text })));
      }));
      var open = state.providerOpen && state.providers ? state.providers[state.providerOpen] : null;
      el.providerDetail.hidden = !open;
      if (open) {
        el.providerDetail.textContent = (PROVIDER_LABELS[state.providerOpen] || state.providerOpen) + ' (' + state.providerOpen + '): ' +
          open.status + '. ' + (open.detail || (open.enabled ? '' : 'Turned off in this environment.'));
      }
    }
  }

  function renderThreads() {
    if (changed('threads', [state.conversations, state.convId])) {
      var items = [h('li', null, h('button', {
        type: 'button', class: 'thread-btn thread-new', 'aria-current': state.convId ? 'false' : 'true',
        onclick: function () { selectThread(null); },
      }, h('span', { class: 'thread-title', text: 'New request' }), h('span', { class: 'thread-meta', text: 'Let Uhlexa find the right thread' })))];
      state.conversations.forEach(function (c) {
        items.push(h('li', null, h('button', {
          type: 'button', class: 'thread-btn', 'aria-current': c.id === state.convId ? 'true' : 'false',
          onclick: function () { selectThread(c.id); },
        },
        h('span', { class: 'thread-title', text: c.title }),
        h('span', { class: 'thread-meta' }, timeEl(c.last_message_at || c.created_at), c.resumed_by && c.resumed_by.length ? chip('resumed', 'violet') : null))));
      });
      fill(el.threads, items);
    }
  }

  function selectThread(id) {
    state.convId = id;
    state.conv = null;
    state.routing = null;
    delete sigs.transcript; delete sigs.threads;
    renderAll();
    refresh();
  }

  function nearBottom(node) { return node.scrollHeight - node.scrollTop - node.clientHeight < 80; }

  function renderTranscript() {
    var conv = state.conv;
    var count = conv && conv.messages ? conv.messages.length : 0;
    var key = [state.convId, conv, state.routing, state.sending, state.online && !!state.board, state.loadError];
    if (!changed('transcript', key)) return;
    var stick = nearBottom(el.transcript) || sigs.lastCount !== count || sigs.lastConv !== state.convId;
    var uid = myUserId();
    var kids = [];

    if (!state.convId) {
      kids.push(h('p', { class: 'empty' },
        h('strong', { text: 'Start with a request. ' }),
        'Your Uhlexa checks the household\u2019s open threads first and resumes one when it fits, so nobody has to repeat themselves.'));
      if (state.routing) kids.push(routingNote(state.routing));
    } else if (!conv) {
      kids.push(state.loadError ? h('p', { class: 'error-line', text: 'Couldn\u2019t load this thread: ' + state.loadError }) : h('p', { class: 'empty', text: 'Loading the thread' }));
    } else {
      kids.push(threadHead(conv));
      if (state.routing && state.routing.convId === conv.id) kids.push(routingNote(state.routing));
      if (!conv.messages.length) kids.push(h('p', { class: 'empty', text: 'No messages yet. Say something below.' }));
      conv.messages.forEach(function (m) {
        var mine = m.sender_type === 'user' && m.sender_user_id === uid;
        var cls = m.sender_type === 'system' ? 'msg msg-system' : (mine ? 'msg msg-mine' : 'msg msg-theirs');
        kids.push(h('div', { class: cls },
          h('div', { class: 'msg-meta' },
            h('span', { text: mine ? 'You' : m.sender_name }),
            timeEl(m.created_at),
            m.task_id ? chip('task created', 'violet') : null),
          h('div', { class: 'msg-body', text: m.body })));
      });
    }
    if (state.sending) kids.push(h('p', { class: 'working', text: 'Uhlexa is working on it' }));
    fill(el.transcript, kids);
    sigs.lastCount = count; sigs.lastConv = state.convId;
    if (stick) el.transcript.scrollTop = el.transcript.scrollHeight;
  }

  function threadHead(conv) {
    var people = conv.participants.map(function (p) { return p.name; }).join(', ');
    var parts = [
      h('h3', { text: conv.title }),
      h('p', { class: 'thread-people', text: 'With ' + people }),
      h('div', { class: 'chip-row' },
        chip(conv.transcript_label, conv.transcript_provider === 'local' ? '' : 'ok'),
        chip(conv.visibility === 'private' ? 'Private' : 'Household', ''),
        conv.status === 'archived' ? chip('Archived', 'amber') : null),
    ];
    if (conv.resumed_by && conv.resumed_by.length) {
      var from = conv.created_by_agent_name ? ' from ' + conv.created_by_agent_name : '';
      parts.push(h('div', { class: 'resume-banner', text: conv.resumed_by.join(' and ') + ' resumed this thread' + from }));
    }
    var ts = conv.thread_state || {};
    var items = Array.isArray(ts.items) ? ts.items.filter(function (x) { return typeof x === 'string'; }) : [];
    var questions = Array.isArray(ts.open_questions) ? ts.open_questions.filter(function (x) { return typeof x === 'string'; }) : [];
    if (ts.summary || items.length || questions.length) {
      parts.push(h('details', { class: 'memory' },
        h('summary', { text: 'What Uhlexa remembers (version ' + conv.thread_state_version + ')' }),
        ts.summary ? h('p', { text: String(ts.summary) }) : null,
        items.length ? h('ul', null, items.map(function (x) { return h('li', { text: x }); })) : null,
        questions.length ? h('p', { text: 'Still open: ' + questions.join('; ') }) : null));
    }
    return h('div', { class: 'thread-head' }, parts);
  }

  function routingNote(r) {
    var verb = r.action === 'resumed' ? 'Resumed an existing thread' : (r.action === 'continued' ? 'Continued this thread' : 'Started a new thread');
    var d = r.decision;
    var conf = d && typeof d.confidence === 'number' ? ' (' + Math.round(d.confidence * 100) + '% sure)' : '';
    return h('div', { class: 'routing-note' },
      h('span', { text: verb }),
      d && d.ai_assisted ? aiChip(d.detail || null) : chip('Rules only', '', d && d.detail ? d.detail : 'No model decision was used for this routing.'),
      conf ? h('span', { text: conf }) : null);
  }

  function renderComposer() {
    if (!el.chips.firstChild) {
      QUICK_PROMPTS.forEach(function (text) {
        el.chips.appendChild(h('button', { type: 'button', class: 'chip-btn', text: text, onclick: function () { send(text); } }));
      });
    }
    var dis = state.sending || !state.board;
    el.send.disabled = dis;
    el.send.textContent = state.sending ? 'Sending' : 'Send';
    var btns = el.chips.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) btns[i].disabled = dis;
    var a = me() ? me().agent.name : 'Uhlexa';
    var ph = state.convId ? 'Reply in this thread' : 'Ask ' + a + ' something';
    if (el.msg.placeholder !== ph) el.msg.placeholder = ph;
  }

  function send(text) {
    text = String(text || '').trim();
    if (!text || state.sending) return Promise.resolve();
    var epoch = state.epoch;
    state.sending = true;
    renderAll();
    return api('/api/conversations/messages', { method: 'POST', body: { text: text, conversation_id: state.convId || undefined } })
      .then(function (res) {
        if (epoch !== state.epoch) return;
        el.msg.value = '';
        state.convId = res.conversation.id;
        state.routing = { convId: res.conversation.id, action: res.routing.action, decision: res.routing.decision };
        delete sigs.transcript;
        if (res.approval && res.approval.status === 'pending') {
          toast(res.approval.can_decide ? 'Approval needed. Review the card before Uhlexa continues.' : 'Waiting for a grown-up to approve this.', 'info');
          if (window.matchMedia && window.matchMedia('(max-width: 999px)').matches) {
            var target = document.getElementById('approvals');
            if (target && target.scrollIntoView) target.scrollIntoView({ block: 'start' });
          }
        }
      })
      .catch(function (e) { toast(e.message, 'error'); })
      .then(function () { state.sending = false; renderAll(); return refresh(); });
  }

  /* approvals: the hero */

  function renderApprovals() {
    var board = state.board;
    var key = [board && board.approvals, myRole(), state.busy, state.loadError, !!board];
    if (!changed('approvals', key)) return;
    if (!board) {
      fill(el.approvals, state.loadError ? h('p', { class: 'error-line', text: 'Couldn\u2019t load approvals: ' + state.loadError }) : h('p', { class: 'empty', text: 'Loading' }));
      return;
    }
    var pending = board.approvals.pending;
    var recent = board.approvals.recent;
    var firstLoad = state.seenApprovals === null;
    var seen = state.seenApprovals || {};
    var kids = [];
    if (!pending.length) kids.push(h('p', { class: 'empty' }, h('strong', { text: 'Nothing waiting. ' }), 'Requests that need a grown-up\u2019s okay show up here.'));
    pending.forEach(function (a) { kids.push(approvalCard(a, !firstLoad && !seen[a.id])); });
    if (recent.length) {
      kids.push(h('h3', { class: 'subhead', text: 'Recently decided' }));
      recent.slice(0, 4).forEach(function (a) {
        kids.push(h('div', { class: 'decided' },
          h('div', { class: 'decided-top' },
            h('span', { class: 'decided-title', text: a.task.title }),
            chip(a.status, a.status === 'approved' ? 'ok' : (a.status === 'denied' ? 'bad' : ''))),
          h('span', { class: 'decided-sub' }, a.decided_by_name ? a.status + ' by ' + a.decided_by_name + ' ' : a.status + ' ', timeEl(a.decided_at))));
      });
    }
    fill(el.approvals, kids);
    el.approvalsTitle.textContent = 'Approvals';
    if (pending.length) el.approvalsTitle.appendChild(h('span', { class: 'count', text: pending.length + ' waiting' }));
    var next = {};
    pending.forEach(function (a) { next[a.id] = true; });
    recent.forEach(function (a) { next[a.id] = true; });
    state.seenApprovals = next;
  }

  function approvalCard(a, isNew) {
    var t = a.task;
    var bKey = 'appr:' + a.id;
    var facts = [];
    if (t.items && t.items.length) facts.push(h('ul', { class: 'item-list' }, t.items.map(function (x) { return h('li', { text: x }); })));
    if (t.amount_cents !== null && t.amount_cents !== undefined) facts.push(h('p', { text: 'Amount: ' + money(t.amount_cents) }));
    if (t.will_use_browser) facts.push(h('p', { text: 'Next step: Uhlexa opens a real browser on ' + (a.requested_for_name || 'the requester') + '\u2019s cloud computer.' }));

    var action;
    if (a.can_decide) {
      action = h('div', { class: 'approval-actions' },
        h('button', { type: 'button', class: 'btn btn-primary btn-lg', disabled: isBusy(bKey), onclick: function () { decide(a, 'approve'); }, text: isBusy(bKey) ? 'Working' : 'Approve' }),
        h('button', { type: 'button', class: 'btn btn-ghost btn-lg', disabled: isBusy(bKey), onclick: function () { decide(a, 'deny'); }, text: 'Deny' }));
    } else {
      var child = myRole() === 'child';
      action = h('div', { class: 'approval-wait' }, 'Waiting for a grown-up',
        !child && a.can_decide_reason ? h('small', { text: a.can_decide_reason }) : null);
    }
    return h('article', { class: 'approval' + (isNew ? ' is-new' : ''), 'aria-label': 'Approval needed: ' + t.title },
      h('div', { class: 'approval-kicker' },
        h('span', { class: 'approval-who', text: 'Needs approval for ' + (a.requested_for_name || 'a family member') }),
        h('span', { class: 'chip-row' }, a.ai_assisted ? aiChip('A model helped judge this request. The rule below made the final call.') : null, timeEl(a.created_at))),
      h('h3', { class: 'approval-title', text: t.title }),
      h('div', { class: 'approval-why' }, a.reason, h('small', { text: 'Rule: ' + a.rule_id })),
      facts.length ? h('div', { class: 'approval-facts' }, facts) : null,
      action);
  }

  function decide(a, verb) {
    return withBusy('appr:' + a.id, function () {
      return api('/api/approvals/' + encodeURIComponent(a.id) + '/' + verb, { method: 'POST', body: {} }).then(function () {
        toast(verb === 'approve' ? 'Approved. Uhlexa can go ahead.' : 'Denied. Nothing will run.', verb === 'approve' ? 'ok' : 'info');
      });
    });
  }

  /* browser tasks */

  function renderBrowser() {
    var list = browserTasks();
    if (!changed('browser', [list, state.busy, myUserId(), !!state.board, state.loadError])) return;
    if (!state.board) {
      fill(el.browser, state.loadError ? h('p', { class: 'error-line', text: 'Couldn\u2019t load browser tasks: ' + state.loadError }) : h('p', { class: 'empty', text: 'Loading' }));
      return;
    }
    if (!list.length) {
      fill(el.browser, h('p', { class: 'empty' }, h('strong', { text: 'No browser work yet. ' }), 'Once a task is approved you can run it on a real cloud browser.'));
      return;
    }
    fill(el.browser, list.map(browserCard));
  }

  function browserCard(bt) {
    var task = taskById(bt.task_id);
    var bKey = 'bt:' + bt.id;
    var steps = bt.steps || [];
    var shown = steps.slice(-6);
    var parts = [];

    parts.push(h('div', { class: 'card-head' },
      h('div', null,
        h('div', { class: 'card-title', text: task ? task.title : human(bt.task_type) }),
        h('div', { class: 'bt-url', text: bt.requested_url })),
      h('span', { class: 'chip-row' }, chip(human(bt.status), BT_KIND[bt.status] || ''))));

    if (bt.status === 'succeeded') {
      if (bt.verified) parts.push(h('div', { class: 'chip-row' }, chip('Verified', 'ok', 'The screenshot shows the expected result.'), aiChip('A vision model checked the final screenshot.')));
      else parts.push(h('div', { class: 'chip-row' }, chip('Not verified', 'amber', 'The run finished but the result could not be confirmed from the screenshot.')));
    }
    if (bt.result && typeof bt.result.summary === 'string' && bt.result.summary) parts.push(h('p', { class: 'card-meta', text: bt.result.summary }));

    if (bt.status === 'needs_takeover' || (bt.requires_takeover && bt.takeover_url)) {
      var href = bt.takeover_url ? safeHref(bt.takeover_url) : null;
      parts.push(h('div', { class: 'takeover' },
        h('strong', { text: 'Uhlexa needs you to take over the browser' }),
        h('p', { text: bt.is_owner ? 'Finish the step it can\u2019t do itself, then hand control back.' : 'Only the person this browser belongs to can take over.' }),
        h('div', { class: 'card-actions' },
          href ? h('a', { class: 'btn btn-amber btn-sm', href: href, target: '_blank', rel: 'noopener noreferrer', text: 'Open the browser' }) : null,
          bt.is_owner ? h('button', { type: 'button', class: 'btn btn-primary btn-sm', disabled: isBusy(bKey), onclick: function () { resumeBt(bt); }, text: isBusy(bKey) ? 'Resuming' : 'I\u2019m done, resume' }) : null),
        bt.is_owner && !href ? h('p', { text: 'The takeover link isn\u2019t available yet. It will appear here.' }) : null));
    }

    if (shown.length) {
      var lis = [];
      if (steps.length > shown.length) lis.push(h('li', { class: 'more', text: (steps.length - shown.length) + ' earlier steps' }));
      shown.forEach(function (s) { lis.push(h('li', { class: s.ok ? '' : 'is-bad', text: human(s.action) + (s.note ? ': ' + s.note : '') })); });
      parts.push(h('ul', { class: 'steps', 'aria-label': 'Step log' }, lis));
    } else if (ACTIVE_BT.indexOf(bt.status) >= 0) {
      parts.push(h('p', { class: 'card-meta', text: 'Waiting for the first step' }));
    }

    if (bt.last_screenshot_ref) parts.push(screenshot(bt));
    if (bt.last_error) parts.push(h('p', { class: 'last-error', text: bt.last_error }));
    parts.push(h('div', { class: 'card-meta' }, 'Updated ', timeEl(bt.updated_at)));
    return h('article', { class: 'card bt', 'data-status': bt.status }, parts);
  }

  function screenshot(bt) {
    var btn = h('button', { type: 'button', class: 'shot-btn', 'aria-label': 'Latest browser screenshot, click to enlarge' });
    var img = h('img', {
      alt: 'Latest browser screenshot', loading: 'lazy',
      src: '/api/browser-tasks/' + encodeURIComponent(bt.id) + '/screenshot?v=' + encodeURIComponent(bt.updated_at || ''),
    });
    img.addEventListener('error', function () { btn.replaceWith(h('p', { class: 'shot-missing', text: 'Screenshot isn\u2019t available.' })); });
    btn.addEventListener('click', function () { btn.classList.toggle('is-zoom'); });
    btn.appendChild(img);
    return btn;
  }

  function resumeBt(bt) {
    return withBusy('bt:' + bt.id, function () {
      return api('/api/browser-tasks/' + encodeURIComponent(bt.id) + '/resume', { method: 'POST', body: {} }).then(function () {
        delete state.btDetail[bt.id];
        toast('Uhlexa has the browser back.', 'ok');
      });
    });
  }

  function runBrowserTask(task) {
    return withBusy('run:' + task.id, function () {
      return api('/api/browser-tasks', { method: 'POST', body: { task_id: task.id } }).then(function () {
        toast('Browser task started.', 'ok');
      });
    });
  }

  /* tasks */

  function renderTasks() {
    var tasks = state.board ? state.board.tasks : null;
    var bts = state.board ? state.board.browser_tasks.map(function (b) { return b.task_id; }) : [];
    if (!changed('tasks', [tasks, bts, state.busy, state.loadError])) return;
    if (!tasks) {
      fill(el.tasks, state.loadError ? h('p', { class: 'error-line', text: 'Couldn\u2019t load tasks: ' + state.loadError }) : h('p', { class: 'empty', text: 'Loading' }));
      return;
    }
    el.tasksTitle.textContent = 'Tasks';
    if (tasks.length) el.tasksTitle.appendChild(h('span', { class: 'count', text: String(tasks.length) }));
    if (!tasks.length) {
      fill(el.tasks, h('p', { class: 'empty' }, h('strong', { text: 'No tasks yet. ' }), 'Ask Uhlexa for something and it shows up here.'));
      return;
    }
    fill(el.tasks, tasks.map(function (t) {
      var items = t.metadata && Array.isArray(t.metadata.items) ? t.metadata.items.filter(function (x) { return typeof x === 'string'; }) : [];
      var canRun = t.status === 'approved' && bts.indexOf(t.id) < 0;
      var by = memberName(t.created_by_user_id);
      return h('article', { class: 'card task', 'data-status': t.status },
        h('div', { class: 'card-head' },
          h('div', { class: 'card-title', text: t.title }),
          chip(human(t.status), TASK_KIND[t.status] || '')),
        items.length ? h('ul', { class: 'item-list' }, items.map(function (x) { return h('li', { text: x }); })) : null,
        h('div', { class: 'card-meta' }, human(t.kind), by ? ' for ' + by : '', ', ', timeEl(t.created_at)),
        canRun ? h('div', { class: 'card-actions' }, h('button', {
          type: 'button', class: 'btn btn-primary btn-sm', disabled: isBusy('run:' + t.id),
          onclick: function () { runBrowserTask(t); }, text: isBusy('run:' + t.id) ? 'Starting' : 'Run browser task',
        })) : null);
    }));
  }

  /* activity */

  function renderActivity() {
    if (!changed('activity', [state.activity, !!state.board, state.loadError])) return;
    if (!state.board) {
      fill(el.activity, state.loadError ? h('p', { class: 'error-line', text: 'Couldn\u2019t load activity: ' + state.loadError }) : h('p', { class: 'empty', text: 'Loading' }));
      return;
    }
    var events = state.activity.slice().sort(function (a, b) { return parseTs(b.created_at) - parseTs(a.created_at); });
    if (!events.length) {
      fill(el.activity, h('p', { class: 'empty' }, h('strong', { text: 'Quiet so far. ' }), 'Everything Uhlexa and the family do gets logged here.'));
      return;
    }
    fill(el.activity, h('ol', { class: 'timeline' }, events.map(function (e) {
      return h('li', null,
        h('div', { class: 'tl-summary', text: e.summary }),
        h('div', { class: 'tl-meta' }, timeEl(e.created_at), h('span', { text: human(e.event_type) })));
    })));
  }

  /* ---------------------------------------------------------------- user, reset, boot */

  function resetView() {
    state.epoch++;
    state.board = null; state.conv = null; state.convId = null; state.routing = null; state.conversations = [];
    state.activity = []; state.btDetail = {}; state.busy = {}; state.seenApprovals = null; state.sending = false;
    state.loadError = ''; state.online = true;
    sigs = {};
  }

  function switchUser(handle) {
    resetView();
    state.handle = handle;
    storeSet(USER_KEY, handle);
    renderAll();
    return api('/demo/login', { method: 'POST', body: { handle: handle } })
      .catch(function (e) { toast('Sign-in: ' + e.message, 'error'); })
      .then(function () { return refresh(); });
  }

  function doReset() {
    var guardian = null;
    state.users.forEach(function (u) { if (!guardian && u.role === 'guardian') guardian = u; });
    if (!guardian) { toast('No guardian found to authorize the reset.', 'error'); return; }
    return withBusy('reset', function () {
      return api('/demo/reset', { method: 'POST', headers: { 'X-Uhlexa-User': guardian.user.handle } }).then(function (res) {
        state.users = res && res.users ? res.users : state.users;
        var keep = state.users.some(function (u) { return u.user.handle === state.handle; });
        var handle = keep ? state.handle : (state.users[0] ? state.users[0].user.handle : null);
        resetView();
        state.handle = handle;
        if (handle) storeSet(USER_KEY, handle);
        toast('Demo reset. Everyone is back to the seeded household.', 'ok');
        return api('/demo/login', { method: 'POST', body: { handle: handle } }).catch(function () { /* header auth still applies */ });
      });
    });
  }

  function init() {
    el.select.addEventListener('change', function () { switchUser(el.select.value); });
    el.reset.addEventListener('click', function () {
      if (!state.resetArmed) {
        state.resetArmed = true;
        clearTimeout(resetTimer);
        resetTimer = setTimeout(function () { state.resetArmed = false; renderHeader(); }, 4000);
        renderHeader();
        return;
      }
      state.resetArmed = false;
      clearTimeout(resetTimer);
      doReset();
    });
    el.composer.addEventListener('submit', function (e) { e.preventDefault(); send(el.msg.value); });
    el.msg.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); el.composer.requestSubmit(); }
    });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
    setInterval(refreshTimes, 15000);
    renderAll();
    boot();
  }

  function boot() {
    api('/demo/users').then(function (res) {
      state.users = res.users || [];
      if (!state.users.length) { state.loadError = 'No demo people exist yet. Use Reset demo.'; renderAll(); return; }
      var saved = storeGet(USER_KEY);
      var pick = null;
      state.users.forEach(function (u) { if (u.user.handle === saved) pick = u.user.handle; });
      if (!pick) state.users.forEach(function (u) { if (!pick && u.user.handle === 'alex') pick = u.user.handle; });
      if (!pick) pick = state.users[0].user.handle;
      return switchUser(pick);
    }).catch(function (e) {
      state.online = false;
      state.loadError = e.message;
      renderAll();
      setTimeout(boot, 3000);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();

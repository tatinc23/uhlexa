/* Uhlexa voice pages: the one shared client behind echo.html, app.html and hub.html.
 *
 * - No third-party requests. The only network targets are this origin and the LiveKit URL the server hands back.
 * - Text that comes from data (captions, titles, names, URLs) only ever goes through textContent / setAttribute.
 *   Nothing in this file assigns to innerHTML.
 * - Approvals are taps: Approve/Deny call the existing approvals API with the viewer's cookie. Voice never approves.
 *
 * Public global: UhlexaVoice (also exported with module.exports so computeState can be unit-tested in node).
 */
(function (root) {
  'use strict';

  /* ------------------------------------------------------------------ constants */

  const WAKE_MODES = ['wake_word', 'follow_up', 'always'];
  const MODE_LABELS = {
    wake_word: 'Say Uhlexa every time',
    follow_up: 'Follow-up (default)',
    always: 'Always listening',
  };
  const SURFACES = ['echo', 'app', 'hub'];

  const ATTR_STATE = 'lk.agent.state';
  const ATTR_AWAKE = 'uhlexa.voice.awake';
  const ATTR_AVATAR = 'uhlexa.avatar';
  const TOPIC_TRANSCRIPTION = 'lk.transcription';
  const ATTR_SEGMENT = 'lk.segment_id';
  const ATTR_FINAL = 'lk.transcription_final';

  const BOARD_POLL_MS = 2000;
  const THINKING_FILLER_MS = 2500;
  const AGENT_JOIN_TIMEOUT_MS = 20000;
  const AVATAR_GRACE_MS = 12000;
  const FACE_UNAVAILABLE = 'Face unavailable — voice only';
  const FACE_STARTING = 'Face starting…';

  // Spec 22.6 states. `caption` is the fixed line for that state; speaking/listening show live transcript text instead.
  const STATE_META = {
    offline: { pill: 'Offline', caption: 'Voice is offline' },
    speaking: { pill: 'Speaking', caption: '' },
    thinking: { pill: 'Thinking', caption: 'One moment…' },
    listening: { pill: 'Listening', caption: '' },
    needs_your_hands: { pill: 'Needs you', caption: 'I need you to take over' },
    waiting_for_you: { pill: 'Waiting for you', caption: 'Waiting for your OK' },
    working_on_computer: { pill: 'Working', caption: 'Working on the family computer' },
    asleep: { pill: 'Asleep', caption: 'Say Uhlexa' },
    idle: { pill: 'Ready', caption: '' },
  };

  /* ------------------------------------------------------------------ small helpers */

  function asArray(x) {
    return Array.isArray(x) ? x : [];
  }

  function safe(fn) {
    if (typeof fn !== 'function') return undefined;
    try {
      return fn.apply(null, Array.prototype.slice.call(arguments, 1));
    } catch (err) {
      if (root.console) root.console.error(err);
      return undefined;
    }
  }

  class UhlexaError extends Error {
    constructor(message, status, code) {
      super(message);
      this.name = 'UhlexaError';
      this.status = status || 0;
      this.code = code || '';
    }
  }

  function detailOf(data, status) {
    if (data && typeof data.detail === 'string' && data.detail) return data.detail;
    if (data && Array.isArray(data.detail) && data.detail.length) {
      return data.detail.map((d) => (d && d.msg) || '').filter(Boolean).join('; ') || 'That request was not valid.';
    }
    if (data && typeof data.error === 'string' && data.error) return data.error;
    if (status === 401) return 'Please sign in first.';
    if (status === 503) return 'Voice is offline.';
    return 'Request failed (' + status + ').';
  }

  async function api(method, path, body) {
    const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      throw new UhlexaError("Can't reach the Uhlexa server.", 0, 'network');
    }
    let data = null;
    try {
      const text = await res.text();
      data = text ? JSON.parse(text) : null;
    } catch (err) {
      data = null;
    }
    if (!res.ok) throw new UhlexaError(detailOf(data, res.status), res.status, data && data.code);
    return data;
  }

  /* ------------------------------------------------------------------ server API wrappers */

  async function loadUsers() {
    const data = await api('GET', '/demo/users');
    // /demo/users returns PrincipalOut rows: {user: {handle, display_name}, role, household, agent}
    return asArray(data && data.users).map((p) => ({
      handle: (p.user && p.user.handle) || p.handle,
      display_name: (p.user && p.user.display_name) || p.display_name || (p.user && p.user.handle) || p.handle,
      role: p.role,
      agent: p.agent,
    })).filter((u) => u.handle);
  }

  async function login(handle) {
    poller.latest = null; // a different person must never see the previous person's board
    const principal = await api('POST', '/demo/login', { handle });
    S.handle = handle;
    return principal;
  }

  async function fetchStatus() {
    try {
      const data = await api('GET', '/api/voice/status');
      const voice = (data && data.voice) || {};
      const avatar = (data && data.avatar) || {};
      return {
        voice: { status: voice.status || 'unavailable', detail: voice.detail || '' },
        avatar: { status: avatar.status || 'unavailable', detail: avatar.detail || '' },
      };
    } catch (err) {
      return {
        voice: { status: 'unavailable', detail: err.message },
        avatar: { status: 'unavailable', detail: '' },
      };
    }
  }

  function getSettings() {
    return api('GET', '/api/voice/settings');
  }

  async function decideApproval(id, verb, note) {
    if (verb !== 'approve' && verb !== 'deny') throw new UhlexaError('Unknown decision.', 0, 'bad_verb');
    const body = note ? { note } : {};
    const result = await api('POST', '/api/approvals/' + encodeURIComponent(id) + '/' + verb, body);
    pollBoard();
    return result;
  }

  /* ------------------------------------------------------------------ board polling */

  const poller = { subs: new Set(), timer: null, inflight: null, latest: null };

  function pageVisible() {
    return typeof document === 'undefined' || document.visibilityState !== 'hidden';
  }

  function onVisibility() {
    if (pageVisible() && poller.subs.size) pollBoard();
  }

  function pollBoard() {
    if (poller.inflight) return poller.inflight;
    poller.inflight = api('GET', '/api/board')
      .then(
        (data) => {
          poller.latest = data;
          poller.subs.forEach((cb) => safe(cb, data, null));
          return data;
        },
        (err) => {
          poller.subs.forEach((cb) => safe(cb, null, err));
          return null;
        }
      )
      .then((data) => {
        poller.inflight = null;
        return data;
      });
    return poller.inflight;
  }

  /** Calls cb(board, null) on every successful poll and cb(null, error) on a failed one. Returns a stop function. */
  function watchBoard(cb) {
    poller.subs.add(cb);
    if (poller.latest) safe(cb, poller.latest, null);
    if (!poller.timer) {
      poller.timer = setInterval(() => {
        if (pageVisible()) pollBoard();
      }, BOARD_POLL_MS);
      if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility);
    }
    pollBoard();
    return function stop() {
      poller.subs.delete(cb);
      if (!poller.subs.size && poller.timer) {
        clearInterval(poller.timer);
        poller.timer = null;
        if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  }

  /* ------------------------------------------------------------------ state machine (spec 22.6) */

  function viewerUserId(board, viewerHandle) {
    if (!board) return null;
    const viewer = board.viewer || {};
    if (viewer.user_id && (!viewerHandle || viewer.handle === viewerHandle)) return viewer.user_id;
    const hit = asArray(board.members).find((m) => m && m.handle === viewerHandle);
    return hit ? hit.user_id : null;
  }

  function ownsTask(task, userId) {
    if (task.is_owner === true) return true;
    if (task.is_owner === false) return false;
    return !!userId && task.user_id === userId;
  }

  /**
   * Highest first: offline > speaking > thinking > listening (and awake) > needs_your_hands > waiting_for_you >
   * working_on_computer > asleep > idle.
   * `agentState` is LiveKit's lk.agent.state, or the string "offline" when voice is down or the room dropped.
   * `awake` is uhlexa.voice.awake as a boolean (undefined = the worker has not said, treated as awake).
   */
  function computeState(agentState, awake, board, viewerHandle) {
    if (agentState === 'offline') return 'offline';
    if (agentState === 'speaking') return 'speaking';
    if (agentState === 'thinking') return 'thinking';
    if (agentState === 'listening' && awake !== false) return 'listening';

    const b = board || {};
    const userId = viewerUserId(b, viewerHandle);
    const browserTasks = asArray(b.browser_tasks).filter(Boolean);
    if (browserTasks.some((t) => t.status === 'needs_takeover' && ownsTask(t, userId))) return 'needs_your_hands';

    const pending = asArray(b.approvals && b.approvals.pending).filter(Boolean);
    if (pending.some((a) => a.can_decide === true && (a.status === undefined || a.status === 'pending'))) {
      return 'waiting_for_you';
    }

    if (browserTasks.some((t) => t.status === 'starting' || t.status === 'running')) return 'working_on_computer';
    if (awake === false) return 'asleep';
    return 'idle';
  }

  /* ------------------------------------------------------------------ voice session */

  const S = {
    opts: null,
    room: null,
    connected: false,
    leaving: false,
    handle: null,
    sessionInfo: null,
    board: null,
    unsubBoard: null,
    capSource: { heard: null, said: null },
    micOn: false,
    canPlayAudio: true,
    videoTracks: new Map(),
    audioEls: new Map(),
    connectedAt: 0,
    offlineReason: '',
    agentTimedOut: false,
    thinkingSince: 0,
    timers: { think: null, join: null, avatar: null },
    lastStateKey: '',
    lastAvatarKey: '',
    wakeMode: null,
  };

  function viewerHandle() {
    return (S.board && S.board.viewer && S.board.viewer.handle) || S.handle;
  }

  function isHumanIdentity(identity) {
    const local = S.room && S.room.localParticipant ? S.room.localParticipant.identity : '';
    return !!identity && (identity === local || String(identity).indexOf('human-') === 0);
  }

  /** The agent's attributes live on whichever remote participant publishes them (the avatar is another participant). */
  function readAgent() {
    const out = { state: undefined, awake: undefined, avatar: undefined, present: false };
    if (!S.room) return out;
    const AGENT_KIND = root.LivekitClient && root.LivekitClient.ParticipantKind ? root.LivekitClient.ParticipantKind.AGENT : 4;
    S.room.remoteParticipants.forEach((p) => {
      const a = p.attributes || {};
      if (a[ATTR_STATE] || p.kind === AGENT_KIND) out.present = true;
      if (out.state === undefined && a[ATTR_STATE]) out.state = a[ATTR_STATE];
      if (out.awake === undefined && a[ATTR_AWAKE] !== undefined) out.awake = String(a[ATTR_AWAKE]) === 'true';
      if (out.avatar === undefined && a[ATTR_AVATAR]) out.avatar = a[ATTR_AVATAR];
    });
    return out;
  }

  function clearTimer(name) {
    if (S.timers[name]) {
      clearTimeout(S.timers[name]);
      S.timers[name] = null;
    }
  }

  function buildStateView() {
    const agent = S.connected ? readAgent() : { state: undefined, awake: undefined, avatar: undefined, present: false };
    let agentState = agent.state;
    let detail = '';
    if (!S.connected) {
      agentState = 'offline';
      detail = S.offlineReason || 'Not connected.';
    } else if (!agent.present && S.agentTimedOut) {
      agentState = 'offline';
      detail = "Uhlexa's voice worker didn't join. Is it running?";
    } else if (!agent.present) {
      detail = 'Waiting for Uhlexa to join…';
    } else if (agent.state === 'initializing') {
      detail = 'Uhlexa is waking up…';
    }

    const state = computeState(agentState, agent.awake, S.board, viewerHandle());
    const meta = STATE_META[state];
    let caption = meta.caption;
    if (state === 'thinking') {
      if (!S.thinkingSince) {
        S.thinkingSince = Date.now();
        clearTimer('think');
        S.timers.think = setTimeout(() => emitState(), THINKING_FILLER_MS + 20);
      }
      caption = Date.now() - S.thinkingSince >= THINKING_FILLER_MS ? meta.caption : '';
    } else {
      S.thinkingSince = 0;
      clearTimer('think');
    }
    return {
      state,
      pill: meta.pill,
      caption,
      detail,
      agentState: agentState || null,
      awake: agent.awake === undefined ? null : agent.awake,
      connected: S.connected,
      wakeMode: S.wakeMode,
    };
  }

  function emitState(force) {
    if (!S.opts) return;
    const view = buildStateView();
    const key = [view.state, view.caption, view.detail, view.agentState, view.awake, view.connected].join('|');
    if (force || key !== S.lastStateKey) {
      S.lastStateKey = key;
      safe(S.opts.onState, view);
    }
    emitAvatar();
  }

  function emitAvatar() {
    if (!S.opts) return;
    const hub = S.opts.surface === 'hub';
    const agent = S.connected ? readAgent() : { avatar: undefined };
    const hasVideo = S.videoTracks.size > 0;
    const requested = !!(S.sessionInfo && S.sessionInfo.avatar);
    let view = 'ring';
    let notice = '';
    if (hasVideo) {
      view = 'video';
    } else if (hub) {
      const failed = agent.avatar === 'failed' || agent.avatar === 'off';
      if (!S.connected) {
        notice = '';
      } else if (!requested || failed) {
        notice = FACE_UNAVAILABLE;
      } else if (Date.now() - S.connectedAt > AVATAR_GRACE_MS) {
        notice = FACE_UNAVAILABLE;
      } else {
        notice = FACE_STARTING;
        if (!S.timers.avatar) {
          S.timers.avatar = setTimeout(() => {
            S.timers.avatar = null;
            emitAvatar();
          }, Math.max(100, AVATAR_GRACE_MS - (Date.now() - S.connectedAt) + 50));
        }
      }
    }
    const out = { surface: S.opts.surface, attr: agent.avatar || null, hasVideo, requested, view, notice };
    const key = [out.attr, out.hasVideo, out.view, out.notice].join('|');
    if (key !== S.lastAvatarKey) {
      S.lastAvatarKey = key;
      safe(S.opts.onAvatar, out);
    }
  }

  /* ---- captions */

  function acceptCaption(role, source) {
    // Agents 1.5 publishes each transcript twice (legacy data packet + text stream). Lock one source per role so a
    // caption is never shown twice, and fall back to whichever source actually carries that role's text.
    if (!S.capSource[role]) S.capSource[role] = source;
    return S.capSource[role] === source;
  }

  function emitCaption(role, source, id, text, final, identity) {
    if (!S.opts || !acceptCaption(role, source)) return;
    safe(S.opts.onCaption, { id: source + ':' + id, role, text: String(text || ''), final: !!final, speaker: identity || '' });
  }

  function onTranscriptionReceived(segments, participant) {
    const identity = participant ? participant.identity : '';
    const role = isHumanIdentity(identity) ? 'heard' : 'said';
    asArray(segments).forEach((seg) => {
      if (seg && typeof seg.text === 'string') emitCaption(role, 'legacy', seg.id, seg.text, seg.final, identity);
    });
  }

  async function onTranscriptionStream(reader, sender) {
    const info = (reader && reader.info) || {};
    const identity = (sender && sender.identity) || '';
    const role = isHumanIdentity(identity) ? 'heard' : 'said';
    const id = (info.attributes && info.attributes[ATTR_SEGMENT]) || info.id || String(Date.now());
    let text = '';
    try {
      for await (const chunk of reader) {
        text += chunk;
        emitCaption(role, 'stream', id, text, false, identity);
      }
    } catch (err) {
      // an interrupted stream still carries what was said so far
    }
    const final = String((info.attributes && info.attributes[ATTR_FINAL]) || '') === 'true';
    emitCaption(role, 'stream', id, text, final, identity);
  }

  /* ---- media */

  function audioHost() {
    let host = document.getElementById('uhlexa-audio');
    if (!host) {
      host = document.createElement('div');
      host.id = 'uhlexa-audio';
      host.hidden = true;
      document.body.appendChild(host);
    }
    return host;
  }

  function attachTrack(track) {
    const LK = root.LivekitClient;
    if (track.kind === LK.Track.Kind.Audio) {
      const node = track.attach();
      audioHost().appendChild(node);
      S.audioEls.set(track.sid, node);
    } else if (track.kind === LK.Track.Kind.Video) {
      const video = S.opts && S.opts.videoEl;
      if (video) {
        video.muted = true;
        video.playsInline = true;
        track.attach(video);
        S.videoTracks.set(track.sid, track);
      }
    }
  }

  function detachTrack(track) {
    const LK = root.LivekitClient;
    if (track.kind === LK.Track.Kind.Audio) {
      track.detach().forEach((n) => n.remove());
      S.audioEls.delete(track.sid);
    } else if (track.kind === LK.Track.Kind.Video) {
      const video = S.opts && S.opts.videoEl;
      if (video) track.detach(video);
      S.videoTracks.delete(track.sid);
    }
  }

  function detachAll() {
    const tracks = [];
    if (S.room) {
      S.room.remoteParticipants.forEach((p) => {
        p.trackPublications.forEach((pub) => {
          if (pub.track) tracks.push(pub.track);
        });
      });
    }
    tracks.forEach((t) => {
      try {
        detachTrack(t);
      } catch (err) {
        /* already gone */
      }
    });
    S.audioEls.forEach((n) => n.remove());
    S.audioEls.clear();
    S.videoTracks.clear();
  }

  function syncMic() {
    const on = !!(S.room && S.room.localParticipant && S.room.localParticipant.isMicrophoneEnabled);
    if (on !== S.micOn) {
      S.micOn = on;
      if (S.opts) safe(S.opts.onMic, on);
    }
  }

  function syncAudioPlayback() {
    const ok = !S.room || S.room.canPlaybackAudio !== false;
    if (ok !== S.canPlayAudio) {
      S.canPlayAudio = ok;
      if (S.opts) safe(S.opts.onAudio, ok);
    }
  }

  /* ---- room events */

  function bindRoom(room) {
    const LK = root.LivekitClient;
    const E = LK.RoomEvent;
    const recompute = () => {
      const agent = readAgent();
      if (agent.present) {
        S.agentTimedOut = false;
        clearTimer('join');
      }
      emitState();
    };

    room.on(E.ParticipantConnected, recompute);
    room.on(E.ParticipantDisconnected, recompute);
    room.on(E.ParticipantAttributesChanged, recompute);
    room.on(E.TrackSubscribed, (track) => {
      try {
        attachTrack(track);
      } catch (err) {
        safe(root.console && root.console.error, err);
      }
      emitState();
    });
    room.on(E.TrackUnsubscribed, (track) => {
      try {
        detachTrack(track);
      } catch (err) {
        /* ignore */
      }
      emitState();
    });
    room.on(E.TranscriptionReceived, onTranscriptionReceived);
    room.on(E.AudioPlaybackStatusChanged, syncAudioPlayback);
    room.on(E.LocalTrackPublished, syncMic);
    room.on(E.LocalTrackUnpublished, syncMic);
    room.on(E.TrackMuted, syncMic);
    room.on(E.TrackUnmuted, syncMic);
    room.on(E.MediaDevicesError, () => {
      safe(S.opts && S.opts.onNotice, 'The microphone is blocked. Allow it in your browser to talk to Uhlexa.');
    });
    room.on(E.ActiveSpeakersChanged, (speakers) => {
      let level = 0;
      asArray(speakers).forEach((p) => {
        if (p && !isHumanIdentity(p.identity)) level = Math.max(level, Number(p.audioLevel) || 0);
      });
      safe(S.opts && S.opts.onLevel, Math.min(1, level * 1.6));
    });
    room.on(E.ConnectionStateChanged, (state) => safe(S.opts && S.opts.onConnection, state));
    room.on(E.Disconnected, (reason) => {
      if (S.leaving) return;
      S.connected = false;
      const reasons = { 2: 'This account joined from another tab.', 3: 'The voice server shut down.', 4: 'You were removed from the room.', 5: 'The voice room closed.', 10: 'The voice room closed.', 14: 'The connection timed out.' };
      S.offlineReason = reasons[reason] || 'The voice connection dropped.';
      detachAll();
      S.thinkingSince = 0;
      emitState(true);
      safe(S.opts && S.opts.onConnection, 'disconnected');
    });

    try {
      room.registerTextStreamHandler(TOPIC_TRANSCRIPTION, onTranscriptionStream);
    } catch (err) {
      /* already registered for this room, or an older client: legacy transcription events still work */
    }
  }

  /**
   * Join voice. opts: { surface, handle?, videoEl?, onState, onCaption, onBoard, onAvatar, onMic, onAudio, onLevel,
   * onNotice, onConnection }. Rejects with an Error whose message is safe to show to the user.
   */
  async function start(opts) {
    if (!opts || SURFACES.indexOf(opts.surface) < 0) throw new UhlexaError('Unknown voice surface.', 0, 'bad_surface');
    if (S.room || S.unsubBoard) await leave(true);

    S.opts = opts;
    S.leaving = false;
    S.connected = false;
    S.offlineReason = 'Connecting…';
    S.agentTimedOut = false;
    S.capSource = { heard: null, said: null };
    S.lastStateKey = '';
    S.lastAvatarKey = '';
    S.board = poller.latest;
    S.sessionInfo = null;

    // Create the Room and unlock audio now, synchronously inside the click that called start(): browsers only allow
    // audio playback after a user gesture, and the awaits below (login, status, session) can outlive it.
    const LK = root.LivekitClient;
    const room = LK && LK.Room ? new LK.Room() : null;
    if (room) {
      try {
        room.startAudio().catch(() => {});
      } catch (err) {
        /* the Enable sound button covers it */
      }
    }

    const fail = (message) => {
      S.offlineReason = message;
      emitState(true);
      return new UhlexaError(message, 0, 'start_failed');
    };

    try {
      if (opts.handle) await login(opts.handle);

      const status = await fetchStatus();
      if (status.voice.status !== 'ok') {
        const msg = status.voice.detail || 'Voice is offline.';
        throw fail(msg);
      }

      let session;
      try {
        session = await api('POST', '/api/voice/session', { surface: opts.surface });
      } catch (err) {
        throw fail(err.message);
      }
      S.sessionInfo = session;
      S.wakeMode = session.wake_mode || null;

      if (!room) throw fail('The voice library did not load. Reload the page.');

      S.unsubBoard = watchBoard((board, err) => {
        if (board) {
          S.board = board;
          safe(opts.onBoard, board, null);
          emitState();
        } else {
          safe(opts.onBoard, null, err);
        }
      });

      S.room = room;
      bindRoom(room);
      try {
        await room.connect(session.url, session.token);
      } catch (err) {
        throw fail("Couldn't join the voice room" + (err && err.message ? ': ' + err.message : '.'));
      }
      S.connected = true;
      S.connectedAt = Date.now();
      S.offlineReason = '';

      try {
        await room.startAudio();
      } catch (err) {
        /* the audio-blocked button handles it */
      }
      syncAudioPlayback();

      try {
        await room.localParticipant.setMicrophoneEnabled(true);
      } catch (err) {
        safe(opts.onNotice, 'The microphone is blocked. Allow it in your browser to talk to Uhlexa.');
      }
      syncMic();

      S.timers.join = setTimeout(() => {
        if (!readAgent().present) {
          S.agentTimedOut = true;
          emitState(true);
        }
      }, AGENT_JOIN_TIMEOUT_MS);

      safe(opts.onConnection, 'connected');
      emitState(true);
      return {
        surface: session.surface,
        room: session.room,
        identity: session.identity,
        wakeMode: session.wake_mode,
        avatar: !!session.avatar,
      };
    } catch (err) {
      await leave(true);
      if (room && room !== S.room) {
        try {
          await room.disconnect(); // created early for the audio unlock but never connected: release its audio context
        } catch (e) {
          /* nothing to release */
        }
      }
      throw err instanceof Error ? err : new UhlexaError(String(err), 0, 'start_failed');
    }
  }

  async function leave(quiet) {
    const opts = S.opts;
    S.leaving = true;
    clearTimer('think');
    clearTimer('join');
    clearTimer('avatar');
    if (S.unsubBoard) {
      S.unsubBoard();
      S.unsubBoard = null;
    }
    if (S.room) {
      const room = S.room;
      try {
        detachAll();
      } catch (err) {
        /* ignore */
      }
      try {
        room.removeAllListeners();
      } catch (err) {
        /* ignore */
      }
      try {
        await room.disconnect();
      } catch (err) {
        /* ignore */
      }
    }
    S.room = null;
    S.connected = false;
    S.micOn = false;
    S.canPlayAudio = true;
    S.thinkingSince = 0;
    S.sessionInfo = null;
    S.board = null;
    S.opts = null;
    S.lastStateKey = '';
    S.lastAvatarKey = '';
    if (!quiet && opts) {
      safe(opts.onConnection, 'left');
      safe(opts.onMic, false);
    }
  }

  function sendData(message) {
    if (!S.room || !S.connected) return Promise.resolve(false);
    const bytes = new TextEncoder().encode(JSON.stringify(message));
    return S.room.localParticipant.publishData(bytes, { reliable: true }).then(
      () => true,
      () => false
    );
  }

  async function setMicEnabled(on) {
    if (!S.room || !S.connected) return false;
    try {
      await S.room.localParticipant.setMicrophoneEnabled(!!on);
    } catch (err) {
      safe(S.opts && S.opts.onNotice, 'The microphone is blocked. Allow it in your browser to talk to Uhlexa.');
    }
    syncMic();
    return S.micOn;
  }

  async function tapToTalk() {
    if (!S.room || !S.connected) return false;
    if (!S.micOn) await setMicEnabled(true);
    return sendData({ type: 'open' });
  }

  /** Saves the preference (PUT) and switches the live session at once (data message). Resolves to the saved mode. */
  async function setWakeMode(mode) {
    if (WAKE_MODES.indexOf(mode) < 0) throw new UhlexaError('Unknown wake mode.', 0, 'bad_mode');
    const saved = await api('PUT', '/api/voice/settings', { wake_mode: mode });
    S.wakeMode = (saved && saved.wake_mode) || mode;
    await sendData({ type: 'wake_mode', value: mode });
    return S.wakeMode;
  }

  async function enableAudio() {
    if (S.room) {
      try {
        await S.room.startAudio();
      } catch (err) {
        /* still blocked */
      }
      syncAudioPlayback();
    }
    return S.canPlayAudio;
  }

  /* ================================================================== UI helpers */

  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      Object.keys(props).forEach((key) => {
        const v = props[key];
        if (v === undefined || v === null || v === false) return;
        if (key === 'class') node.className = v;
        else if (key === 'text') node.textContent = v;
        else if (key === 'dataset') Object.keys(v).forEach((k) => { node.dataset[k] = v[k]; });
        else if (key === 'value') node.value = v;
        else if (key.slice(0, 2) === 'on' && typeof v === 'function') node.addEventListener(key.slice(2), v);
        else node.setAttribute(key, v === true ? '' : String(v));
      });
    }
    append(node, children);
    return node;
  }

  function append(node, children) {
    if (children === undefined || children === null || children === false) return;
    if (Array.isArray(children)) {
      children.forEach((c) => append(node, c));
    } else if (typeof children === 'string' || typeof children === 'number') {
      node.appendChild(document.createTextNode(String(children)));
    } else {
      node.appendChild(children);
    }
  }

  function emptyNote(host, text) {
    if (!text) return;
    const tag = host.tagName === 'UL' || host.tagName === 'OL' ? 'li' : 'p';
    host.appendChild(el(tag, { class: 'empty', text }));
  }

  function renderInto(host, data, build) {
    const sig = JSON.stringify(data);
    if (host._uhSig === sig) return false;
    host._uhSig = sig;
    host.textContent = '';
    build(host);
    return true;
  }

  function fmtMoney(cents) {
    const n = Number(cents);
    return Number.isFinite(n) ? '$' + (n / 100).toFixed(2) : '';
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    if (!iso || Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function words(s) {
    return String(s || '').replace(/[_-]+/g, ' ');
  }

  function sentence(s) {
    const w = words(s);
    return w.charAt(0).toUpperCase() + w.slice(1);
  }

  const TASK_STATUS = {
    pending_approval: 'Waiting for approval',
    approved: 'Approved',
    running: 'In progress',
    needs_takeover: 'Needs you',
    completed: 'Done',
    failed: 'Failed',
    denied: 'Denied',
    cancelled: 'Cancelled',
  };
  const TASK_ORDER = ['needs_takeover', 'pending_approval', 'approved', 'running', 'completed', 'failed', 'denied', 'cancelled'];
  const BROWSER_STATUS = {
    queued: 'Queued',
    starting: 'Starting',
    running: 'Working',
    needs_takeover: 'Needs you to take over',
    succeeded: 'Finished',
    failed: 'Failed',
    cancelled: 'Cancelled',
    unavailable: 'Unavailable',
  };
  const BROWSER_TONE = {
    queued: 'grey', starting: 'violet', running: 'violet', needs_takeover: 'amber',
    succeeded: 'ok', failed: 'bad', cancelled: 'grey', unavailable: 'grey',
  };

  function safeLink(url) {
    try {
      const u = new URL(String(url), root.location.href);
      return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
    } catch (err) {
      return null;
    }
  }

  function screenshotSrc(bt) {
    const ref = bt && bt.last_screenshot_ref;
    if (!ref || !bt.id) return '';
    const own = typeof ref === 'string' && ref.charAt(0) === '/' && ref.indexOf('//') !== 0;
    const base = own ? ref : '/api/browser-tasks/' + encodeURIComponent(bt.id) + '/screenshot';
    return base + (base.indexOf('?') < 0 ? '?v=' : '&v=') + encodeURIComponent(bt.updated_at || '');
  }

  /* ---- cards */

  const deciding = new Set();

  function approvalCard(a, opts) {
    const o = opts || {};
    const task = a.task || {};
    const pending = a.status === undefined || a.status === 'pending';
    const items = asArray(task.items).filter((x) => typeof x === 'string' && x);
    const card = el('article', { class: 'card approval' + (pending ? ' is-pending' : ''), dataset: { id: a.id } });

    card.appendChild(
      el('div', { class: 'card-head' }, [
        el('h3', { class: 'card-title', text: task.title || 'Approval needed' }),
        el('span', { class: 'chip ' + (pending ? 'chip-amber' : a.status === 'approved' ? 'chip-ok' : 'chip-grey'), text: pending ? (a.can_decide === true ? 'Needs your OK' : 'Waiting') : sentence(a.status) }),
      ])
    );
    if (task.amount_cents !== null && task.amount_cents !== undefined) card.appendChild(el('p', { class: 'card-meta', text: fmtMoney(task.amount_cents) }));
    if (items.length) card.appendChild(el('ul', { class: 'item-list' }, items.map((i) => el('li', { text: i }))));
    if (task.will_use_browser) card.appendChild(el('p', { class: 'card-meta', text: 'Will use the family computer' }));
    if (a.reason) card.appendChild(el('p', { class: 'card-note', text: a.reason }));
    if (a.requested_for_name) card.appendChild(el('p', { class: 'card-meta', text: 'For ' + a.requested_for_name }));

    if (!pending) {
      if (a.decided_by_name) card.appendChild(el('p', { class: 'card-meta', text: 'Decided by ' + a.decided_by_name }));
      return card;
    }
    if (o.hintOnly) {
      card.appendChild(el('p', { class: 'card-hint', text: 'Approve on the app or hub' }));
      return card;
    }
    if (a.can_decide !== true) {
      card.appendChild(el('p', { class: 'card-hint', text: a.can_decide_reason || 'Waiting for a grown-up' }));
      return card;
    }

    const error = el('p', { class: 'card-error', role: 'alert', hidden: true });
    const approve = el('button', { type: 'button', class: 'btn btn-primary', text: 'Approve' });
    const deny = el('button', { type: 'button', class: 'btn btn-ghost', text: 'Deny' });
    const busy = deciding.has(a.id);
    approve.disabled = busy;
    deny.disabled = busy;

    function decide(verb) {
      if (deciding.has(a.id)) return;
      deciding.add(a.id);
      approve.disabled = true;
      deny.disabled = true;
      error.hidden = true;
      decideApproval(a.id, verb).then(
        () => {
          deciding.delete(a.id);
        },
        (err) => {
          deciding.delete(a.id);
          approve.disabled = false;
          deny.disabled = false;
          error.textContent = err.message;
          error.hidden = false;
        }
      );
    }
    approve.addEventListener('click', () => decide('approve'));
    deny.addEventListener('click', () => decide('deny'));
    card.appendChild(el('div', { class: 'card-actions' }, [approve, deny]));
    card.appendChild(error);
    return card;
  }

  function browserCard(bt, opts) {
    const o = opts || {};
    const status = bt.status || 'queued';
    const link = bt.takeover_url ? safeLink(bt.takeover_url) : null;
    const card = el('article', { class: 'card browser status-' + status, dataset: { id: bt.id } });

    const chips = [el('span', { class: 'chip chip-' + (BROWSER_TONE[status] || 'grey'), text: BROWSER_STATUS[status] || sentence(status) })];
    if (bt.verified === true) chips.push(el('span', { class: 'chip chip-ok', text: 'Verified' }));
    else if (status === 'succeeded') chips.push(el('span', { class: 'chip chip-grey', text: 'Not verified' }));
    card.appendChild(el('div', { class: 'card-head' }, [el('h3', { class: 'card-title', text: 'Family computer' }), el('div', { class: 'chip-row' }, chips)]));

    if (bt.requested_url) card.appendChild(el('p', { class: 'card-meta url', text: bt.requested_url }));
    if (bt.last_error) card.appendChild(el('p', { class: 'card-error', text: bt.last_error }));

    if (o.screenshot) {
      const src = screenshotSrc(bt);
      if (src) {
        const img = el('img', { class: 'shot', src, alt: 'Latest screenshot from the family computer', loading: 'lazy' });
        img.addEventListener('error', () => { img.hidden = true; });
        card.appendChild(img);
      }
    }
    const steps = asArray(bt.steps).slice(-(o.steps || 3));
    if (steps.length) {
      card.appendChild(
        el('ol', { class: 'steps' }, steps.map((s) => el('li', { class: s && s.ok === false ? 'is-bad' : 'is-ok', text: ((s && s.action) || 'step') + (s && s.note ? ': ' + s.note : '') })))
      );
    }
    if (link) {
      card.appendChild(
        el('div', { class: 'card-actions' }, [el('a', { class: 'btn btn-amber', href: link, target: '_blank', rel: 'noopener noreferrer', text: 'Take over' })])
      );
    }
    return card;
  }

  function conversationCard(c) {
    const state = c.thread_state || {};
    const items = asArray(state.items)
      .map((x) => (typeof x === 'string' ? x : x && (x.text || x.name || x.title)))
      .filter((x) => typeof x === 'string' && x);
    const people = asArray(c.participants).map((p) => p && p.name).filter(Boolean);
    const resumed = asArray(c.resumed_by).filter(Boolean);
    const creator = typeof c.created_by_agent_name === 'string' ? c.created_by_agent_name : '';
    const card = el('article', { class: 'card thread', dataset: { id: c.id } });
    card.appendChild(
      el('div', { class: 'card-head' }, [
        el('h3', { class: 'card-title', text: c.title || 'Thread' }),
        c.status && c.status !== 'open' ? el('span', { class: 'chip chip-grey', text: sentence(c.status) }) : null,
      ])
    );
    if (items.length) card.appendChild(el('ul', { class: 'item-list' }, items.map((i) => el('li', { text: i }))));
    // "Started by A · continued by B, C": which agent opened the thread and which agents picked it up since.
    let origin = '';
    if (creator && resumed.length) origin = 'Started by ' + creator + ' · continued by ' + resumed.join(', ');
    else if (creator) origin = 'Started by ' + creator;
    else if (resumed.length) origin = 'Continued by ' + resumed.join(', ');
    if (origin) card.appendChild(el('p', { class: 'card-meta', text: origin }));
    if (people.length) card.appendChild(el('p', { class: 'card-meta', text: 'With ' + people.join(', ') }));
    return card;
  }

  function renderApprovals(host, board, opts) {
    const o = opts || {};
    const pending = asArray(board && board.approvals && board.approvals.pending);
    const recent = o.recent ? asArray(board && board.approvals && board.approvals.recent).slice(0, o.recent) : [];
    renderInto(host, { pending, recent, hint: !!o.hintOnly, deciding: Array.from(deciding) }, () => {
      if (!pending.length && !recent.length) {
        emptyNote(host, o.empty === undefined ? 'Nothing waiting for an OK.' : o.empty);
        return;
      }
      pending.forEach((a) => host.appendChild(approvalCard(a, o)));
      recent.forEach((a) => host.appendChild(approvalCard(a, o)));
    });
  }

  function renderBrowserTasks(host, board, opts) {
    const o = opts || {};
    const list = asArray(board && board.browser_tasks).slice(0, o.limit || 2);
    renderInto(host, { list, shot: !!o.screenshot }, () => {
      if (!list.length) {
        emptyNote(host, o.empty === undefined ? 'The family computer is idle.' : o.empty);
        return;
      }
      list.forEach((bt) => host.appendChild(browserCard(bt, o)));
    });
  }

  function renderConversations(host, board, opts) {
    const o = opts || {};
    const list = asArray(board && board.conversations).slice(0, o.limit || 4);
    renderInto(host, list, () => {
      if (!list.length) {
        emptyNote(host, 'No threads yet.');
        return;
      }
      list.forEach((c) => host.appendChild(conversationCard(c)));
    });
  }

  function renderTasks(host, board) {
    const tasks = asArray(board && board.tasks);
    renderInto(host, tasks, () => {
      if (!tasks.length) {
        emptyNote(host, 'No tasks yet.');
        return;
      }
      const groups = {};
      tasks.forEach((t) => {
        const key = (t && t.status) || 'pending_approval';
        (groups[key] = groups[key] || []).push(t);
      });
      const order = TASK_ORDER.filter((k) => groups[k]).concat(Object.keys(groups).filter((k) => TASK_ORDER.indexOf(k) < 0));
      order.forEach((key) => {
        const group = groups[key];
        const shown = group.slice(0, 4);
        host.appendChild(
          el('section', { class: 'task-group status-' + key }, [
            el('h4', { class: 'group-title' }, [el('span', { text: TASK_STATUS[key] || sentence(key) }), el('span', { class: 'count', text: String(group.length) })]),
            el('ul', { class: 'item-list' }, shown.map((t) => el('li', { text: (t.title || 'Task') + (t.amount_cents != null ? ' · ' + fmtMoney(t.amount_cents) : '') }))),
            group.length > shown.length ? el('p', { class: 'card-meta', text: '+' + (group.length - shown.length) + ' more' }) : null,
          ])
        );
      });
    });
  }

  function renderActivity(host, board, limit) {
    const events = asArray(board && board.activity).slice(0, limit || 12);
    renderInto(host, events.map((e) => [e.id, e.summary, e.created_at]), () => {
      if (!events.length) {
        emptyNote(host, 'No activity yet.');
        return;
      }
      events.forEach((e) => {
        host.appendChild(el('li', { class: 'act' }, [el('time', { text: fmtTime(e.created_at) }), el('span', { text: e.summary || words(e.event_type) })]));
      });
    });
  }

  function renderProviders(host, board) {
    const providers = (board && board.providers) || {};
    const names = Object.keys(providers);
    renderInto(host, providers, () => {
      names.forEach((name) => {
        const p = providers[name] || {};
        const status = p.status || (p.configured ? 'ok' : 'unavailable');
        const tone = status === 'ok' ? 'ok' : status === 'disabled' ? 'grey' : 'amber';
        host.appendChild(
          el('li', { class: 'prov prov-' + tone, title: p.detail || '' }, [el('span', { class: 'dot', 'aria-hidden': 'true' }), el('span', { text: words(name) + ' · ' + words(status) })])
        );
      });
    });
  }

  /* ---- caption log (aria-live region; interim text stays aria-hidden until it is final) */

  function captionLog(host, opts) {
    const o = opts || {};
    const max = o.max || 6;
    const labels = o.labels || { heard: 'Heard', said: 'Said' };
    const nodes = new Map();

    function push(c) {
      if (!c || (!c.text && !c.final)) return;
      let node = nodes.get(c.id);
      if (!node) {
        if (!c.text) return;
        const label = el('span', { class: 'cap-label' + (o.hideLabels ? ' sr-only' : ''), text: labels[c.role] || c.role });
        const text = el('span', { class: 'cap-text' });
        node = el('div', { class: 'cap cap-' + c.role + (o.bubbles ? ' bubble' : '') }, [label, text]);
        node._uhText = text;
        host.appendChild(node);
        nodes.set(c.id, node);
        while (nodes.size > max) {
          const oldest = nodes.keys().next().value;
          const gone = nodes.get(oldest);
          nodes.delete(oldest);
          if (gone && gone.parentNode) gone.parentNode.removeChild(gone);
        }
      }
      if (c.text) node._uhText.textContent = c.text;
      if (c.final) {
        node.classList.remove('interim');
        node.removeAttribute('aria-hidden');
        if (!c.text && node._uhText.textContent === '') {
          nodes.delete(c.id);
          node.remove();
        }
      } else {
        node.classList.add('interim');
        node.setAttribute('aria-hidden', 'true');
      }
      if (o.bubbles) host.scrollTop = host.scrollHeight;
    }

    function clear() {
      nodes.clear();
      host.textContent = '';
    }
    return { push, clear };
  }

  /* ---- wake-mode segmented control (radiogroup with roving tabindex) */

  function modeToggle(host, onChange) {
    host.textContent = '';
    host.setAttribute('role', 'radiogroup');
    if (!host.getAttribute('aria-label')) host.setAttribute('aria-label', 'How Uhlexa wakes up');
    const buttons = {};
    let current = null;

    function paint(mode) {
      current = mode;
      WAKE_MODES.forEach((m) => {
        const on = m === mode;
        buttons[m].setAttribute('aria-checked', on ? 'true' : 'false');
        buttons[m].tabIndex = on || (!mode && m === WAKE_MODES[0]) ? 0 : -1;
        buttons[m].classList.toggle('is-on', on);
      });
    }

    function choose(mode) {
      if (mode === current) return;
      const previous = current;
      paint(mode);
      Promise.resolve(onChange && onChange(mode)).catch(() => paint(previous));
    }

    WAKE_MODES.forEach((m, i) => {
      const b = el('button', { type: 'button', role: 'radio', class: 'seg-btn', 'aria-checked': 'false', dataset: { mode: m }, text: MODE_LABELS[m] });
      b.addEventListener('click', () => choose(m));
      b.addEventListener('keydown', (ev) => {
        const next = ev.key === 'ArrowRight' || ev.key === 'ArrowDown' ? 1 : ev.key === 'ArrowLeft' || ev.key === 'ArrowUp' ? -1 : 0;
        if (!next) return;
        ev.preventDefault();
        const target = WAKE_MODES[(i + next + WAKE_MODES.length) % WAKE_MODES.length];
        buttons[target].focus();
        choose(target);
      });
      buttons[m] = b;
      host.appendChild(b);
    });
    paint(null);
    return { set: paint, get: () => current };
  }

  /* ---- hub agent tiles (VibeGen Meet tile state machine: idle / thinking / speaking) */

  const TILE_COLORS = ['#ff6b5a', '#8b5cf6', '#ffb020', '#f472b6'];

  function renderRail(host, board) {
    const members = asArray(board && board.members);
    const sig = members.map((m) => [m.handle, m.agent && m.agent.id, m.agent && m.agent.name, m.is_viewer]);
    if (host._uhSig !== JSON.stringify(sig)) {
      host._uhSig = JSON.stringify(sig);
      host.textContent = '';
      members.forEach((m, i) => {
        const name = (m.agent && m.agent.name) || (m.display_name ? m.display_name + "'s Uhlexa" : 'Uhlexa');
        const bars = [];
        for (let b = 0; b < 6; b += 1) bars.push(el('span', { class: 'bar' }));
        const tile = el('li', { class: 'tile', dataset: { agent: (m.agent && m.agent.id) || m.handle, handle: m.handle, state: 'idle' } }, [
          el('div', { class: 'tile-stage' }, [el('span', { class: 'tile-mono', 'aria-hidden': 'true', text: (m.display_name || m.handle || '?').charAt(0).toUpperCase() }), el('span', { class: 'wave', 'aria-hidden': 'true' }, bars)]),
          el('div', { class: 'tile-foot' }, [
            el('span', { class: 'tile-name', text: name }),
            el('span', { class: 'tile-badge', text: 'idle' }),
          ]),
          m.is_viewer ? el('span', { class: 'tile-you', text: 'you' }) : null,
        ]);
        tile.style.setProperty('--tile', TILE_COLORS[i % TILE_COLORS.length]);
        host.appendChild(tile);
      });
    }
  }

  function setTile(host, handle, state, badge) {
    const tile = Array.prototype.find.call(host.children, (t) => t.dataset.handle === handle);
    if (!tile) return;
    tile.dataset.state = state;
    tile.querySelector('.tile-badge').textContent = badge || state;
  }

  /** The viewer's tile follows voice; everyone else's follows recent activity by their agent (relative to the board clock). */
  function updateTiles(host, board, voiceState) {
    const members = asArray(board && board.members);
    const clock = Date.parse(board && board.generated_at);
    const events = asArray(board && board.activity);
    members.forEach((m) => {
      if (m.is_viewer) {
        const vs = voiceState || 'idle';
        const visual = vs === 'speaking' ? 'speaking' : vs === 'thinking' ? 'thinking' : 'idle';
        const badge = vs === 'idle' ? 'idle' : (STATE_META[vs] ? STATE_META[vs].pill.toLowerCase() : vs);
        setTile(host, m.handle, visual, badge);
        return;
      }
      const agentId = m.agent && m.agent.id;
      const recent = events.find((e) => {
        if (!e || !agentId || e.actor_agent_id !== agentId) return false;
        const t = Date.parse(e.created_at);
        return Number.isFinite(t) && Number.isFinite(clock) && clock - t <= 6000;
      });
      if (!recent) setTile(host, m.handle, 'idle', 'idle');
      else if (String(recent.event_type || '').indexOf('message') === 0) setTile(host, m.handle, 'speaking', 'speaking');
      else setTile(host, m.handle, 'thinking', 'working');
    });
  }

  /* ================================================================== page wiring */

  function $(id) {
    return document.getElementById(id);
  }

  /* ---- kiosk mode: ?kiosk=1[&as=<handle>] puts `kiosk` on <body> and CSS hides every control and banner (voice.css).
   * This file only reads the URL, logs what would have been a banner, and wires the one "Tap to start" gesture.
   * Without the param nothing below runs and the pages behave exactly as before. */

  const KIOSK = { on: false, as: '' };

  function readKiosk() {
    KIOSK.on = false;
    KIOSK.as = '';
    try {
      const q = new URLSearchParams(root.location.search);
      const flag = q.get('kiosk');
      if (flag === null || /^(0|false|off|no)$/i.test(flag.trim())) return;
      KIOSK.on = true;
      KIOSK.as = (q.get('as') || '').trim();
    } catch (err) {
      /* no location, or no URLSearchParams: normal mode */
    }
  }

  function kioskWarn(message) {
    if (root.console && message) root.console.warn('[uhlexa kiosk] ' + message);
  }

  function ringStateLabel(state) {
    return 'Uhlexa light ring: ' + (STATE_META[state] ? STATE_META[state].pill.toLowerCase() : state);
  }

  /** Everything the three pages share: picker, Start/Leave, state -> DOM, captions, mode toggle, mic and talk buttons. */
  function wirePage(cfg) {
    const body = document.body;
    const ring = cfg.ring;
    const log = cfg.captions ? captionLog(cfg.captions, cfg.captionOpts) : null;
    const rings = Array.prototype.slice.call(document.querySelectorAll('.ring:not(.ring-kiosk)'));
    let voiceStatus = null;
    let stopPlainBoard = null;
    let live = false;
    let startFailed = false;
    let starting = false;
    let kioskReady = false; // /demo/users has confirmed the as= handle
    const overlay = KIOSK.on ? $('kiosk-start') : null;
    if (KIOSK.on) body.classList.add('kiosk');

    const toggle = cfg.modeHost
      ? modeToggle(cfg.modeHost, async (mode) => {
          try {
            await setWakeMode(mode);
            showError('');
          } catch (err) {
            showError(err.message);
            throw err;
          }
        })
      : null;

    function showError(message) {
      if (KIOSK.on) {
        // no banner may ever be in frame: the console gets it instead
        kioskWarn(message);
        return;
      }
      if (cfg.error) {
        cfg.error.textContent = message || '';
        cfg.error.hidden = !message;
      }
    }

    function setRing(state, pill, caption, detail) {
      body.dataset.state = state;
      rings.forEach((r) => {
        r.dataset.state = state;
        r.setAttribute('aria-label', ringStateLabel(state));
      });
      if (cfg.pill) {
        cfg.pill.textContent = pill;
        cfg.pill.dataset.state = state;
      }
      if (cfg.caption) cfg.caption.textContent = caption || '';
      if (cfg.detail) cfg.detail.textContent = detail || '';
    }

    function showLive(on, voice) {
      live = on;
      cfg.pre.hidden = on;
      cfg.live.hidden = !on;
      body.classList.toggle('is-live', on);
      body.classList.toggle('no-voice', on && voice === false);
    }

    function resetIdle() {
      if (!voiceStatus) {
        setRing('asleep', 'Checking', 'Checking voice\u2026', '');
        if (cfg.onReset) cfg.onReset();
        return;
      }
      const ok = voiceStatus.voice.status === 'ok';
      setRing(ok ? 'asleep' : 'offline', ok ? 'Not started' : 'Offline', ok ? 'Press Start to talk to Uhlexa' : STATE_META.offline.caption, ok ? '' : voiceStatus.voice.detail || '');
      if (cfg.onReset) cfg.onReset();
    }

    function onState(view) {
      setRing(view.state, view.pill, view.caption, view.detail);
      if (cfg.onState) cfg.onState(view);
      if (toggle && view.wakeMode && toggle.get() !== view.wakeMode) toggle.set(view.wakeMode);
    }

    const callbacks = {
      onState,
      onCaption: (c) => { if (log) log.push(c); },
      onBoard: (board, err) => { if (cfg.onBoard) cfg.onBoard(board, err); },
      onAvatar: (v) => { if (cfg.onAvatar) cfg.onAvatar(v); },
      onMic: (on) => {
        if (cfg.micBtn) {
          cfg.micBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
          cfg.micBtn.classList.toggle('is-on', on);
          const label = cfg.micBtn.querySelector('.mic-label');
          if (label) label.textContent = on ? 'Mic on' : 'Mic muted';
        }
      },
      onAudio: (ok) => { if (cfg.audioBtn) cfg.audioBtn.hidden = ok; },
      onLevel: (level) => rings.forEach((r) => r.style.setProperty('--lvl', level.toFixed(2))),
      onNotice: (msg) => showError(msg),
      onConnection: () => {},
      videoEl: cfg.video || null,
    };

    async function populateUsers() {
      try {
        const users = await loadUsers();
        cfg.select.textContent = '';
        users.forEach((u) => cfg.select.appendChild(el('option', { value: u.handle, text: u.display_name + (u.role ? ' (' + words(u.role) + ')' : '') })));
        kioskResolve(users);
        if (!users.length) showError('No family members found. Reset the demo, then reload.');
        cfg.start.disabled = !users.length;
      } catch (err) {
        kioskOff('Could not load the demo users.');
        showError(err.message);
        cfg.start.disabled = true;
      }
    }

    async function refreshStatus() {
      voiceStatus = await fetchStatus();
      const ok = voiceStatus.voice.status === 'ok';
      if (cfg.statusNote) {
        cfg.statusNote.textContent = ok ? '' : 'Voice is offline: ' + (voiceStatus.voice.detail || voiceStatus.voice.status) + '.';
        cfg.statusNote.hidden = ok;
      }
      if (KIOSK.on && !ok) kioskWarn('Voice is offline: ' + (voiceStatus.voice.detail || voiceStatus.voice.status) + '.');
      if (cfg.noVoice) cfg.noVoice.hidden = ok && !startFailed;
      if (!live) resetIdle();
    }

    async function startSession() {
      showError('');
      cfg.start.disabled = true;
      starting = true;
      const label = cfg.start.textContent;
      cfg.start.textContent = 'Connecting…';
      try {
        await start(Object.assign({ surface: cfg.surface, handle: cfg.select.value }, callbacks));
        startFailed = false;
        showLive(true, true);
        if (cfg.onLive) cfg.onLive();
        try {
          const settings = await getSettings();
          if (toggle && settings && settings.wake_mode && !toggle.get()) toggle.set(settings.wake_mode);
        } catch (err) {
          /* the session response already carried the mode */
        }
      } catch (err) {
        showError(err.message);
        startFailed = true;
        await refreshStatus();
      } finally {
        starting = false;
        cfg.start.disabled = false;
        cfg.start.textContent = label;
      }
    }

    cfg.start.addEventListener('click', startSession);

    /* kiosk: no picker, no Start button. The as= handle is checked against /demo/users, then one tap anywhere starts
     * the session exactly like Start does (login, voice, board) and the overlay fades out once it is up or has failed. */

    function kioskOff(why) {
      if (!KIOSK.on) return;
      kioskWarn(why + ' Showing the normal page and picker instead.');
      KIOSK.on = false;
      body.classList.remove('kiosk');
      if (overlay) overlay.remove();
    }

    function kioskResolve(users) {
      if (!KIOSK.on) return;
      const want = KIOSK.as.toLowerCase();
      const hit = want ? users.find((u) => String(u.handle).toLowerCase() === want) : null;
      if (!hit) {
        kioskOff(want ? 'Unknown demo user "' + KIOSK.as + '".' : 'No as=<handle> given.');
        return;
      }
      cfg.select.value = hit.handle;
      kioskReady = true;
    }

    function kioskDismiss() {
      if (!overlay || overlay.classList.contains('is-leaving')) return;
      overlay.classList.add('is-leaving');
      setTimeout(() => overlay.remove(), 900);
    }

    // Also the retry: if the first attempt failed (voice offline), a tap anywhere tries again.
    function kioskTap() {
      if (!KIOSK.on || !kioskReady || live || starting) return;
      const ringEl = overlay && overlay.querySelector('.ring');
      if (ringEl) ringEl.dataset.state = 'thinking'; // the tap registered; still no text
      startSession().then(kioskDismiss, kioskDismiss);
    }

    if (KIOSK.on) {
      document.addEventListener('click', kioskTap);
      if (overlay) {
        overlay.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            kioskTap();
          }
        });
      }
      // the ring is the only control left on the speaker page: tapping it is Tap to talk
      if (cfg.kioskRingTalk && cfg.ring) cfg.ring.addEventListener('click', () => { if (KIOSK.on && live) tapToTalk(); });
      // no Enable sound button in kiosk: any later tap unlocks audio if the browser blocked it
      document.addEventListener('pointerdown', () => { if (KIOSK.on && live && !S.canPlayAudio) enableAudio(); });
    }

    if (cfg.noVoice) {
      cfg.noVoice.addEventListener('click', async () => {
        showError('');
        try {
          await login(cfg.select.value);
        } catch (err) {
          showError(err.message);
          return;
        }
        showLive(true, false);
        setRing('offline', STATE_META.offline.pill, STATE_META.offline.caption, 'Showing the family board without voice.');
        stopPlainBoard = watchBoard((board, err) => callbacks.onBoard(board, err));
        if (cfg.onLive) cfg.onLive();
      });
    }

    cfg.leave.addEventListener('click', async () => {
      if (stopPlainBoard) {
        stopPlainBoard();
        stopPlainBoard = null;
      }
      await leave();
      startFailed = false;
      if (log) log.clear();
      showLive(false);
      showError('');
      if (cfg.onLeave) cfg.onLeave();
      await refreshStatus();
    });

    if (cfg.talk) cfg.talk.addEventListener('click', () => { tapToTalk(); });
    if (cfg.micBtn) cfg.micBtn.addEventListener('click', () => { setMicEnabled(cfg.micBtn.getAttribute('aria-pressed') !== 'true'); });
    if (cfg.audioBtn) cfg.audioBtn.addEventListener('click', () => { enableAudio(); });

    window.addEventListener('pagehide', () => { leave(true); });

    showLive(false);
    resetIdle();
    populateUsers();
    refreshStatus();
    return { setRing, showError };
  }

  /* ---- echo */

  function initEcho() {
    const hint = $('hint');
    let lastBoard = null;
    function paintHint(state) {
      const pending = asArray(lastBoard && lastBoard.approvals && lastBoard.approvals.pending).filter((a) => a && a.can_decide === true).length;
      let text = '';
      if (state === 'needs_your_hands') text = 'Take over on the app or hub';
      else if (pending) text = (pending === 1 ? '1 waiting for your OK. ' : pending + ' waiting for your OK. ') + 'Approve on the app or hub';
      hint.textContent = text;
      hint.hidden = !text;
    }
    let state = 'asleep';
    wirePage({
      surface: 'echo',
      ring: $('ring'),
      pill: $('state-pill'),
      caption: $('state-caption'),
      detail: $('state-detail'),
      captions: $('captions'),
      captionOpts: { max: 3 },
      pre: $('pre'),
      live: $('live'),
      select: $('user-select'),
      start: $('start-btn'),
      error: $('start-error'),
      statusNote: $('status-note'),
      leave: $('leave-btn'),
      talk: $('talk-btn'),
      micBtn: $('mic-btn'),
      audioBtn: $('audio-btn'),
      modeHost: $('mode-toggle'),
      onState: (view) => { state = view.state; paintHint(state); },
      onBoard: (board) => { if (board) { lastBoard = board; paintHint(state); } },
      onReset: () => { lastBoard = null; paintHint('asleep'); },
      kioskRingTalk: true,
    });
  }

  /* ---- app (phone) */

  function initApp() {
    const sheet = $('sheet');
    const who = $('who');
    const approvals = $('approvals');
    const browser = $('browser');
    const tray = $('tray');

    function paintTray(board) {
      renderApprovals(approvals, board, { empty: '' });
      renderBrowserTasks(browser, board, { limit: 1, empty: '', steps: 2 });
      const has = !!(approvals.querySelector('.card') || browser.querySelector('.card'));
      tray.hidden = !has;
    }

    wirePage({
      surface: 'app',
      ring: $('ring'),
      pill: $('state-pill'),
      caption: $('state-caption'),
      detail: $('state-detail'),
      captions: $('chat'),
      captionOpts: { max: 60, bubbles: true, hideLabels: true, labels: { heard: 'You said', said: 'Uhlexa said' } },
      pre: $('pre'),
      live: $('live'),
      select: $('user-select'),
      start: $('start-btn'),
      noVoice: $('novoice-btn'),
      error: $('start-error'),
      statusNote: $('status-note'),
      leave: $('leave-btn'),
      talk: $('talk-btn'),
      micBtn: $('mic-btn'),
      audioBtn: $('audio-btn'),
      modeHost: $('mode-toggle'),
      onBoard: (board) => {
        if (!board) return;
        paintTray(board);
        who.textContent = board.viewer && board.viewer.display_name ? 'Signed in as ' + board.viewer.display_name : '';
      },
      onLeave: () => {
        if (sheet.open) sheet.close();
        tray.hidden = true;
        approvals.textContent = '';
        approvals._uhSig = null;
        browser.textContent = '';
        browser._uhSig = null;
        who.textContent = '';
      },
    });

    $('settings-btn').addEventListener('click', () => { if (!sheet.open) sheet.showModal(); });
    $('sheet-close').addEventListener('click', () => sheet.close());
    sheet.addEventListener('click', (ev) => { if (ev.target === sheet) sheet.close(); });
  }

  /* ---- hub */

  function initHub() {
    const rail = $('rail');
    const stageNotice = $('stage-notice');
    const ring = $('ring');
    let voiceState = 'idle';
    let lastBoard = null;

    function paint(board) {
      lastBoard = board;
      renderRail(rail, board);
      updateTiles(rail, board, voiceState);
      renderApprovals($('cards-approvals'), board, { recent: 2 });
      renderBrowserTasks($('cards-browser'), board, { screenshot: true, limit: 2, steps: 3 });
      renderConversations($('cards-threads'), board, { limit: 3 });
      renderTasks($('cards-tasks'), board);
      renderActivity($('activity'), board, 14);
      renderProviders($('providers'), board);
      $('counts').textContent = board && board.counts ? (board.counts.pending_approvals || 0) + ' waiting · ' + (board.counts.open_conversations || 0) + ' open threads' : '';
      $('who').textContent = board && board.viewer && board.viewer.display_name ? board.viewer.display_name : '';
    }

    function clearCards() {
      ['cards-approvals', 'cards-browser', 'cards-threads', 'cards-tasks', 'activity', 'providers', 'rail'].forEach((id) => {
        const host = $(id);
        host.textContent = '';
        host._uhSig = null;
      });
      $('counts').textContent = '';
      $('who').textContent = '';
      lastBoard = null;
    }

    wirePage({
      surface: 'hub',
      ring,
      video: $('stage-video'),
      pill: $('state-pill'),
      caption: $('state-caption'),
      detail: $('state-detail'),
      captions: $('captions'),
      captionOpts: { max: 4 },
      pre: $('pre'),
      live: $('live'),
      select: $('user-select'),
      start: $('start-btn'),
      noVoice: $('novoice-btn'),
      error: $('start-error'),
      statusNote: $('status-note'),
      leave: $('leave-btn'),
      talk: $('talk-btn'),
      micBtn: $('mic-btn'),
      audioBtn: $('audio-btn'),
      modeHost: $('mode-toggle'),
      onState: (view) => {
        voiceState = view.state;
        if (lastBoard) updateTiles(rail, lastBoard, voiceState);
      },
      onBoard: (board) => { if (board) paint(board); },
      onAvatar: (v) => {
        ring.classList.toggle('has-video', v.view === 'video');
        stageNotice.textContent = v.notice;
        stageNotice.hidden = !v.notice;
        if (KIOSK.on && v.notice) kioskWarn(v.notice);
      },
      onReset: () => {
        ring.classList.remove('has-video');
        stageNotice.hidden = true;
      },
      onLeave: clearCards,
    });
  }

  /* ------------------------------------------------------------------ exports and auto-init */

  const ui = {
    el, captionLog, modeToggle, renderApprovals, renderBrowserTasks, renderConversations, renderTasks,
    renderActivity, renderProviders, renderRail, updateTiles, approvalCard, browserCard, screenshotSrc, safeLink, fmtMoney, fmtTime,
  };

  const UhlexaVoice = {
    start,
    leave,
    computeState,
    setWakeMode,
    tapToTalk,
    setMicEnabled,
    enableAudio,
    watchBoard,
    decideApproval,
    loadUsers,
    login,
    fetchStatus,
    getSettings,
    WAKE_MODES,
    MODE_LABELS,
    STATE_META,
    UhlexaError,
    ui,
  };

  root.UhlexaVoice = UhlexaVoice;
  if (typeof module !== 'undefined' && module.exports) module.exports = UhlexaVoice;

  if (typeof document !== 'undefined') {
    const init = () => {
      readKiosk();
      const surface = document.body && document.body.dataset.surface;
      if (surface === 'echo') initEcho();
      else if (surface === 'app') initApp();
      else if (surface === 'hub') initHub();
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }
})(typeof window !== 'undefined' ? window : globalThis);

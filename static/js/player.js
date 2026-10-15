/* AURALIS web player.
 *
 * One HTMLAudioElement drives a client-side queue built by scraping the
 * `data-track-*` attributes off the current page's track rows (see
 * templates/music/includes/track_table.html). Playing audio URLs come from
 * /player/manifest/, which returns absolute media paths plus authoritative
 * durations in one round trip.
 *
 * Design notes:
 *  - Play counting is fire-and-forget: it POSTs /api/tracks/<id>/play/ once per
 *    completed play and swallows failures so a flaky network never stalls audio.
 *  - Volume, shuffle and repeat persist in localStorage; the queue does not,
 *    because a stale queue of deleted tracks is worse than an empty one.
 *  - Queue state is the single source of truth. The <audio> element mirrors it.
 */
(function () {
  "use strict";

  var MANIFEST_URL = "/player/manifest/";
  var MANIFEST_COLLECTION_URL = "/player/collection/";
  var LYRICS_URL = "/player/lyrics/";
  var PLAY_URL = "/api/tracks/";
  var FAVORITE_URL = "/api/favorites/tracks/";
  var STORAGE_KEY = "auralis.player.prefs";

  var SEEK_STEPS = 1000; // resolution of the 0..1000 range input

  var el = {};
  var audio = null;

  /* --- state -------------------------------------------------------------- */

  var state = {
    queue: [],          // [{ id, title, artist, artistSlug, url, cover, duration, pageUrl }]
    index: -1,
    shuffle: false,
    repeat: "off",      // "off" | "all" | "one"
    volume: 0.8,
    muted: false,
    seeking: false,
    reportedTrackId: null,
    prefsLoaded: false,
    npOpen: false,
    lyricsTrackId: null,
    lyricsRequestId: 0
  };

  /* --- utils -------------------------------------------------------------- */

  function $(id) { return document.getElementById(id); }

  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    var total = Math.floor(seconds);
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    var mm = h > 0 ? String(m).padStart(2, "0") : String(m);
    return (h > 0 ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0");
  }

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  /* Adaptive streaming (HLS) is only used when the browser can play it
   * natively. That covers Safari and iOS, which is where it matters most;
   * every other engine falls back to the progressive MP3 the manifest also
   * carries, so no hls.js dependency is needed. */
  function canPlayNativeHls() {
    return !!(audio && audio.canPlayType &&
      audio.canPlayType("application/vnd.apple.mpegurl") !== "");
  }

  /* --- preferences -------------------------------------------------------- */

  function loadPrefs() {
    if (state.prefsLoaded) return;
    state.prefsLoaded = true;
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      var prefs = JSON.parse(raw);
      if (typeof prefs.volume === "number") state.volume = Math.min(1, Math.max(0, prefs.volume));
      if (typeof prefs.shuffle === "boolean") state.shuffle = prefs.shuffle;
      if (["off", "all", "one"].indexOf(prefs.repeat) !== -1) state.repeat = prefs.repeat;
    } catch (err) {
      // Private mode / disabled storage: run with defaults.
    }
  }

  function savePrefs() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
        volume: state.volume,
        shuffle: state.shuffle,
        repeat: state.repeat
      }));
    } catch (err) { /* non-fatal */ }
  }

  /* --- queue scraping ----------------------------------------------------- */

  function trackFromRow(row) {
    var data = row.dataset;
    return {
      id: data.trackId,
      title: data.trackTitle || "Untitled",
      artist: data.trackArtist || "",
      artistSlug: data.trackArtistSlug || "",
      url: data.trackUrl || "",
      cover: data.trackCover || "",
      duration: parseInt(data.trackDuration, 10) || 0,
      isFavorite: false,
      row: row
    };
  }

  /* Replace the queue with every playable row on the page, preserving order. */
  function collectPageQueue(scope) {
    var rows = (scope || document).querySelectorAll("tr[data-track-id]");
    var tracks = [];
    var seen = Object.create(null);
    for (var i = 0; i < rows.length; i++) {
      var track = trackFromRow(rows[i]);
      if (!track.id || seen[track.id]) continue;
      seen[track.id] = true;
      tracks.push(track);
    }
    return tracks;
  }

  /* A hero/detail "Play" button is not a row, so it needs its own parser. It
   * carries the same data-track-* payload the tables do. */
  function trackFromButton(node) {
    if (node.tagName !== "BUTTON") return null;
    var data = node.dataset;
    return {
      id: data.trackId,
      title: data.trackTitle || "Untitled",
      artist: data.trackArtist || "",
      artistSlug: data.trackArtistSlug || "",
      url: data.trackUrl || "",
      cover: data.trackCover || "",
      duration: parseInt(data.trackDuration, 10) || 0,
      isFavorite: false,
      row: null
    };
  }

  /* --- manifest hydration ------------------------------------------------- */

  /* The page markup carries no audio URL (keeping it out of the HTML), so ask
   * the server for real sources before the first play of a queue. */
  function hydrate(tracks) {
    if (!tracks.length) return Promise.resolve(tracks);
    var ids = tracks.map(function (t) { return t.id; }).join(",");
    return fetch(MANIFEST_URL + "?ids=" + encodeURIComponent(ids), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        if (!payload || !payload.tracks) return tracks;
        var byId = Object.create(null);
        payload.tracks.forEach(function (row) {
          byId[row.id] = {
            id: row.id,
            title: row.title,
            artist: row.artist,
            artistSlug: row.artist_slug,
            url: row.url,
            hlsUrl: row.hls_url || "",
            cover: row.cover,
            duration: row.duration_seconds || 0,
            isFavorite: !!row.is_favorite,
            pageUrl: row.track_slug ? "/tracks/" + row.track_slug + "/" : ""
          };
        });
        // Merge rather than replace: keep each row's position and page link for
        // tracks the manifest skipped (drafts, or a track with no audio file).
        return tracks.map(function (track) {
          var full = byId[track.id];
          if (!full) return null;
          full.row = track.row;
          full.pageUrl = track.url || full.pageUrl;
          return full;
        }).filter(Boolean);
      })
      .catch(function () {
        return tracks.filter(function (t) { return t.url; });
      });
  }

  /* --- queue operations --------------------------------------------------- */

  function setQueue(tracks, startIndex) {
    state.queue = tracks;
    state.index = startIndex == null ? -1 : startIndex;
    if (state.shuffle && tracks.length > 1) {
      state.index = Math.floor(Math.random() * tracks.length);
    }
    renderQueue();
    updateQueueCount();
  }

  function playAt(index, autoplay) {
    if (index < 0 || index >= state.queue.length) return;
    state.index = index;
    var track = state.queue[index];

    // Each load starts a new listen, so re-arm play reporting.
    state.reportedTrackId = null;

    // Prefer the adaptive HLS rendition when the track has one and this
    // browser can play it; otherwise use the progressive source.
    audio.src = (track.hlsUrl && canPlayNativeHls()) ? track.hlsUrl : track.url;
    audio.load();

    renderNowPlaying();
    markActiveRow();
    show();

    if (autoplay !== false) {
      var attempt = audio.play();
      if (attempt && typeof attempt.catch === "function") {
        // Autoplay policies reject until the user has interacted with the page.
        // Stay paused and let the UI reflect it rather than throwing.
        attempt.catch(function () { syncPlayState(); });
      }
    }
    syncPlayState();
  }

  function nextIndex(direction) {
    var count = state.queue.length;
    if (!count) return -1;

    if (state.shuffle && count > 1) {
      var pick = state.index;
      while (pick === state.index) pick = Math.floor(Math.random() * count);
      return pick;
    }
    var next = state.index + (direction || 1);
    if (next >= count) {
      if (state.repeat === "all") return 0;
      return -1; // end of queue
    }
    if (next < 0) return state.repeat === "all" ? count - 1 : 0;
    return next;
  }

  function handleEnded() {
    if (state.repeat === "one") {
      audio.currentTime = 0;
      audio.play().catch(function () {});
      return;
    }
    var next = nextIndex(1);
    if (next === -1) {
      audio.pause();
      syncPlayState();
      return;
    }
    playAt(next, true);
  }

  /* --- playback reporting ------------------------------------------------- */

  /* Count at most one play per listen. A listen starts when a track is loaded,
   * so replaying a track counts again, while reaching the end after already
   * crossing the halfway mark does not double count. Halfway is the industry
   * threshold for "a play"; ``force`` covers tracks that end before it. */
  function maybeReportPlay(force) {
    var track = state.queue[state.index];
    if (!track || state.reportedTrackId === track.id) return;

    if (!force) {
      var duration = audio.duration;
      var threshold = isFinite(duration) && duration > 0 ? duration * 0.5 : 30;
      if (audio.currentTime < threshold && !audio.ended) return;
    }

    state.reportedTrackId = track.id;
    reportPlay(track.id, Math.round(audio.currentTime), true);
  }

  function reportPlay(trackId, listenedSeconds, completed) {
    var body = JSON.stringify({
      source: "web",
      listened_seconds: listenedSeconds || 0,
      completed: completed !== false
    });
    fetch(PLAY_URL + trackId + "/play/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken() },
      body: body
    }).catch(function () { /* analytics must never break playback */ });
  }

  /* --- favourites --------------------------------------------------------- */

  function toggleFavorite(trackId, button) {
    var pressed = button.getAttribute("aria-pressed") === "true";
    fetch(FAVORITE_URL + trackId + "/", {
      method: pressed ? "DELETE" : "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken() }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("favourite failed");
        return response.json();
      })
      .then(function (payload) {
        var now = !pressed;
        button.setAttribute("aria-pressed", now ? "true" : "false");
        document.querySelectorAll('[data-favorite="' + trackId + '"]').forEach(function (node) {
          node.setAttribute("aria-pressed", now ? "true" : "false");
          if (node.textContent.indexOf("Favourite") !== -1) {
            node.textContent = now ? "♥ Favourited" : "♡ Favourite";
          }
        });

        // Keep the queue's copy in step so switching tracks (or revisiting a
        // queue later) does not show a stale heart.
        var queued = state.queue.find(function (track) { return track.id === trackId; });
        if (queued) queued.isFavorite = now;

        var playing = current();
        if (playing && playing.id === trackId) syncLikeButton(playing);

        if (payload && typeof payload.favorite_count === "number" && el.playerLike) {
          el.playerLike.dataset.count = payload.favorite_count;
        }
      })
      .catch(function () { /* ignore: the button keeps its previous state */ });
  }

  /* --- follows ------------------------------------------------------------ */

  var FOLLOW_URLS = {
    artist: "/api/artists/",
    playlist: "/api/playlists/",
    user: "/api/users/"
  };

  function toggleFollow(kind, id, button) {
    var base = FOLLOW_URLS[kind];
    if (!base || !id) return;
    var pressed = button.getAttribute("aria-pressed") === "true";
    fetch(base + id + "/follow/", {
      method: pressed ? "DELETE" : "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken() }
    })
      .then(function (response) {
        if (!response.ok) throw new Error("follow failed");
        var now = !pressed;
        var selector = "[data-follow-" + kind + '="' + id + '"]';
        document.querySelectorAll(selector).forEach(function (node) {
          node.setAttribute("aria-pressed", now ? "true" : "false");
          var label = node.querySelector("[data-follow-label]");
          if (label) label.textContent = now ? "Following" : "Follow";
        });
      })
      .catch(function () { /* ignore: the button keeps its previous state */ });
  }

  /* --- rendering ---------------------------------------------------------- */

  function show() { if (el.player) el.player.hidden = false; document.body.classList.add("has-player"); }

  function current() { return state.queue[state.index] || null; }

  function renderNowPlaying() {
    var track = current();
    if (!el.title || !track) return;
    el.title.textContent = track.title;
    if (el.artist) el.artist.textContent = track.artist;

    setArt(el.art, el.artFallback, track);

    // Mirror the same information onto the full-screen surface.
    if (el.npTitle) el.npTitle.textContent = track.title;
    if (el.npArtist) el.npArtist.textContent = track.artist;
    setArt(el.npArt, el.npArtFallback, track);

    if (el.duration) el.duration.textContent = formatTime(track.duration);
    if (el.npDuration) el.npDuration.textContent = formatTime(track.duration);

    syncLikeButton(track);
    loadNpLyrics(track);
    syncLyrics();
    renderQueue();
  }

  /* Point an <img>/fallback placeholder pair at a track's cover art. */
  function setArt(img, fallback, track) {
    if (!img || !fallback) return;
    if (track.cover) {
      img.src = track.cover;
      img.alt = "Cover art for " + track.title;
      img.hidden = false;
      fallback.hidden = true;
    } else {
      img.hidden = true;
      fallback.hidden = false;
    }
  }

  /* The heart is meaningless without an account, and its pressed state has to
   * follow whatever track is loaded, so refresh both on every render. */
  function syncLikeButton(track) {
    var authed = isAuthenticated();
    [el.like, el.npLike].forEach(function (button) {
      if (!button) return;
      if (!track || !authed) {
        button.hidden = true;
        return;
      }
      button.hidden = false;
      button.setAttribute("aria-pressed", track.isFavorite ? "true" : "false");
      button.setAttribute(
        "aria-label",
        (track.isFavorite ? "Remove from favourites: " : "Favourite: ") + track.title
      );
    });
  }

  function isAuthenticated() {
    return !!(el.player && el.player.dataset.authenticated === "1");
  }

  function markActiveRow() {
    var track = current();
    document.querySelectorAll("tr[data-track-id]").forEach(function (row) {
      var isCurrent = track && row.dataset.trackId === track.id;
      row.classList.toggle("is-playing", !!isCurrent);
      var button = row.querySelector(".row-play");
      if (button) button.classList.toggle("is-active", !!isCurrent);
    });
  }

  function updateQueueCount() {
    if (el.queueCount) el.queueCount.textContent = String(state.queue.length);
  }

  function renderQueue() {
    renderQueueInto(el.queueList, "Queue is empty. Press play on any track.");
    renderQueueInto(el.npQueueList, "Nothing queued yet.");
    if (el.queueSource) el.queueSource.textContent = state.queue.length ? pageLabel() : "";
  }

  function renderQueueInto(list, emptyText) {
    if (!list) return;
    list.textContent = "";

    if (!state.queue.length) {
      var empty = document.createElement("li");
      empty.className = "queue__empty";
      empty.textContent = emptyText;
      list.appendChild(empty);
      return;
    }

    state.queue.forEach(function (track, position) {
      var item = document.createElement("li");
      item.className = "queue__item" + (position === state.index ? " is-current" : "");
      item.tabIndex = 0;
      item.setAttribute("role", "button");

      var art = document.createElement("span");
      art.className = "art";
      if (track.cover) {
        var img = document.createElement("img");
        img.src = track.cover;
        img.alt = "";
        img.loading = "lazy";
        art.appendChild(img);
      } else {
        art.classList.add("art--placeholder");
        art.textContent = "♫";
      }

      var text = document.createElement("div");
      text.className = "queue__meta";
      var title = document.createElement("div");
      title.className = "queue__title";
      title.textContent = track.title;
      var sub = document.createElement("div");
      sub.className = "queue__sub";
      sub.textContent = track.artist;
      text.appendChild(title);
      text.appendChild(sub);

      var dur = document.createElement("span");
      dur.className = "queue__dur";
      dur.textContent = formatTime(track.duration);

      item.appendChild(art);
      item.appendChild(text);
      item.appendChild(dur);
      item.addEventListener("click", function () { playAt(position, true); });
      item.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          playAt(position, true);
        }
      });
      list.appendChild(item);
    });
  }

  function pageLabel() {
    var detail = document.querySelector(".detail__title");
    if (detail) return detail.textContent.trim();
    return document.title.replace(/ · .*$/, "");
  }

  function syncPlayState() {
    if (!el.toggle || !audio) return;
    var playing = !audio.paused && !audio.ended;
    var labels = { "true": "Pause", "false": "Play" };
    [el.toggle, el.npToggle].forEach(function (button) {
      if (!button) return;
      var iconPlay = button.querySelector(".icon-play");
      var iconPause = button.querySelector(".icon-pause");
      if (iconPlay) iconPlay.hidden = playing;
      if (iconPause) iconPause.hidden = !playing;
      button.setAttribute("aria-label", labels[String(playing)]);
      button.title = labels[String(playing)];
    });
    document.querySelectorAll(".row-play").forEach(function (button) {
      var row = button.closest("tr[data-track-id]");
      var track = current();
      button.classList.toggle("is-active", !!(track && row && row.dataset.trackId === track.id && playing));
    });
  }

  function syncSeek() {
    if (!audio) return;
    var duration = audio.duration;
    if (state.seeking) return; // don't fight the user's drag

    var ratio = isFinite(duration) && duration > 0 ? audio.currentTime / duration : 0;
    var position = Math.max(0, Math.min(1, ratio));
    var width = (position * 100) + "%";

    [el.fill, el.npFill].forEach(function (node) { if (node) node.style.width = width; });
    [el.seek, el.npSeek].forEach(function (node) { if (node) node.value = String(Math.round(position * SEEK_STEPS)); });
    [el.elapsed, el.npElapsed].forEach(function (node) {
      if (node) node.textContent = formatTime(audio.currentTime);
    });
    [el.duration, el.npDuration].forEach(function (node) {
      if (node && isFinite(duration)) node.textContent = formatTime(duration);
    });
    var buffered = audio.buffered;
    var bufferedWidth = (buffered.length ? (buffered.end(buffered.length - 1) / duration) * 100 : 0) + "%";
    [el.buffer, el.npBuffer].forEach(function (node) { if (node) node.style.width = bufferedWidth; });
  }

  /* --- transport handlers ------------------------------------------------- */

  function togglePlay() {
    if (!audio) return;
    if (!state.queue.length) {
      // Nothing queued yet: adopt whatever is on screen.
      var rows = collectPageQueue();
      if (!rows.length) return;
      hydrate(rows).then(function (tracks) {
        setQueue(tracks, 0);
        playAt(0, true);
      });
      return;
    }
    if (audio.paused) {
      audio.play().catch(function () {});
    } else {
      audio.pause();
    }
    syncPlayState();
  }

  function step(direction) {
    var target = nextIndex(direction);
    if (target === -1) {
      audio.pause();
      audio.currentTime = 0;
      syncPlayState();
      return;
    }
    playAt(target, true);
  }

  function setRepeat(mode) {
    state.repeat = mode;
    savePrefs();
    renderRepeat();
  }

  function cycleRepeat() {
    setRepeat(state.repeat === "off" ? "all" : state.repeat === "all" ? "one" : "off");
  }

  function renderRepeat() {
    var active = state.repeat !== "off";
    var labels = { off: "Repeat off", all: "Repeat all", one: "Repeat one" };
    [el.repeat, el.npRepeat].forEach(function (button) {
      if (!button) return;
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.setAttribute("aria-label", labels[state.repeat]);
      button.title = labels[state.repeat];
    });
    [el.repeatOne, el.npRepeatOne].forEach(function (badge) {
      if (badge) badge.hidden = state.repeat !== "one";
    });
  }

  function toggleQueuePanel(force) {
    if (!el.queue) return;
    var show = force == null ? el.queue.hidden : force;
    el.queue.hidden = !show;
    if (el.queueToggle) el.queueToggle.setAttribute("aria-expanded", show ? "true" : "false");
  }

  /* --- now-playing surface ------------------------------------------------ */

  function openNowPlaying() {
    if (!el.np) return;
    state.npOpen = true;
    el.np.hidden = false;
    document.body.classList.add("np-open");
    if (el.npClose) el.npClose.focus();
    syncPlayState();
    syncSeek();
  }

  function closeNowPlaying() {
    if (!el.np || !state.npOpen) return;
    state.npOpen = false;
    el.np.hidden = true;
    document.body.classList.remove("np-open");
    if (el.npOpen) el.npOpen.focus();
  }

  function bindNpTabs() {
    var tabs = document.querySelectorAll("[data-np-tab]");
    if (!tabs.length) return;
    Array.prototype.forEach.call(tabs, function (tab) {
      tab.addEventListener("click", function () {
        Array.prototype.forEach.call(tabs, function (other) {
          var active = other === tab;
          other.classList.toggle("is-active", active);
          other.setAttribute("aria-selected", active ? "true" : "false");
          var panel = $("np-panel-" + other.dataset.npTab);
          if (panel) panel.hidden = !active;
        });
      });
    });
  }

  /* --- lyrics (now-playing) ----------------------------------------------- */

  /* Lyrics are fetched lazily: only when the full-screen view is populated, and
   * only once per track. A stale response is dropped if the user skipped on
   * before it arrived, so the panel never shows the wrong song's words. */
  function loadNpLyrics(track) {
    if (!el.npLyrics || !track) return;

    if (String(track.id) === state.lyricsTrackId) {
      syncLyrics();
      return;
    }

    var requestId = ++state.lyricsRequestId;
    state.lyricsTrackId = String(track.id);
    el.npLyrics.dataset.lyricsTrackId = String(track.id);
    el.npLyrics.classList.remove("lyrics--synced");
    setLyricsMessage("Loading lyrics…");

    fetch(LYRICS_URL + "?id=" + encodeURIComponent(track.id), { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        if (requestId !== state.lyricsRequestId) return;
        if (!payload || String(payload.id) !== state.lyricsTrackId) {
          setLyricsMessage("Couldn't load lyrics.");
          return;
        }
        renderLyrics(payload);
      })
      .catch(function () {
        if (requestId !== state.lyricsRequestId) return;
        setLyricsMessage("Couldn't load lyrics.");
      });
  }

  function setLyricsMessage(text) {
    if (!el.npLyrics) return;
    el.npLyrics.textContent = "";
    var note = document.createElement("p");
    note.className = "lyrics__placeholder";
    note.textContent = text;
    el.npLyrics.appendChild(note);
  }

  function renderLyrics(payload) {
    if (!el.npLyrics) return;
    el.npLyrics.textContent = "";
    el.npLyrics.classList.toggle("lyrics--synced", !!payload.synced);

    var lines = payload.lines || [];
    if (!payload.has_lyrics || !lines.length) {
      setLyricsMessage("No lyrics for this track.");
      return;
    }

    lines.forEach(function (line) {
      var node = document.createElement("p");
      node.className = "lyrics__line";
      node.textContent = line.text;
      if (line.time != null) node.dataset.lyricsTime = String(line.time);
      el.npLyrics.appendChild(node);
    });

    syncLyrics();
  }

  /* --- track context menu ------------------------------------------------- */

  var menuContext = null;

  function openTrackMenu(button) {
    if (!el.menu) return;
    var row = button.closest("tr[data-track-id]");
    var data = row ? row.dataset : button.dataset;
    menuContext = {
      row: row,
      id: data.trackId || button.dataset.menuTrack,
      title: data.trackTitle || "",
      pageUrl: data.trackUrl || "",
      artistUrl: data.trackArtistUrl || "",
      albumUrl: data.trackAlbumUrl || ""
    };

    var artistLink = el.menu.querySelector("[data-menu-artist]");
    if (artistLink) {
      artistLink.hidden = !menuContext.artistUrl;
      if (menuContext.artistUrl) artistLink.href = menuContext.artistUrl;
    }
    var albumLink = el.menu.querySelector("[data-menu-album]");
    if (albumLink) {
      albumLink.hidden = !menuContext.albumUrl;
      if (menuContext.albumUrl) albumLink.href = menuContext.albumUrl;
    }

    el.menu.hidden = false;
    var rect = button.getBoundingClientRect();
    var maxLeft = window.innerWidth - el.menu.offsetWidth - 8;
    var maxTop = window.innerHeight - el.menu.offsetHeight - 8;
    var left = Math.max(8, Math.min(rect.right - el.menu.offsetWidth, maxLeft));
    var top = Math.max(8, Math.min(rect.bottom + 6, maxTop));
    el.menu.style.left = left + "px";
    el.menu.style.top = top + "px";

    var first = el.menu.querySelector("[data-menu-play]");
    if (first) first.focus();
  }

  function closeTrackMenu() {
    if (el.menu) el.menu.hidden = true;
    menuContext = null;
  }

  function bindMenu() {
    if (!el.menu) return;

    el.menu.addEventListener("click", function (event) {
      var item = event.target.closest("[data-menu-play], [data-menu-queue], [data-menu-copy]");
      if (!item) return; // artist / album links navigate normally
      event.preventDefault();
      var context = menuContext;
      closeTrackMenu();
      if (!context) return;

      if (item.hasAttribute("data-menu-play") && context.row) {
        playFromElement(context.row);
      } else if (item.hasAttribute("data-menu-queue") && context.row) {
        enqueueRow(context.row);
      } else if (item.hasAttribute("data-menu-copy")) {
        copyToClipboard(new URL(context.pageUrl || window.location.href, window.location.origin).href);
      }
    });

    document.addEventListener("click", function (event) {
      if (el.menu.hidden) return;
      if (event.target.closest("#track-menu") || event.target.closest("[data-menu-track]")) return;
      closeTrackMenu();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !el.menu.hidden) closeTrackMenu();
    });
    window.addEventListener("resize", closeTrackMenu);
    window.addEventListener("scroll", closeTrackMenu, true);
  }

  /* Append a single row's track to the queue without interrupting playback;
   * hydrate first so the entry has a real audio source. */
  function enqueueRow(row) {
    var track = trackFromRow(row);
    if (!track.id) return;
    hydrate([track]).then(function (tracks) {
      if (!tracks.length) return;
      state.queue.push(tracks[0]);
      renderQueue();
      updateQueueCount();
      show();
    });
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(function () {});
    }
  }

  /* --- play intents from the page ----------------------------------------- */

  /* "Play" on a row / card / hero starts that track and queues its section. */
  function playFromElement(node) {
    var id = node.dataset.play || node.dataset.trackId;
    if (!id) return;

    var scope = node.closest(".shelf, .hero, .detail, .panel") || document;
    var rows = collectPageQueue(scope);
    if (!rows.length) rows = collectPageQueue();

    // The clicked button is not itself a row, so make sure its track is part
    // of the queue even when no table on the page contains it.
    var clicked = trackFromButton(node);
    if (clicked && clicked.id) {
      var present = rows.some(function (track) { return track.id === clicked.id; });
      if (!present) rows.unshift(clicked);
    }

    hydrate(rows).then(function (tracks) {
      if (!tracks.length) return;

      var target = tracks.findIndex(function (track) { return track.id === id; });
      // Fall back to the front rather than silently playing an unrelated track.
      if (target === -1) target = 0;

      setQueue(tracks, target);
      playAt(target, true);
    });
  }

  /* Play an entire album/playlist. The card carries no track rows, so the queue
   * comes from the server: it knows the authoritative order (album track_number,
   * playlist position) and enforces playlist privacy. */
  function playCollection(node) {
    var slug = node.dataset.playAlbum;
    if (!slug) return;
    var kind = node.dataset.playKind || (node.dataset.playPlaylist ? "playlist" : "album");

    fetch(MANIFEST_COLLECTION_URL + "?kind=" + encodeURIComponent(kind) +
          "&slug=" + encodeURIComponent(slug), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        if (!payload || !payload.tracks || !payload.tracks.length) return;
        var tracks = payload.tracks.map(function (row) {
          return {
            id: row.id,
            title: row.title,
            artist: row.artist,
            artistSlug: row.artist_slug,
            url: row.url,
            hlsUrl: row.hls_url || "",
            cover: row.cover,
            duration: row.duration_seconds || 0,
            pageUrl: row.track_slug ? "/tracks/" + row.track_slug + "/" : ""
          };
        });
        setQueue(tracks, 0);
        playAt(0, true);
      })
      .catch(function () { /* leave the current queue alone on failure */ });
  }

  function playArtistTop(node) {
    var section = node.closest(".shelf") || document;
    var rows = collectPageQueue(section);
    if (!rows.length) return;
    hydrate(rows).then(function (tracks) {
      setQueue(tracks, 0);
      playAt(0, true);
    });
  }

  /* --- keyboard ----------------------------------------------------------- */

  function onKeydown(event) {
    var tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    switch (event.key) {
      case " ":
        event.preventDefault();
        togglePlay();
        break;
      case "ArrowRight":
        if (event.shiftKey) { step(1); } else if (audio) { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 5); }
        break;
      case "ArrowLeft":
        if (event.shiftKey) { step(-1); } else if (audio) { audio.currentTime = Math.max(0, audio.currentTime - 5); }
        break;
      case "ArrowUp":
        event.preventDefault();
        setVolume(state.volume + 0.05);
        break;
      case "ArrowDown":
        event.preventDefault();
        setVolume(state.volume - 0.05);
        break;
      case "m":
      case "M":
        toggleMute();
        break;
      case "s":
      case "S":
        state.shuffle = !state.shuffle;
        savePrefs();
        renderShuffle();
        break;
      case "r":
      case "R":
        cycleRepeat();
        break;
      default:
        break;
    }
  }

  function setVolume(value) {
    state.volume = Math.min(1, Math.max(0, value));
    audio.volume = state.volume;
    if (state.volume > 0 && state.muted) toggleMute();
    if (el.volume) el.volume.value = String(Math.round(state.volume * 100));
    savePrefs();
  }

  function toggleMute() {
    state.muted = !state.muted;
    audio.muted = state.muted;
    if (el.mute) {
      var iconVol = el.mute.querySelector(".icon-vol");
      var iconMute = el.mute.querySelector(".icon-mute");
      if (iconVol) iconVol.hidden = state.muted;
      if (iconMute) iconMute.hidden = !state.muted;
      el.mute.setAttribute("aria-label", state.muted ? "Unmute" : "Mute");
    }
  }

  function renderShuffle() {
    [el.shuffle, el.npShuffle].forEach(function (button) {
      if (button) button.setAttribute("aria-pressed", state.shuffle ? "true" : "false");
    });
  }

  /* --- init --------------------------------------------------------------- */

  function cacheElements() {
    el.player = $("player");
    el.audio = $("player-audio");
    el.toggle = $("player-toggle");
    el.next = $("player-next");
    el.prev = $("player-prev");
    el.shuffle = $("player-shuffle");
    el.repeat = $("player-repeat");
    el.repeatOne = $("player-repeat-one");
    el.mute = $("player-mute");
    el.volume = $("player-volume");
    el.seek = $("player-seek");
    el.fill = $("player-fill");
    el.buffer = $("player-buffer");
    el.elapsed = $("player-elapsed");
    el.duration = $("player-duration");
    el.art = $("player-art");
    el.artFallback = $("player-art-fallback");
    el.title = $("player-title");
    el.artist = $("player-artist");
    el.like = $("player-like");
    el.queue = $("player-queue");
    el.queueList = $("queue-list");
    el.queueToggle = $("player-queue-toggle");
    el.queueCount = $("player-queue-count");
    el.queueSource = $("queue-source");

    // Now-playing (full-screen) surface.
    el.np = $("now-playing");
    el.npOpen = $("player-expand");
    el.npClose = $("np-close");
    el.npToggle = $("np-toggle");
    el.npPrev = $("np-prev");
    el.npNext = $("np-next");
    el.npShuffle = $("np-shuffle");
    el.npRepeat = $("np-repeat");
    el.npRepeatOne = $("np-repeat-one");
    el.npSeek = $("np-seek");
    el.npFill = $("np-fill");
    el.npBuffer = $("np-buffer");
    el.npElapsed = $("np-elapsed");
    el.npDuration = $("np-duration");
    el.npArt = $("np-art");
    el.npArtFallback = $("np-art-fallback");
    el.npTitle = $("np-title");
    el.npArtist = $("np-artist");
    el.npSource = $("np-source");
    el.npLike = $("np-like");
    el.npQueueList = $("np-queue-list");
    el.npLyrics = $("np-lyrics");
    el.menu = $("track-menu");
  }

  function bindAudio() {
    audio = el.audio;
    if (!audio) return;

    audio.volume = state.volume;
    audio.preload = "metadata";

    audio.addEventListener("loadedmetadata", function () {
      if (el.duration) el.duration.textContent = formatTime(audio.duration);
    });
    audio.addEventListener("timeupdate", function () { syncSeek(); maybeReportPlay(); syncLyrics(); });
    audio.addEventListener("progress", syncSeek);
    audio.addEventListener("play", syncPlayState);
    audio.addEventListener("pause", syncPlayState);
    audio.addEventListener("ended", function () {
      maybeReportPlay(true);
      handleEnded();
    });
    audio.addEventListener("error", function () {
      // A broken source should not wedge the transport: drop it and move on.
      if (!current()) return;
      var track = current();
      console.warn("Playback failed for", track.title);
      var next = nextIndex(1);
      if (next !== -1 && next !== state.index) playAt(next, true);
      else syncPlayState();
    });
  }

  function bindControls() {
    if (el.toggle) el.toggle.addEventListener("click", togglePlay);
    if (el.next) el.next.addEventListener("click", function () { step(1); });
    if (el.prev) el.prev.addEventListener("click", function () { step(-1); });
    if (el.repeat) el.repeat.addEventListener("click", cycleRepeat);
    if (el.shuffle) el.shuffle.addEventListener("click", function () {
      state.shuffle = !state.shuffle;
      savePrefs();
      renderShuffle();
    });
    if (el.mute) el.mute.addEventListener("click", toggleMute);
    if (el.like) el.like.addEventListener("click", function () {
      var track = current();
      if (track) toggleFavorite(track.id, el.like);
    });

    var topbarBack = $("topbar-back");
    if (topbarBack) topbarBack.addEventListener("click", function () { window.history.back(); });
    var topbarForward = $("topbar-forward");
    if (topbarForward) topbarForward.addEventListener("click", function () { window.history.forward(); });

    // Full-screen now-playing transport mirrors the bar's controls.
    if (el.npOpen) el.npOpen.addEventListener("click", openNowPlaying);
    if (el.npClose) el.npClose.addEventListener("click", closeNowPlaying);
    if (el.npToggle) el.npToggle.addEventListener("click", togglePlay);
    if (el.npPrev) el.npPrev.addEventListener("click", function () { step(-1); });
    if (el.npNext) el.npNext.addEventListener("click", function () { step(1); });
    if (el.npRepeat) el.npRepeat.addEventListener("click", cycleRepeat);
    if (el.npShuffle) el.npShuffle.addEventListener("click", function () {
      state.shuffle = !state.shuffle;
      savePrefs();
      renderShuffle();
    });
    if (el.npLike) el.npLike.addEventListener("click", function () {
      var track = current();
      if (track) toggleFavorite(track.id, el.npLike);
    });

    if (el.volume) {
      el.volume.value = String(Math.round(state.volume * 100));
      el.volume.addEventListener("input", function () {
        setVolume(parseInt(el.volume.value, 10) / 100);
      });
    }

    bindSeek(el.seek, el.fill);
    bindSeek(el.npSeek, el.npFill);

    if (el.queueToggle) {
      el.queueToggle.addEventListener("click", function () { toggleQueuePanel(); });
    }
    var queueClose = $("queue-close");
    if (queueClose) queueClose.addEventListener("click", function () { toggleQueuePanel(false); });
    var queueClear = $("queue-clear");
    if (queueClear) queueClear.addEventListener("click", function () {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      setQueue([], -1);
      if (el.title) el.title.textContent = "Nothing playing";
      if (el.artist) el.artist.textContent = "";
      if (el.player) el.player.hidden = true;
      document.body.classList.remove("has-player");
      if (state.npOpen) closeNowPlaying();
      markActiveRow();
    });

    // Escape closes the now-playing view from anywhere, including focus on a
    // range input (which the global shortcut handler deliberately ignores).
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && state.npOpen) closeNowPlaying();
    });
  }

  /* Wire a seek range input to the audio element, keeping the matching fill bar
   * in step while the user drags. */
  function bindSeek(input, fill) {
    if (!input) return;
    var apply = function () {
      if (!audio || !isFinite(audio.duration) || audio.duration <= 0) return;
      var ratio = parseInt(input.value, 10) / SEEK_STEPS;
      audio.currentTime = ratio * audio.duration;
      if (fill) fill.style.width = (ratio * 100) + "%";
    };
    input.addEventListener("input", function () { state.seeking = true; apply(); });
    input.addEventListener("change", function () { apply(); state.seeking = false; });
  }

  /* One delegated listener covers every play/favourite button on the page,
   * including rows injected by pagination or the queue drawer. */
  function bindDelegates() {
    document.addEventListener("click", function (event) {
      var menuButton = event.target.closest("[data-menu-track]");
      if (menuButton) {
        event.preventDefault();
        openTrackMenu(menuButton);
        return;
      }
      var playButton = event.target.closest("[data-play]");
      if (playButton) {
        event.preventDefault();
        playFromElement(playButton);
        return;
      }
      var collection = event.target.closest("[data-play-album]");
      if (collection) {
        event.preventDefault();
        playCollection(collection);
        return;
      }
      var artistTop = event.target.closest("[data-play-artist]");
      if (artistTop) {
        event.preventDefault();
        playArtistTop(artistTop);
        return;
      }
      var favorite = event.target.closest("[data-favorite]");
      if (favorite) {
        event.preventDefault();
        toggleFavorite(favorite.dataset.favorite, favorite);
        return;
      }
      var followArtist = event.target.closest("[data-follow-artist]");
      if (followArtist) {
        event.preventDefault();
        toggleFollow("artist", followArtist.dataset.followArtist, followArtist);
        return;
      }
      var followPlaylist = event.target.closest("[data-follow-playlist]");
      if (followPlaylist) {
        event.preventDefault();
        toggleFollow("playlist", followPlaylist.dataset.followPlaylist, followPlaylist);
        return;
      }
      var followUser = event.target.closest("[data-follow-user]");
      if (followUser) {
        event.preventDefault();
        toggleFollow("user", followUser.dataset.followUser, followUser);
      }
    });
  }

  function bindMediaKeys() {
    if (!("mediaSession" in navigator)) return;
    try {
      navigator.mediaSession.setActionHandler("play", function () { togglePlay(); });
      navigator.mediaSession.setActionHandler("pause", function () { togglePlay(); });
      navigator.mediaSession.setActionHandler("previoustrack", function () { step(-1); });
      navigator.mediaSession.setActionHandler("nexttrack", function () { step(1); });
    } catch (err) { /* handler unsupported on this platform */ }
  }

  /* --- notifications ------------------------------------------------------ */

  /* The server renders the badge with whatever count it has; this refreshes it
   * after load so a long-lived tab stays roughly in sync. The endpoint is a
   * cheap COUNT and failures are silently ignored. */
  function bindNotifications() {
    var badge = document.querySelector("[data-notif-badge]");
    if (!badge) return;
    fetch("/api/notifications/unread_count/", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        if (!payload) return;
        var count = payload.unread || 0;
        badge.textContent = count > 99 ? "99+" : String(count);
        badge.hidden = count === 0;
      })
      .catch(function () { /* offline: keep whatever the server rendered */ });
  }

  function syncMediaSession() {
    if (!("mediaSession" in navigator) || !navigator.mediaSession.metadata) return;
    var track = current();
    if (!track) return;
    navigator.mediaSession.metadata = new window.MediaMetadata({
      title: track.title,
      artist: track.artist,
      artwork: track.cover ? [{ src: track.cover, sizes: "512x512", type: "image/jpeg" }] : []
    });
    if (navigator.mediaSession.setPositionState && isFinite(audio.duration) && audio.duration > 0) {
      try {
        navigator.mediaSession.setPositionState({
          duration: audio.duration,
          position: Math.min(audio.currentTime, audio.duration),
          playbackRate: audio.playbackRate
        });
      } catch (err) { /* position state can reject transiently */ }
    }
  }

  /* --- synced lyrics ------------------------------------------------------ */

  /* Highlight the lyric line matching the playhead, in every lyrics scope on
   * the page (the track page panel and the now-playing panel). A scope only
   * highlights while the playing track is the one it was rendered for;
   * otherwise the highlight clears so a stale line never sticks. */
  function syncLyrics() {
    document.querySelectorAll("[data-lyrics-scope]").forEach(syncLyricsPanel);
  }

  function syncLyricsPanel(panel) {
    var lines = panel.querySelectorAll("[data-lyrics-time]");
    if (!lines.length) return;

    var track = current();
    var active = -1;
    if (track && String(track.id) === panel.dataset.lyricsTrackId) {
      var time = audio ? audio.currentTime : 0;
      for (var i = 0; i < lines.length; i++) {
        if (parseFloat(lines[i].dataset.lyricsTime) <= time + 0.15) active = i;
        else break;
      }
    }

    for (var j = 0; j < lines.length; j++) {
      var isActive = j === active;
      lines[j].classList.toggle("is-active", isActive);
      if (isActive) lines[j].setAttribute("aria-current", "true");
      else lines[j].removeAttribute("aria-current");
    }

    if (active !== -1 && panel.classList.contains("lyrics--synced")) {
      lines[active].scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  /* --- theme -------------------------------------------------------------- */

  var THEME_KEY = "auralis.theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  /* Paint the toggle to match the active theme: the icon shows the theme you
   * would switch *to* (a sun while dark, a moon while light). */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var button = $("theme-toggle");
    if (!button) return;
    var light = theme === "light";
    button.setAttribute("aria-pressed", light ? "true" : "false");
    button.setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
    var sun = button.querySelector(".icon-sun");
    var moon = button.querySelector(".icon-moon");
    if (sun) sun.hidden = light;
    if (moon) moon.hidden = !light;
  }

  function bindTheme() {
    var button = $("theme-toggle");
    if (!button) return;
    applyTheme(currentTheme());
    button.addEventListener("click", function () {
      var next = currentTheme() === "light" ? "dark" : "light";
      try { window.localStorage.setItem(THEME_KEY, next); } catch (err) { /* non-fatal */ }
      applyTheme(next);
    });
  }

  /* --- live search -------------------------------------------------------- */

  var SEARCH_URL = "/api/search/";
  var SEARCH_LIMIT = 6;
  var searchState = { controller: null, timer: 0, query: "" };

  function bindSearch() {
    var form = $("site-search-form");
    var input = $("site-search");
    var panel = $("search-results");
    if (!form || !input || !panel) return;

    input.addEventListener("input", function () {
      window.clearTimeout(searchState.timer);
      var query = input.value.trim();
      searchState.timer = window.setTimeout(function () { runSearch(query, input, panel); }, 220);
    });
    input.addEventListener("focus", function () {
      if (panel.childElementCount && input.value.trim().length >= 2) openSearchPanel(input, panel);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeSearchPanel(input, panel);
        input.blur();
      }
    });
    document.addEventListener("click", function (event) {
      if (!form.contains(event.target)) closeSearchPanel(input, panel);
    });
    form.addEventListener("submit", function () { closeSearchPanel(input, panel); });
  }

  function openSearchPanel(input, panel) {
    panel.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function closeSearchPanel(input, panel) {
    panel.hidden = true;
    if (input) input.setAttribute("aria-expanded", "false");
  }

  function runSearch(query, input, panel) {
    if (query.length < 2) {
      panel.textContent = "";
      closeSearchPanel(input, panel);
      return;
    }
    if (searchState.controller) searchState.controller.abort();
    searchState.controller = ("AbortController" in window) ? new AbortController() : null;
    searchState.query = query;

    fetch(SEARCH_URL + "?q=" + encodeURIComponent(query) + "&limit=" + SEARCH_LIMIT, {
      credentials: "same-origin",
      signal: searchState.controller ? searchState.controller.signal : undefined
    })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (payload) {
        // Drop a response whose query is no longer the one in the box.
        if (!payload || payload.query !== searchState.query) return;
        renderSearchResults(payload, panel);
        openSearchPanel(input, panel);
      })
      .catch(function () { /* aborted or offline: leave the panel as-is */ });
  }

  function renderSearchResults(payload, panel) {
    panel.textContent = "";
    var results = payload.results || {};
    var specs = [
      ["tracks", "Tracks", renderTrackResult],
      ["artists", "Artists", renderArtistResult],
      ["albums", "Albums", renderAlbumResult],
      ["genres", "Genres", renderGenreResult],
      ["playlists", "Playlists", renderPlaylistResult]
    ];
    var any = false;
    specs.forEach(function (spec) {
      var rows = results[spec[0]] || [];
      if (!rows.length) return;
      any = true;
      panel.appendChild(groupHeading(spec[1]));
      rows.forEach(function (row) { panel.appendChild(spec[2](row)); });
    });

    if (!any) {
      var empty = document.createElement("p");
      empty.className = "sr-empty";
      empty.textContent = "No results for \u201c" + payload.query + "\u201d";
      panel.appendChild(empty);
      return;
    }

    var all = document.createElement("a");
    all.className = "sr-all";
    all.href = "/search/?q=" + encodeURIComponent(payload.query);
    all.textContent = "See all results for \u201c" + payload.query + "\u201d";
    panel.appendChild(all);
  }

  function groupHeading(text) {
    var node = document.createElement("div");
    node.className = "sr-group";
    node.textContent = text;
    return node;
  }

  function artThumb(url, fallback, round) {
    var wrap = document.createElement("span");
    wrap.className = "sr-item__art" + (round ? " sr-item__art--round" : "");
    if (url) {
      var img = document.createElement("img");
      img.src = url;
      img.alt = "";
      img.loading = "lazy";
      wrap.appendChild(img);
    } else {
      wrap.classList.add("art", "art--placeholder");
      wrap.textContent = fallback;
    }
    return wrap;
  }

  function resultItem(spec) {
    var item = document.createElement("div");
    item.className = "sr-item";
    if (spec.art !== undefined) item.appendChild(artThumb(spec.art, spec.fallback, spec.round));

    var body = document.createElement("div");
    body.className = "sr-item__body";
    if (spec.href) {
      var link = document.createElement("a");
      link.className = "sr-item__label";
      link.href = spec.href;
      link.textContent = spec.title;
      body.appendChild(link);
    } else {
      var span = document.createElement("span");
      span.className = "sr-item__label";
      span.textContent = spec.title;
      body.appendChild(span);
    }
    if (spec.subtitle) {
      var sub = document.createElement("span");
      sub.className = "sr-item__sub";
      sub.textContent = spec.subtitle;
      body.appendChild(sub);
    }
    item.appendChild(body);
    if (spec.play) item.appendChild(spec.play);
    return item;
  }

  function playButton(label, data) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "sr-item__play";
    button.setAttribute("aria-label", label);
    Object.keys(data).forEach(function (key) { button.dataset[key] = data[key]; });
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
    return button;
  }

  function renderTrackResult(track) {
    var artist = track.artist || {};
    return resultItem({
      art: track.cover_image,
      fallback: "\u266b",
      href: "/tracks/" + track.slug + "/",
      title: track.title,
      subtitle: [artist.name, track.duration_display].filter(Boolean).join(" \u00b7 "),
      play: playButton("Play " + track.title, {
        play: track.id,
        trackId: track.id,
        trackTitle: track.title,
        trackArtist: artist.name || "",
        trackArtistSlug: artist.slug || "",
        trackUrl: "/tracks/" + track.slug + "/",
        trackDuration: track.duration_seconds || 0,
        trackCover: track.cover_image || ""
      })
    });
  }

  function renderArtistResult(artist) {
    return resultItem({
      art: artist.image,
      fallback: (artist.name || "?").charAt(0).toUpperCase(),
      round: true,
      href: "/artists/" + artist.slug + "/",
      title: artist.name,
      subtitle: "Artist" + (artist.track_count ? " \u00b7 " + artist.track_count + " tracks" : ""),
      play: playButton("Play " + artist.name, { playAlbum: artist.slug, playKind: "artist" })
    });
  }

  function renderAlbumResult(album) {
    var artist = album.artist || {};
    return resultItem({
      art: album.cover_image,
      fallback: "\u25c8",
      href: "/albums/" + album.slug + "/",
      title: album.title,
      subtitle: ["Album", artist.name].filter(Boolean).join(" \u00b7 "),
      play: playButton("Play " + album.title, { playAlbum: album.slug, playKind: "album" })
    });
  }

  function renderGenreResult(genre) {
    return resultItem({
      art: null,
      fallback: "\u25c8",
      href: "/genres/" + genre.slug + "/",
      title: genre.name,
      subtitle: "Genre" + (genre.track_count ? " \u00b7 " + genre.track_count + " tracks" : "")
    });
  }

  function renderPlaylistResult(playlist) {
    return resultItem({
      art: playlist.cover_image,
      fallback: "\u2263",
      href: "/playlists/" + playlist.slug + "/",
      title: playlist.name,
      subtitle: "Playlist" + (playlist.owner ? " \u00b7 " + playlist.owner.display_name : ""),
      play: playButton("Play " + playlist.name, { playAlbum: playlist.slug, playKind: "playlist" })
    });
  }

  function init() {
    cacheElements();
    if (!el.audio) return;

    loadPrefs();
    bindAudio();
    bindControls();
    bindDelegates();
    bindMenu();
    bindNpTabs();
    bindTheme();
    bindSearch();
    bindMediaKeys();
    bindNotifications();

    audio.addEventListener("loadedmetadata", syncMediaSession);
    audio.addEventListener("timeupdate", syncMediaSession);

    renderRepeat();
    renderShuffle();
    renderQueue();

    document.addEventListener("keydown", onKeydown);

    // Expose a tiny surface for debugging without leaking internals.
    window.AuralisPlayer = {
      state: state,
      playAt: playAt,
      playFromElement: playFromElement,
      queue: function () { return state.queue.slice(); }
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

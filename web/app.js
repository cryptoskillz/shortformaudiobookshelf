/* Shortform Audio Bookshelf — browser player.
 *
 * A book is a list of tracks with known offsets, so the scrubber can span the
 * whole book rather than one chapter: seeking maps a book-wide position back to
 * (track, offset within track). Files whose duration we cannot read without
 * decoding (mp3/m4b) fall back to chapter-level scrubbing.
 */

const $ = (id) => document.getElementById(id);

const el = {
  heading: $("heading"), crumb: $("crumb"), back: $("back"), search: $("search"), rescan: $("rescan"),
  libraryView: $("library-view"), libraryMeta: $("library-meta"), grid: $("grid"), empty: $("empty"),
  filterAuthor: $("filter-author"), filterGenre: $("filter-genre"),
  filterState: $("filter-state"), filterClear: $("filter-clear"),
  bookView: $("book-view"), bookCover: $("book-cover"), bookTitle: $("book-title"),
  bookAuthor: $("book-author"), bookMeta: $("book-meta"), bookDesc: $("book-desc"),
  bookPath: $("book-path"), bookSource: $("book-source"), tracks: $("tracks"),
  findMetadata: $("find-metadata"), metadata: $("metadata"),
  metaTitle: $("meta-title"), metaAuthor: $("meta-author"), metaSearch: $("meta-search"),
  metaResults: $("meta-results"), metaNote: $("meta-note"), metaStatus: $("meta-status"),
  metaClear: $("meta-clear"), metaClose: $("meta-close"), home: $("home"),
  authorBio: $("author-bio"), authorBioName: $("author-bio-name"),
  authorBioText: $("author-bio-text"), authorBioSource: $("author-bio-source"),
  authorSearch: $("author-search"), authorResults: $("author-results"),
  authorNote: $("author-note"), authorCurrent: $("author-current"),
  srcAudible: $("src-audible"), srcItunes: $("src-itunes"), srcGoogle: $("src-google"),
  playBook: $("play-book"), restartBook: $("restart-book"), downloadPlaylist: $("download-playlist"),
  player: $("player"), audio: $("audio"), seek: $("seek"), timeNow: $("time-now"), timeLeft: $("time-left"),
  playpause: $("playpause"), prev: $("prev"), next: $("next"), back15: $("back15"), fwd30: $("fwd30"),
  nowTitle: $("now-title"), nowChapter: $("now-chapter"), nowCover: $("now-cover"),
  speed: $("speed"), sleep: $("sleep"), toast: $("toast"),

  app: $("app"), signin: $("signin"), signinForm: $("signin-form"),
  loginUsername: $("login-username"), loginPassword: $("login-password"),
  loginSubmit: $("login-submit"), loginError: $("login-error"), signinHint: $("signin-hint"),
  defaultPasswordWarning: $("default-password-warning"),
  containerNote: $("container-note"), libraryWritable: $("library-writable"),
  outputWritable: $("output-writable"), uploadBlocked: $("upload-blocked"), stateWarning: $("state-warning"), stateWarningDetail: $("state-warning-detail"),
  signinState: $("signin-state"),
  menuToggle: $("menu-toggle"), userMenu: $("user-menu"), menuWho: $("menu-who"),
  signout: $("signout"), settingsOpen: $("settings-open"), settings: $("settings"), settingsSave: $("settings-save"),
  settingsCancel: $("settings-cancel"), settingsStatus: $("settings-status"),
  settingsPaths: $("settings-paths"), authWarning: $("auth-warning"), authStatus: $("auth-status"),
  setLibrary: $("set-library"), setOutput: $("set-output"), setHost: $("set-host"), setPort: $("set-port"),
  userList: $("user-list"), addUserBox: $("add-user-box"), addUser: $("add-user"),
  newUsername: $("new-username"), newPassword: $("new-password"), newRole: $("new-role"),
  ownCurrent: $("own-current"), ownCurrentField: $("own-current-field"),
  ownPassword: $("own-password"), ownConfirm: $("own-confirm"), changeOwn: $("change-own"),
  verifyPassword: $("verify-password"), verifyResult: $("verify-result"), matchResult: $("match-result"),
  accountsSection: $("accounts-section"), organiseMove: $("organise-move"),

  fetchAll: $("fetch-all"), fetchStop: $("fetch-stop"),
  fetchProgress: $("fetch-progress"), fetchResult: $("fetch-result"),

  setImport: $("set-import"), importMove: $("import-move"),
  importPreview: $("import-preview"), importRun: $("import-run"),
  importProgress: $("import-progress"), importResult: $("import-result"),

  organisePreview: $("organise-preview"), organiseRun: $("organise-run"),
  organisePlaylists: $("organise-playlists"), organiseProgress: $("organise-progress"),
  organiseResult: $("organise-result"),

  picker: $("picker"), pickerTitle: $("picker-title"), pickerPath: $("picker-path"),
  pickerUp: $("picker-up"), pickerGo: $("picker-go"), pickerList: $("picker-list"),
  pickerShortcuts: $("picker-shortcuts"), pickerInfo: $("picker-info"),
  pickerStatus: $("picker-status"), pickerChoose: $("picker-choose"), pickerCancel: $("picker-cancel"),

  removeBook: $("remove-book"), removeDialog: $("remove"), removeWhat: $("remove-what"),
  removeFiles: $("remove-files"), removeWarning: $("remove-warning"), removeList: $("remove-list"),
  removeCancel: $("remove-cancel"), removeConfirm: $("remove-confirm"), removeStatus: $("remove-status"),

  uploadOpen: $("upload-open"), upload: $("upload"), uploadClose: $("upload-close"),
  dropzone: $("dropzone"), chooseFiles: $("choose-files"), fileInput: $("file-input"),
  uploadList: $("upload-list"), uploadStatus: $("upload-status"), uploadTarget: $("upload-target"),
};

const state = {
  books: [],          // library summaries
  progress: {},       // bookId -> {track, position, finished}
  viewing: null,      // book detail currently on screen
  playing: null,      // book detail currently loaded into <audio>
  trackIndex: 0,
  scrubbing: false,
  sleepAt: 0,         // epoch ms, 0 = off
  sleepAfterChapter: false,
  lastSaved: 0,
};

/* ---------------------------------------------------------------- helpers */

const api = async (path, options) => {
  const response = await fetch(path, options);
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || `${response.status} ${response.statusText}`);
  }
  return response.json();
};

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

function toast(message) {
  el.toast.textContent = message;
  el.toast.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.toast.hidden = true; }, 2600);
}

function coverMarkup(book, className = "") {
  if (book.hasCover) {
    return `<img src="/cover/${book.id}" alt="" loading="lazy" class="${className}">`;
  }
  const label = (book.title || "").slice(0, 60);
  return `<div class="placeholder">${escapeHtml(label)}</div>`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Fraction 0–1 of a book already listened to, from the saved resume point.
 *  The server sends this pre-computed, because only it knows chapter offsets. */
function progressFraction(book) {
  const saved = state.progress[book.id];
  if (!saved) return 0;
  if (saved.finished) return 1;
  return typeof saved.fraction === "number" ? saved.fraction : 0;
}

/* ------------------------------------------------------------ library view */

/** Fill a filter dropdown with the values actually present in the library,
 *  keeping the current selection even if it is momentarily filtered out. */
function fillFilter(select, values, label) {
  const chosen = select.value;
  const sorted = [...new Set(values.filter(Boolean))].sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: "base" }));
  select.innerHTML =
    `<option value="">All ${label}</option>` +
    sorted.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  select.value = sorted.includes(chosen) ? chosen : "";
  select.parentElement.hidden = sorted.length === 0;
}

function renderFilters() {
  fillFilter(el.filterAuthor, state.books.map((b) => b.author), "authors");
  fillFilter(el.filterGenre, state.books.map((b) => b.genre), "genres");
}

function matchesFilters(book) {
  const query = el.search.value.trim().toLowerCase();
  if (query && !`${book.title} ${book.author} ${book.narrator} ${book.series} ${book.genre}`
      .toLowerCase().includes(query)) return false;
  if (el.filterAuthor.value && book.author !== el.filterAuthor.value) return false;
  if (el.filterGenre.value && book.genre !== el.filterGenre.value) return false;

  const saved = state.progress[book.id];
  switch (el.filterState.value) {
    case "inprogress": return Boolean(saved) && !saved.finished;
    case "unstarted": return !saved;
    case "finished": return Boolean(saved && saved.finished);
    default: return true;
  }
}

function renderLibrary() {
  const filtering = Boolean(el.search.value.trim() || el.filterAuthor.value ||
                            el.filterGenre.value || el.filterState.value);
  const books = state.books.filter(matchesFilters);
  el.filterClear.hidden = !filtering;

  el.libraryMeta.textContent = state.books.length
    ? `${state.books.length} book${state.books.length === 1 ? "" : "s"}` +
      (filtering ? ` · ${books.length} shown` : "")
    : "";

  el.grid.innerHTML = books.map((book) => {
    const saved = state.progress[book.id];
    const fraction = progressFraction(book);
    const badge = saved && saved.finished
      ? '<span class="badge">Done</span>'
      : saved ? '<span class="badge">Resume</span>' : "";
    const bar = fraction > 0 && fraction < 1
      ? `<div class="bar"><span style="width:${(fraction * 100).toFixed(1)}%"></span></div>`
      : "";
    return `
      <button class="card" data-id="${book.id}">
        <div class="art">${coverMarkup(book)}${badge}${bar}</div>
        <span class="title">${escapeHtml(book.title)}</span>
        <span class="sub">${escapeHtml(book.author)}</span>
        <span class="sub">${escapeHtml(book.durationText)}${book.trackCount > 1 ? ` · ${book.trackCount} chapters` : ""}</span>
      </button>`;
  }).join("");

  el.empty.hidden = books.length > 0;
  el.empty.textContent = state.books.length
    ? "Nothing matches those filters."
    : (state.libraryMissing
        ? `The library folder does not exist: ${state.libraryRoot}. ` +
          "Set it under Settings, or check the folder is mounted."
        : "No audiobooks found. Check the library directory, then press Rescan.");
}

async function loadLibrary() {
  const data = await api("/api/library");
  state.books = data.books;
  state.progress = data.progress || {};
  state.libraryRoot = data.root;
  document.title = "Shortform Audio Bookshelf";
  renderFilters();
  renderLibrary();
  try {
    const who = await api("/api/users");
    applyRole(who.you, who.accountsConfigured);
  } catch {
    /* older server or not signed in yet; leave the UI as-is */
  }
}

/* --------------------------------------------------------------- book view */

function showLibrary() {
  el.bookView.hidden = true;
  el.libraryView.hidden = false;
  el.back.hidden = true;
  el.crumb.textContent = "";
  state.viewing = null;
  renderLibrary();
}

async function showBook(bookId) {
  const book = await api(`/api/books/${bookId}`);
  state.viewing = book;
  if (book.progress) state.progress[book.id] = book.progress;

  el.libraryView.hidden = true;
  el.bookView.hidden = false;
  el.back.hidden = false;
  el.crumb.textContent = book.title;

  el.bookCover.innerHTML = coverMarkup(book);
  el.bookTitle.textContent = book.title;
  // The author is a link back to the library filtered to just their books.
  el.bookAuthor.innerHTML = book.author
    ? `<a href="#" class="author-link" data-author="${escapeHtml(book.author)}">${escapeHtml(book.author)}</a>`
    : "";

  const bits = [book.durationText, `${book.trackCount} file${book.trackCount === 1 ? "" : "s"}`];
  if (book.narrator) bits.push(`Read by ${book.narrator}`);
  if (book.genre) bits.push(book.genre);
  if (book.series) bits.push(book.series);
  if (book.year) bits.push(book.year);
  if (book.untimed) bits.push("duration unknown");
  el.bookMeta.textContent = bits.join(" · ");

  el.bookDesc.textContent = book.description || "";
  el.bookSource.innerHTML = book.metadata
    ? `Details from ${escapeHtml(book.metadata.provider)}` +
      (book.metadata.matchedTitle && book.metadata.matchedTitle !== book.title
        ? ` — matched as “${escapeHtml(book.metadata.matchedTitle)}”` : "") +
      (book.metadata.link ? ` · <a href="${escapeHtml(book.metadata.link)}" target="_blank" rel="noopener">view</a>` : "")
    : "";
  el.bookPath.textContent = book.directory;

  const bio = book.authorBio;
  el.authorBio.hidden = !bio;
  if (bio) {
    el.authorBioName.textContent = `About ${bio.name || book.author}`;
    el.authorBioText.textContent = bio.bio;
    const life = [bio.born, bio.died].filter(Boolean).join(" – ");
    el.authorBioSource.innerHTML =
      [life, `from ${escapeHtml(bio.provider)}`].filter(Boolean).join(" · ") +
      (bio.link ? ` · <a href="${escapeHtml(bio.link)}" target="_blank" rel="noopener">view</a>` : "");
  }
  el.downloadPlaylist.href = `/api/books/${book.id}/playlist.m3u`;

  const saved = state.progress[book.id];
  const resumable = saved && !saved.finished && (saved.track > 0 || saved.position > 5);
  el.playBook.textContent = saved && saved.finished
    ? "Play again"
    : resumable
      ? `Resume · ${book.trackCount > 1 ? `ch ${saved.track + 1}, ` : ""}${formatTime(saved.position)}`
      : "Play";
  el.restartBook.hidden = !saved;

  renderTracks();
}

function renderTracks() {
  const book = state.viewing;
  if (!book) return;
  const isPlaying = state.playing && state.playing.id === book.id;
  el.tracks.innerHTML = book.tracks.map((track) => `
    <li class="track ${isPlaying && track.index === state.trackIndex ? "active" : ""}" data-index="${track.index}">
      <span class="num">${track.index + 1}</span>
      <span class="name">${escapeHtml(track.title)}</span>
      <span class="len">${track.duration ? formatTime(track.duration) : ""}</span>
    </li>`).join("");
}

/* ------------------------------------------------------------------ player */

async function playBook(bookOrId, trackIndex = 0, position = 0, autoplay = true) {
  const book = typeof bookOrId === "string" ? await api(`/api/books/${bookOrId}`) : bookOrId;
  state.playing = book;
  el.player.hidden = false;
  loadTrack(trackIndex, position, autoplay);
  renderTracks();
  syncPlayerHeight();  // the observer does not fire for a display:none → visible flip
}

function loadTrack(index, position = 0, autoplay = true) {
  const book = state.playing;
  if (!book) return;
  const bounded = Math.max(0, Math.min(index, book.tracks.length - 1));
  const track = book.tracks[bounded];
  state.trackIndex = bounded;

  el.audio.src = `/audio/${book.id}/${bounded}`;
  el.audio.playbackRate = parseFloat(el.speed.value) || 1;
  if (position > 0) {
    el.audio.addEventListener("loadedmetadata", () => { el.audio.currentTime = position; }, { once: true });
  }
  if (autoplay) {
    el.audio.play().catch(() => {/* autoplay blocked until the user gestures */});
  }

  el.nowTitle.textContent = book.title;
  el.nowChapter.textContent = book.tracks.length > 1
    ? `${track.title} · ${bounded + 1} of ${book.tracks.length}`
    : book.author;
  el.nowCover.innerHTML = coverMarkup(book);
  updateMediaSession(track);
  renderTracks();
  syncPlayerHeight();  // a longer chapter title can wrap the controls onto another row
}

function bookPosition() {
  const book = state.playing;
  if (!book) return { elapsed: 0, total: 0, bookWide: false };
  const track = book.tracks[state.trackIndex] || { offset: 0, duration: 0 };
  const current = el.audio.currentTime || 0;
  if (book.duration > 0 && !book.untimed) {
    return { elapsed: track.offset + current, total: book.duration, bookWide: true };
  }
  return { elapsed: current, total: el.audio.duration || 0, bookWide: false };
}

function renderProgress() {
  const { elapsed, total } = bookPosition();
  if (!state.scrubbing) {
    el.seek.value = total > 0 ? Math.round((elapsed / total) * 1000) : 0;
  }
  el.timeNow.textContent = formatTime(elapsed);
  el.timeLeft.textContent = total > 0 ? `-${formatTime(Math.max(0, total - elapsed))}` : "";
}

function seekToFraction(fraction) {
  const book = state.playing;
  if (!book) return;
  const { bookWide } = bookPosition();

  if (!bookWide) {
    if (isFinite(el.audio.duration)) el.audio.currentTime = fraction * el.audio.duration;
    return;
  }
  const target = fraction * book.duration;
  const index = book.tracks.findIndex((t, i) => {
    const next = book.tracks[i + 1];
    return target >= t.offset && (!next || target < next.offset);
  });
  const bounded = index === -1 ? book.tracks.length - 1 : index;
  const within = Math.max(0, target - book.tracks[bounded].offset);

  if (bounded === state.trackIndex) {
    el.audio.currentTime = within;
  } else {
    loadTrack(bounded, within, !el.audio.paused);
  }
}

function togglePlay() {
  if (!state.playing) return;
  if (el.audio.paused) el.audio.play().catch(() => {}); else el.audio.pause();
}

function skip(seconds) {
  const book = state.playing;
  if (!book) return;
  const target = el.audio.currentTime + seconds;
  if (target < 0 && state.trackIndex > 0) {
    const previous = book.tracks[state.trackIndex - 1];
    loadTrack(state.trackIndex - 1, Math.max(0, (previous.duration || 0) + target), !el.audio.paused);
  } else if (isFinite(el.audio.duration) && target > el.audio.duration) {
    if (state.trackIndex < book.tracks.length - 1) {
      loadTrack(state.trackIndex + 1, target - el.audio.duration, !el.audio.paused);
    } else {
      el.audio.currentTime = el.audio.duration;
    }
  } else {
    el.audio.currentTime = Math.max(0, target);
  }
}

/* ------------------------------------------------------------- persistence */

/** Persist the resume point. `beacon` is for the page-unload path only: it
 *  survives the tab closing but cannot be awaited, so anything that reads the
 *  progress back straight afterwards must use the normal fetch path. */
function saveProgress(force = false, finished = false, beacon = false) {
  const book = state.playing;
  if (!book) return Promise.resolve();
  const now = Date.now();
  if (!force && now - state.lastSaved < 8000) return Promise.resolve();
  state.lastSaved = now;

  const position = el.audio.currentTime || 0;
  const body = JSON.stringify({ track: state.trackIndex, position, finished });

  // Mirror the server's calculation so the library grid stays right without a reload.
  const offset = (book.tracks[state.trackIndex] || {}).offset || 0;
  const elapsed = offset + position;
  state.progress[book.id] = {
    track: state.trackIndex,
    position,
    finished,
    elapsed,
    fraction: finished ? 1 : (book.duration > 0 ? Math.min(1, elapsed / book.duration) : 0),
  };

  const url = `/api/progress/${book.id}`;
  if (beacon && navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    return Promise.resolve();
  }
  return fetch(url, { method: "POST", body, keepalive: true }).catch(() => {});
}

/* ------------------------------------------------------------ media keys */

function updateMediaSession(track) {
  if (!("mediaSession" in navigator)) return;
  const book = state.playing;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: book.tracks.length > 1 ? track.title : book.title,
    artist: book.author,
    album: book.title,
    artwork: book.hasCover
      ? [{ src: `/cover/${book.id}`, sizes: "512x512", type: "image/jpeg" }]
      : [],
  });
  const actions = {
    play: () => el.audio.play(),
    pause: () => el.audio.pause(),
    seekbackward: () => skip(-15),
    seekforward: () => skip(30),
    previoustrack: () => loadTrack(state.trackIndex - 1, 0, !el.audio.paused),
    nexttrack: () => loadTrack(state.trackIndex + 1, 0, !el.audio.paused),
  };
  for (const [action, handler] of Object.entries(actions)) {
    try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* unsupported */ }
  }
}

/* ------------------------------------------------------------ sleep timer */

function armSleepTimer() {
  const value = el.sleep.value;
  state.sleepAfterChapter = value === "chapter";
  state.sleepAt = /^\d+$/.test(value) && Number(value) > 0 ? Date.now() + Number(value) * 1000 : 0;
  if (state.sleepAt) toast(`Sleeping in ${Math.round(Number(value) / 60)} minutes`);
  else if (state.sleepAfterChapter) toast("Sleeping at the end of this chapter");
}

function checkSleepTimer() {
  if (state.sleepAt && Date.now() >= state.sleepAt) {
    el.audio.pause();
    state.sleepAt = 0;
    el.sleep.value = "0";
    toast("Sleep timer — paused");
  }
}

/* ----------------------------------------------------------------- events */

el.grid.addEventListener("click", (event) => {
  const card = event.target.closest(".card");
  if (card) showBook(card.dataset.id).catch((error) => toast(error.message));
});

el.tracks.addEventListener("click", (event) => {
  const row = event.target.closest(".track");
  if (!row || !state.viewing) return;
  playBook(state.viewing, Number(row.dataset.index), 0, true);
});

el.back.addEventListener("click", showLibrary);
el.home.addEventListener("click", () => {
  el.search.value = "";
  el.filterAuthor.value = el.filterGenre.value = el.filterState.value = "";
  showLibrary();
  window.scrollTo(0, 0);
});

el.bookAuthor.addEventListener("click", (event) => {
  const link = event.target.closest(".author-link");
  if (!link) return;
  event.preventDefault();
  el.search.value = "";
  el.filterGenre.value = el.filterState.value = "";
  el.filterAuthor.value = link.dataset.author;
  showLibrary();
  window.scrollTo(0, 0);
});
el.search.addEventListener("input", renderLibrary);
el.filterAuthor.addEventListener("change", renderLibrary);
el.filterGenre.addEventListener("change", renderLibrary);
el.filterState.addEventListener("change", renderLibrary);
el.filterClear.addEventListener("click", () => {
  el.search.value = "";
  el.filterAuthor.value = el.filterGenre.value = el.filterState.value = "";
  renderLibrary();
});

el.rescan.addEventListener("click", async () => {
  el.rescan.disabled = true;
  el.rescan.textContent = "Scanning…";
  try {
    const result = await api("/api/rescan", { method: "POST" });
    await loadLibrary();
    showLibrary();
    toast(`${result.count} books · ${result.tracks} files · ${result.seconds}s`);
  } catch (error) {
    toast(error.message);
  } finally {
    el.rescan.disabled = false;
    el.rescan.textContent = "Rescan";
  }
});

el.playBook.addEventListener("click", () => {
  const book = state.viewing;
  if (!book) return;
  const saved = state.progress[book.id];
  const resume = saved && !saved.finished ? saved : { track: 0, position: 0 };
  playBook(book, resume.track, resume.position, true);
});

el.restartBook.addEventListener("click", async () => {
  const book = state.viewing;
  if (!book) return;
  await fetch(`/api/progress/${book.id}`, {
    method: "POST",
    body: JSON.stringify({ clear: true }),
  }).catch(() => {});
  delete state.progress[book.id];
  el.playBook.textContent = "Play";
  el.restartBook.hidden = true;
  playBook(book, 0, 0, true);
});

el.playpause.addEventListener("click", togglePlay);
el.back15.addEventListener("click", () => skip(-15));
el.fwd30.addEventListener("click", () => skip(30));
el.prev.addEventListener("click", () => {
  if (el.audio.currentTime > 3) el.audio.currentTime = 0;
  else loadTrack(state.trackIndex - 1, 0, !el.audio.paused);
});
el.next.addEventListener("click", () => loadTrack(state.trackIndex + 1, 0, !el.audio.paused));

el.speed.addEventListener("change", () => {
  el.audio.playbackRate = parseFloat(el.speed.value) || 1;
  localStorage.setItem("shortlist.speed", el.speed.value);
});
el.sleep.addEventListener("change", armSleepTimer);

el.seek.addEventListener("input", () => { state.scrubbing = true; });
el.seek.addEventListener("change", () => {
  seekToFraction(Number(el.seek.value) / 1000);
  state.scrubbing = false;
  saveProgress(true);
});

el.audio.addEventListener("timeupdate", () => {
  renderProgress();
  saveProgress();
  checkSleepTimer();
});
el.audio.addEventListener("play", () => { el.playpause.textContent = "⏸"; });
el.audio.addEventListener("pause", () => { el.playpause.textContent = "▶"; saveProgress(true); });
el.audio.addEventListener("loadedmetadata", renderProgress);

el.audio.addEventListener("ended", async () => {
  const book = state.playing;
  if (!book) return;
  if (state.sleepAfterChapter) {
    state.sleepAfterChapter = false;
    el.sleep.value = "0";
    saveProgress(true);
    toast("Sleep timer — end of chapter");
    return;
  }
  if (state.trackIndex < book.tracks.length - 1) {
    loadTrack(state.trackIndex + 1, 0, true);
  } else {
    await saveProgress(true, true);
    toast("Finished");
    if (state.viewing && state.viewing.id === book.id) showBook(book.id).catch(() => {});
  }
});

el.audio.addEventListener("error", () => {
  if (el.audio.src) toast("Could not play this file — try Rescan.");
});

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, select, textarea")) return;
  if (document.querySelector("dialog[open]")) return;  // the dialog owns the keyboard
  const keys = {
    " ": () => togglePlay(),
    ArrowLeft: () => skip(-15),
    ArrowRight: () => skip(30),
    ArrowUp: () => loadTrack(state.trackIndex - 1, 0, !el.audio.paused),
    ArrowDown: () => loadTrack(state.trackIndex + 1, 0, !el.audio.paused),
    Escape: () => { if (state.viewing) showLibrary(); },
  };
  const handler = keys[event.key];
  if (handler) { event.preventDefault(); handler(); }
});

window.addEventListener("pagehide", () => saveProgress(true, false, true));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") saveProgress(true, false, true);
});

/* ---------------------------------------------------------------- settings */

async function openSettings() {
  try {
    const config = await api("/api/settings");
    el.setLibrary.value = config.library || "";
    el.setOutput.value = config.output || "";
    el.setImport.value = config.importDir || "";
    el.setHost.value = config.host || "";
    el.setPort.value = config.port || "";
    el.ownCurrent.value = el.ownPassword.value = el.ownConfirm.value = "";
    el.verifyResult.textContent = el.matchResult.textContent = "";
    el.verifyResult.className = el.matchResult.className = "";
    state.hadAccounts = config.authEnabled;
    await loadUsers();
    state.libraryMissing = config.libraryExists === false;
    el.containerNote.hidden = !config.inContainer;

    // A folder the server cannot write to fails much later and confusingly,
    // so say it where the folder is configured.
    el.libraryWritable.hidden = config.libraryWritable !== false;
    el.libraryWritable.textContent = config.libraryWritable === false
      ? `Not writable (${config.libraryProblem}) — uploads will fail. `
        + "In Docker, chown it to your PUID/PGID on the host."
      : "";
    el.outputWritable.hidden = config.outputWritable !== false;
    el.outputWritable.textContent = config.outputWritable === false
      ? `Not writable (${config.outputProblem}) — organising will fail. `
        + "In Docker, chown it to your PUID/PGID on the host."
      : "";
    el.stateWarning.hidden = config.stateWritable !== false;
    el.stateWarningDetail.textContent = config.stateProblem
      ? `${config.stateDir}: ${config.stateProblem}.` : "";
    el.settingsPaths.textContent =
      `${config.bookCount} books loaded · listening on ${config.boundHost}:${config.boundPort}`
      + ` · settings in ${config.settingsFile}`;
    el.settingsStatus.textContent = "";
    el.organiseResult.textContent = "";
    el.organiseResult.className = "";
    el.organiseProgress.hidden = true;
    el.settings.showModal();
  } catch (error) {
    toast(error.message);
  }
}

async function saveSettings() {
  el.settingsSave.disabled = true;
  el.settingsStatus.textContent = "Saving…";
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        library: el.setLibrary.value.trim(),
        output: el.setOutput.value.trim(),
        importDir: el.setImport.value.trim(),
        host: el.setHost.value.trim(),
        port: el.setPort.value,
      }),
    });
    if (result.rescanned) {
      await loadLibrary();
      showLibrary();
    }
    if (result.rebindError) {
      el.settingsStatus.textContent =
        `Saved, but could not move to port ${result.port}: ${result.rebindError}. Still on ${location.port}.`;
      return;
    }
    if (result.rebound) {
      // The socket we are talking to is about to close; follow it to the new one.
      const target = `${location.protocol}//${location.hostname}:${result.port}${location.pathname}`;
      el.settingsStatus.textContent = `Moved to port ${result.port} — reconnecting…`;
      toast(`Server moved to port ${result.port}`);
      setTimeout(() => { location.href = target; }, 1200);
      return;
    }
    el.settingsStatus.textContent = "Saved.";
    toast(result.rescanned ? `Library switched · ${result.bookCount} books` : "Settings saved");
  } catch (error) {
    el.settingsStatus.textContent = error.message;
  } finally {
    el.settingsSave.disabled = false;
  }
}

function closeMenu() {
  el.userMenu.hidden = true;
  el.menuToggle.setAttribute("aria-expanded", "false");
}

el.menuToggle.addEventListener("click", (event) => {
  event.stopPropagation();
  const opening = el.userMenu.hidden;
  el.userMenu.hidden = !opening;
  el.menuToggle.setAttribute("aria-expanded", String(opening));
});

// Clicking anywhere else, or pressing escape, dismisses it.
document.addEventListener("click", (event) => {
  if (!el.userMenu.hidden && !event.target.closest(".menu-wrap")) closeMenu();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !el.userMenu.hidden) closeMenu();
});

el.settingsOpen.addEventListener("click", () => { closeMenu(); openSettings(); });
el.settingsSave.addEventListener("click", saveSettings);
el.settingsCancel.addEventListener("click", () => el.settings.close());

/* ------------------------------------------------------------ remove book */

function refreshRemoveDialog() {
  const deleting = el.removeFiles.checked;
  el.removeWarning.hidden = !deleting;
  el.removeConfirm.textContent = deleting ? "Delete files permanently" : "Remove from library";
}

el.removeBook.addEventListener("click", async () => {
  const book = state.viewing;
  if (!book) return;
  el.removeFiles.checked = false;
  el.removeStatus.textContent = "";
  refreshRemoveDialog();
  el.removeWhat.innerHTML =
    `<strong>${escapeHtml(book.title)}</strong> by ${escapeHtml(book.author)} — ` +
    `${book.trackCount} file${book.trackCount === 1 ? "" : "s"}, ${escapeHtml(book.durationText)}.` +
    `<br><span class="muted small">Removing hides it from the library and forgets its ` +
    `progress and details. The files stay on disk unless you tick the box.</span>`;
  el.removeList.innerHTML = book.tracks
    .map((t) => `<li>${escapeHtml(t.rel)}</li>`).join("");
  el.removeDialog.showModal();
});

el.removeFiles.addEventListener("change", refreshRemoveDialog);
el.removeCancel.addEventListener("click", () => el.removeDialog.close());

el.removeConfirm.addEventListener("click", async () => {
  const book = state.viewing;
  if (!book) return;
  const deleting = el.removeFiles.checked;
  el.removeConfirm.disabled = true;
  el.removeStatus.textContent = deleting ? "Deleting…" : "Removing…";
  try {
    const result = await api(`/api/books/${book.id}/remove`, {
      method: "POST",
      body: JSON.stringify({ deleteFiles: deleting, confirm: deleting }),
    });
    el.removeDialog.close();
    await loadLibrary();
    showLibrary();
    toast(result.hiddenOnly
      ? `${book.title} removed from the library`
      : `${book.title} deleted — ${result.filesDeleted} file(s)`);
    if (result.failed && result.failed.length) {
      toast(`${result.failed.length} file(s) could not be deleted`);
    }
  } catch (error) {
    el.removeStatus.textContent = error.message;
  } finally {
    el.removeConfirm.disabled = false;
  }
});

/* --------------------------------------------------------- book metadata */

async function searchMetadata() {
  const book = state.viewing;
  if (!book) return;
  const providers = [
    el.srcAudible.checked && "audible",
    el.srcItunes.checked && "itunes",
    el.srcGoogle.checked && "google",
  ].filter(Boolean);
  if (!providers.length) {
    el.metaNote.textContent = "Pick at least one source.";
    return;
  }

  el.metaSearch.disabled = true;
  el.metaNote.textContent = "Searching…";
  el.metaResults.innerHTML = "";
  try {
    const params = new URLSearchParams({
      title: el.metaTitle.value.trim(),
      author: el.metaAuthor.value.trim(),
      providers: providers.join(","),
    });
    const found = await api(`/api/books/${book.id}/metadata/search?${params}`);
    const failed = Object.entries(found.errors || {});
    el.metaNote.textContent = [
      `${found.candidates.length} match${found.candidates.length === 1 ? "" : "es"}`,
      ...failed.map(([name, message]) => `${name}: ${message}`),
    ].join(" · ");

    el.metaResults.innerHTML = found.candidates.map((candidate, index) => {
      const bits = [
        candidate.authors.join(", "),
        candidate.narrators.length ? `read by ${candidate.narrators.join(", ")}` : "",
        candidate.year,
        candidate.runtimeMinutes ? `${Math.round(candidate.runtimeMinutes / 60)}h` : "",
      ].filter(Boolean);
      return `
        <li>
          ${candidate.coverUrl
            ? `<img class="thumb" src="${escapeHtml(candidate.coverUrl)}" alt="" loading="lazy">`
            : '<div class="thumb"></div>'}
          <div class="body">
            <div class="name"><span class="badge-src">${escapeHtml(candidate.provider)}</span>${escapeHtml(candidate.title)}</div>
            <div class="meta">${escapeHtml(bits.join(" · "))}</div>
            ${candidate.description ? `<div class="blurb">${escapeHtml(candidate.description)}</div>` : ""}
          </div>
          <button class="ghost use" data-index="${index}">Use this</button>
        </li>`;
    }).join("");
    metadataCandidates = found.candidates;
  } catch (error) {
    el.metaNote.textContent = error.message;
  } finally {
    el.metaSearch.disabled = false;
  }
}

let metadataCandidates = [];

async function applyMetadata(candidate) {
  const book = state.viewing;
  if (!book) return;
  el.metaStatus.textContent = "Applying…";
  try {
    await api(`/api/books/${book.id}/metadata`, {
      method: "POST",
      body: JSON.stringify(candidate),
    });
    el.metadata.close();
    await showBook(book.id);
    await loadLibrary();      // the grid may now have a cover it did not before
    toast(`Details added from ${candidate.provider}`);
  } catch (error) {
    el.metaStatus.textContent = error.message;
  }
}

el.findMetadata.addEventListener("click", () => {
  const book = state.viewing;
  if (!book) return;
  el.metaTitle.value = book.title;
  el.metaAuthor.value = book.author === "Unknown author" ? "" : book.author;
  el.metaResults.innerHTML = "";
  el.metaNote.textContent = "";
  el.metaStatus.textContent = "";
  el.metaClear.hidden = !book.metadata;
  el.authorResults.innerHTML = "";
  el.authorNote.textContent = "";
  el.authorCurrent.textContent = book.authorBio
    ? `Bio already saved for ${book.author} (from ${book.authorBio.provider}).`
    : (book.author && book.author !== "Unknown author" ? book.author : "No author on this book.");
  el.metadata.showModal();
  searchMetadata();
});

el.metaSearch.addEventListener("click", (event) => { event.preventDefault(); searchMetadata(); });
el.metaClose.addEventListener("click", () => el.metadata.close());
el.metaResults.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-index]");
  if (button) applyMetadata(metadataCandidates[Number(button.dataset.index)]);
});
el.metaClear.addEventListener("click", async () => {
  const book = state.viewing;
  if (!book) return;
  try {
    await api(`/api/books/${book.id}/metadata`, { method: "POST", body: JSON.stringify({ clear: true }) });
    el.metadata.close();
    await showBook(book.id);
    await loadLibrary();
    toast("Details removed");
  } catch (error) {
    el.metaStatus.textContent = error.message;
  }
});

for (const [key, field] of [["Enter", el.metaTitle], ["Enter", el.metaAuthor]]) {
  field.addEventListener("keydown", (event) => {
    if (event.key === key) { event.preventDefault(); searchMetadata(); }
  });
}

/* ------------------------------------------------------------ author bios */

let authorCandidates = [];

async function searchAuthor() {
  const book = state.viewing;
  if (!book || !book.author || book.author === "Unknown author") {
    el.authorNote.textContent = "This book has no author to look up.";
    return;
  }
  el.authorSearch.disabled = true;
  el.authorNote.textContent = `Searching Wikipedia and Open Library for ${book.author}…`;
  el.authorResults.innerHTML = "";
  try {
    const found = await api(`/api/authors/search?name=${encodeURIComponent(book.author)}`);
    authorCandidates = found.candidates;
    const failed = Object.entries(found.errors || {});
    el.authorNote.textContent = [
      `${found.candidates.length} possible author${found.candidates.length === 1 ? "" : "s"}`,
      ...failed.map(([name, message]) => `${name}: ${message}`),
    ].join(" · ");

    el.authorResults.innerHTML = found.candidates.map((candidate, index) => {
      const life = [candidate.born, candidate.died].filter(Boolean).join(" – ");
      const bits = [candidate.summary, life, candidate.topWork].filter(Boolean);
      return `
        <li>
          <div class="body">
            <div class="name"><span class="badge-src">${escapeHtml(candidate.provider)}</span>${escapeHtml(candidate.name)}</div>
            <div class="meta">${escapeHtml(bits.join(" · "))}</div>
            <div class="blurb">${escapeHtml(candidate.bio || "No biography on this source.")}</div>
          </div>
          <button class="ghost use" data-author-index="${index}"
            ${candidate.bio ? "" : "disabled"}>Use this</button>
        </li>`;
    }).join("");
  } catch (error) {
    el.authorNote.textContent = error.message;
  } finally {
    el.authorSearch.disabled = false;
  }
}

async function applyAuthor(candidate) {
  const book = state.viewing;
  if (!book) return;
  try {
    await api("/api/authors", {
      method: "POST",
      body: JSON.stringify({ ...candidate, author: book.author }),
    });
    el.metadata.close();
    await showBook(book.id);
    toast(`Author bio added from ${candidate.provider}`);
  } catch (error) {
    el.authorNote.textContent = error.message;
  }
}

el.authorSearch.addEventListener("click", (event) => { event.preventDefault(); searchAuthor(); });
el.authorResults.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-author-index]");
  if (button) applyAuthor(authorCandidates[Number(button.dataset.authorIndex)]);
});

/* ------------------------------------------------- bulk metadata lookup */

async function pollFetchAll() {
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    let status;
    try {
      status = await api("/api/metadata/fetch-all/status");
    } catch (error) {
      el.fetchResult.textContent = `Lost contact: ${error.message}`;
      break;
    }
    if (status.total) {
      el.fetchProgress.firstElementChild.style.width =
        `${Math.round((status.done / status.total) * 100)}%`;
    }
    const minutes = Math.ceil((status.etaSeconds || 0) / 60);
    const trouble = status.failed
      ? ` · ${status.failed} failed${status.lastError ? ` (${status.lastError})` : ""}`
      : "";
    el.fetchResult.textContent = status.running
      ? (status.backingOff ? "Rate limited — waiting a minute before carrying on. " : "") +
        `${status.done} of ${status.total} · ${status.applied} added, ${status.skipped} skipped` +
        (minutes ? ` · about ${minutes} min left` : "") + trouble
      : `${status.stopped ? "Stopped" : "Finished"} — ${status.applied} added, ` +
        `${status.skipped} skipped, ${status.failed} failed` +
        (status.lastError ? ` · last error: ${status.lastError}` : "");
    if (!status.running) {
      el.fetchStop.hidden = true;
      el.fetchAll.disabled = false;
      if (status.applied) await loadLibrary();
      break;
    }
  }
}

el.fetchAll.addEventListener("click", async (event) => {
  event.preventDefault();
  el.fetchAll.disabled = true;
  el.fetchResult.textContent = "Starting…";
  el.fetchProgress.hidden = false;
  el.fetchProgress.firstElementChild.style.width = "0%";
  try {
    const started = await api("/api/metadata/fetch-all", { method: "POST", body: "{}" });
    if (!started.queued) {
      el.fetchResult.textContent = "Every book already has details.";
      el.fetchAll.disabled = false;
      return;
    }
    el.fetchStop.hidden = false;
    el.fetchResult.textContent = `Looking up ${started.queued} books…`;
    pollFetchAll();
  } catch (error) {
    el.fetchResult.textContent = error.message;
    el.fetchAll.disabled = false;
  }
});

el.fetchStop.addEventListener("click", async (event) => {
  event.preventDefault();
  el.fetchStop.disabled = true;
  await api("/api/metadata/fetch-all/stop", { method: "POST", body: "{}" }).catch(() => {});
  el.fetchStop.disabled = false;
});

/* ----------------------------------------------------------------- import */

async function runImport(dryRun) {
  const source = el.setImport.value.trim();
  if (!source) {
    el.importResult.textContent = "Set a download folder first.";
    el.importResult.className = "bad";
    return;
  }
  if (!dryRun && el.importMove.checked &&
      !confirm("This deletes each download once its copy in the library is verified.\n\n" +
               "The download folder will end up empty. Continue?")) {
    return;
  }

  el.importPreview.disabled = el.importRun.disabled = true;
  el.importResult.className = "";
  el.importResult.textContent = dryRun ? "Working out the plan…" : "Starting…";
  el.importProgress.hidden = false;
  el.importProgress.firstElementChild.style.width = "0%";

  try {
    const started = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({
        importDir: source,
        dryRun,
        deleteOriginals: el.importMove.checked,
        confirm: el.importMove.checked,
      }),
    });
    el.importResult.textContent = `Found ${started.books} books in the download folder…`;
  } catch (error) {
    el.importResult.textContent = error.message;
    el.importResult.className = "bad";
    el.importProgress.hidden = true;
    el.importPreview.disabled = el.importRun.disabled = false;
    return;
  }

  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 800));
    let status;
    try {
      status = await api("/api/organise/status");
    } catch (error) {
      el.importResult.textContent = `Lost contact with the server: ${error.message}`;
      break;
    }
    if (status.total) {
      el.importProgress.firstElementChild.style.width =
        `${Math.round((status.done / status.total) * 100)}%`;
      el.importResult.textContent = `${status.done} of ${status.total} — ${status.current || ""}`;
    }
    if (!status.running) {
      el.importProgress.firstElementChild.style.width = "100%";
      if (status.error) {
        el.importResult.textContent = status.error;
        el.importResult.className = "bad";
      } else {
        // The library has new files in it now, so it has to be re-read.
        if (!dryRun) {
          try { await api("/api/rescan", { method: "POST" }); await loadLibrary(); } catch {}
        }
        el.importResult.textContent =
          (dryRun ? "Preview — nothing written. " : "Imported. ") + (status.summary || "");
        el.importResult.className = "ok";
      }
      break;
    }
  }
  el.importPreview.disabled = el.importRun.disabled = false;
}

el.importPreview.addEventListener("click", (e) => { e.preventDefault(); runImport(true); });
el.importRun.addEventListener("click", (e) => { e.preventDefault(); runImport(false); });

/* --------------------------------------------------------------- organise */

async function runOrganise(dryRun) {
  const output = el.setOutput.value.trim();
  if (!output) {
    el.organiseResult.textContent = "Set an output folder first.";
    el.organiseResult.className = "bad";
    return;
  }
  // Moving deletes your originals, so ask in as many words before starting.
  if (!dryRun && el.organiseMove.checked &&
      !confirm("This deletes each original file after its copy is verified.\n\n" +
               "Your library folder will end up empty. Continue?")) {
    return;
  }
  el.organisePreview.disabled = el.organiseRun.disabled = true;
  el.organiseResult.className = "";
  el.organiseResult.textContent = dryRun ? "Working out the plan…" : "Starting…";
  el.organiseProgress.hidden = false;
  el.organiseProgress.firstElementChild.style.width = "0%";

  try {
    await api("/api/organise", {
      method: "POST",
      body: JSON.stringify({
        output,
        dryRun,
        playlistsOnly: el.organisePlaylists.checked,
        deleteOriginals: el.organiseMove.checked,
        confirm: el.organiseMove.checked,
      }),
    });
  } catch (error) {
    el.organiseResult.textContent = error.message;
    el.organiseResult.className = "bad";
    el.organiseProgress.hidden = true;
    el.organisePreview.disabled = el.organiseRun.disabled = false;
    return;
  }

  // The copy runs on the server; poll until it reports itself finished.
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 700));
    let status;
    try {
      status = await api("/api/organise/status");
    } catch (error) {
      el.organiseResult.textContent = `Lost contact with the server: ${error.message}`;
      el.organiseResult.className = "bad";
      break;
    }
    if (status.total) {
      const percent = Math.round((status.done / status.total) * 100);
      el.organiseProgress.firstElementChild.style.width = `${percent}%`;
      el.organiseResult.textContent =
        `${status.done} of ${status.total} — ${status.current || ""}`;
    }
    if (!status.running) {
      el.organiseProgress.firstElementChild.style.width = "100%";
      if (status.error) {
        el.organiseResult.textContent = status.error;
        el.organiseResult.className = "bad";
      } else {
        el.organiseResult.textContent =
          (dryRun ? "Preview — nothing written. " : "Done. ") + (status.summary || "");
        el.organiseResult.className = "ok";
      }
      break;
    }
  }
  el.organisePreview.disabled = el.organiseRun.disabled = false;
}

el.organisePreview.addEventListener("click", (event) => { event.preventDefault(); runOrganise(true); });
el.organiseRun.addEventListener("click", (event) => { event.preventDefault(); runOrganise(false); });

/* ----------------------------------------------------------- folder picker */

const picker = { target: null, path: "" };

async function showFolder(path) {
  el.pickerStatus.textContent = "";
  try {
    const view = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
    picker.path = view.path;
    el.pickerPath.value = view.path;
    el.pickerUp.disabled = !view.parent;

    el.pickerShortcuts.innerHTML = view.shortcuts
      .map((s) => `<button data-path="${escapeHtml(s.path)}">${escapeHtml(s.name)}</button>`)
      .join("");

    el.pickerList.innerHTML = view.directories.length
      ? view.directories
          .map((d) => `<li data-path="${escapeHtml(d.path)}">📁 ${escapeHtml(d.name)}</li>`)
          .join("")
      : '<li class="none">No sub-folders here.</li>';

    const bits = [];
    if (view.audioFiles) bits.push(`${view.audioFiles} audio file${view.audioFiles === 1 ? "" : "s"} here`);
    if (!view.writable) bits.push("read-only");
    el.pickerInfo.textContent = bits.join(" · ");
  } catch (error) {
    el.pickerStatus.textContent = error.message;
  }
}

function openPicker(targetId) {
  picker.target = { "set-library": el.setLibrary, "set-output": el.setOutput,
                    "set-import": el.setImport }[targetId] || el.setLibrary;
  el.pickerTitle.textContent = {
    "set-library": "Choose the library folder",
    "set-output": "Choose the output folder",
    "set-import": "Choose the download folder",
  }[targetId] || "Choose a folder";
  el.picker.showModal();
  showFolder(picker.target.value.trim());
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-browse]");
  if (!button) return;
  event.preventDefault();
  openPicker(button.dataset.browse);
});

el.pickerList.addEventListener("click", (event) => {
  const row = event.target.closest("li[data-path]");
  if (row) showFolder(row.dataset.path);
});
el.pickerShortcuts.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-path]");
  if (button) showFolder(button.dataset.path);
});
el.pickerUp.addEventListener("click", () => {
  const parent = picker.path.replace(/\/[^/]+\/?$/, "") || "/";
  showFolder(parent);
});
el.pickerGo.addEventListener("click", () => showFolder(el.pickerPath.value.trim()));
el.pickerPath.addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); showFolder(el.pickerPath.value.trim()); }
});
el.pickerCancel.addEventListener("click", () => el.picker.close());
el.pickerChoose.addEventListener("click", () => {
  if (picker.target) picker.target.value = picker.path;
  el.picker.close();
});

/* -------------------------------------------------------- password helpers */

function checkPasswordsMatch() {
  const password = el.ownPassword.value;
  const confirmation = el.ownConfirm.value;
  if (!password && !confirmation) {
    el.matchResult.textContent = "";
    el.matchResult.className = "";
    return null;
  }
  const matched = password === confirmation;
  el.matchResult.textContent = matched ? "Passwords match." : "Passwords do not match.";
  el.matchResult.className = matched ? "ok" : "bad";
  return matched;
}

el.ownPassword.addEventListener("input", checkPasswordsMatch);
el.ownConfirm.addEventListener("input", checkPasswordsMatch);

el.verifyPassword.addEventListener("click", async (event) => {
  event.preventDefault();
  el.verifyResult.textContent = "Checking…";
  el.verifyResult.className = "";
  try {
    const result = await api("/api/settings/password/verify", {
      method: "POST",
      body: JSON.stringify({ password: el.ownCurrent.value }),
    });
    el.verifyResult.textContent = result.valid
      ? "That is the current password."
      : "That is not the current password.";
    el.verifyResult.className = result.valid ? "ok" : "bad";
  } catch (error) {
    el.verifyResult.textContent = error.message;
    el.verifyResult.className = "bad";
  }
});

/* ---------------------------------------------------------------- accounts */

/** Hide everything a listener cannot do, so the UI matches what the server
 *  will actually allow rather than failing on click. */
function applyRole(you, accountsConfigured) {
  const admin = you.role === "admin";
  state.isAdmin = admin;

  const signedIn = Boolean(you.username);
  el.menuWho.hidden = !signedIn;
  el.signout.hidden = !signedIn;
  if (signedIn) {
    el.menuWho.innerHTML =
      `<strong>${escapeHtml(you.username)}</strong>` +
      `<span class="role-tag">${escapeHtml(you.role)}</span>`;
  }
  for (const control of [el.uploadOpen, el.rescan]) control.hidden = !admin;
  el.findMetadata.hidden = !admin;
  el.removeBook.hidden = !admin;
  el.addUserBox.hidden = !admin;
  document.querySelectorAll("#settings .dialog-body section").forEach((section) => {
    if (section.id !== "accounts-section") section.hidden = !admin;
  });
  el.settingsSave.hidden = !admin;
  el.ownCurrentField.hidden = admin;   // admins may set their own without proving it
}

async function loadUsers() {
  const data = await api("/api/users");
  applyRole(data.you, data.accountsConfigured);

  el.authStatus.textContent = data.accountsConfigured
    ? `Signed in as ${data.you.username} (${data.you.role}).`
    : "No accounts yet — the server is open to anyone on your network. Add one below.";
  el.authWarning.hidden = data.accountsConfigured;
  el.defaultPasswordWarning.hidden = !data.defaultPassword;

  el.userList.innerHTML = data.users.map((user) => `
    <li>
      <span class="who"><strong>${escapeHtml(user.username)}</strong></span>
      <select data-role-for="${escapeHtml(user.username)}">
        <option value="listener" ${user.role === "listener" ? "selected" : ""}>Listener</option>
        <option value="admin" ${user.role === "admin" ? "selected" : ""}>Admin</option>
      </select>
      <button class="ghost" data-reset-for="${escapeHtml(user.username)}">Set password</button>
      <button class="ghost danger" data-delete-for="${escapeHtml(user.username)}">Delete</button>
    </li>`).join("");
}

el.addUser.addEventListener("click", async (event) => {
  event.preventDefault();
  try {
    await api("/api/users", {
      method: "POST",
      body: JSON.stringify({
        username: el.newUsername.value.trim(),
        password: el.newPassword.value,
        role: el.newRole.value,
      }),
    });
    const created = el.newUsername.value.trim();
    el.newUsername.value = el.newPassword.value = "";
    // The very first account switches the server from open to requiring a
    // login, so this page has no credentials yet. Reload and let the browser
    // ask, rather than refreshing the list and getting a 401.
    if (!state.hadAccounts) {
      el.settingsStatus.textContent = `Added ${created}. Signing in…`;
      setTimeout(() => location.reload(), 1200);
      return;
    }
    await loadUsers();
    el.settingsStatus.textContent = `Added ${created}.`;
  } catch (error) {
    el.settingsStatus.textContent = error.message;
  }
});

el.userList.addEventListener("change", async (event) => {
  const select = event.target.closest("select[data-role-for]");
  if (!select) return;
  try {
    await api(`/api/users/${encodeURIComponent(select.dataset.roleFor)}/role`, {
      method: "POST", body: JSON.stringify({ role: select.value }),
    });
    el.settingsStatus.textContent = `${select.dataset.roleFor} is now a ${select.value}.`;
    await loadUsers();
  } catch (error) {
    el.settingsStatus.textContent = error.message;
    await loadUsers();
  }
});

el.userList.addEventListener("click", async (event) => {
  const remove = event.target.closest("button[data-delete-for]");
  const reset = event.target.closest("button[data-reset-for]");
  try {
    if (remove) {
      const name = remove.dataset.deleteFor;
      if (!confirm(`Delete the account "${name}"? Their saved places are deleted too.`)) return;
      await api(`/api/users/${encodeURIComponent(name)}/delete`, { method: "POST", body: "{}" });
      el.settingsStatus.textContent = `Deleted ${name}.`;
      await loadUsers();
    } else if (reset) {
      const name = reset.dataset.resetFor;
      const password = prompt(`New password for ${name}:`);
      if (!password) return;
      await api(`/api/users/${encodeURIComponent(name)}/password`, {
        method: "POST", body: JSON.stringify({ password }),
      });
      el.settingsStatus.textContent = `Password changed for ${name}.`;
    }
  } catch (error) {
    el.settingsStatus.textContent = error.message;
  }
});

el.changeOwn.addEventListener("click", async (event) => {
  event.preventDefault();
  if (el.ownPassword.value !== el.ownConfirm.value) {
    el.matchResult.textContent = "The two passwords do not match.";
    el.matchResult.className = "bad";
    return;
  }
  try {
    const data = await api("/api/users");
    await api(`/api/users/${encodeURIComponent(data.you.username)}/password`, {
      method: "POST",
      body: JSON.stringify({ current: el.ownCurrent.value, password: el.ownPassword.value }),
    });
    el.ownCurrent.value = el.ownPassword.value = el.ownConfirm.value = "";
    el.matchResult.textContent = "";
    el.settingsStatus.textContent = "Your password has been changed.";
  } catch (error) {
    el.settingsStatus.textContent = error.message;
  }
});

el.signout.addEventListener("click", async () => {
  closeMenu();
  if (el.audio.src) el.audio.pause();
  try {
    await fetch("/api/logout", { method: "POST", body: "{}", cache: "no-store" });
  } catch {
    /* the cookie is server-side; a reload will land on the sign-in screen anyway */
  }
  location.reload();
});

/* ------------------------------------------------------------------ upload */

async function uploadFiles(files) {
  const queue = [...files];
  if (!queue.length) return;
  let stored = 0;

  for (const file of queue) {
    const row = document.createElement("li");
    row.innerHTML = `<span class="name">${escapeHtml(file.name)}</span><span class="state">…</span>`;
    el.uploadList.prepend(row);
    const status = row.querySelector(".state");

    try {
      const response = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || response.statusText);
      status.textContent = result.renamed ? `saved as ${result.stored}` : "added";
      status.className = "state ok";
      stored += 1;
    } catch (error) {
      status.textContent = error.message;
      status.className = "state bad";
    }
  }

  if (!stored) {
    el.uploadStatus.textContent = "Nothing was added.";
    return;
  }
  // New files only appear once the library has been re-read.
  el.uploadStatus.textContent = `${stored} added — rescanning…`;
  try {
    const result = await api("/api/rescan", { method: "POST" });
    await loadLibrary();
    showLibrary();
    el.uploadStatus.textContent = `${stored} added · library now has ${result.count} books`;
  } catch (error) {
    el.uploadStatus.textContent = `Added, but the rescan failed: ${error.message}`;
  }
}

el.uploadOpen.addEventListener("click", async () => {
  el.uploadList.innerHTML = "";
  el.uploadStatus.textContent = "";
  try {
    const config = await api("/api/settings");
    el.uploadTarget.textContent = `Files are saved into ${config.activeLibrary}`;
    el.uploadBlocked.hidden = config.libraryWritable !== false;
    el.uploadBlocked.textContent = config.libraryWritable === false
      ? `That folder is not writable (${config.libraryProblem}), so uploads will fail.`
      : "";
  } catch {
    el.uploadTarget.textContent = "";
  }
  el.upload.showModal();
});
el.uploadClose.addEventListener("click", () => el.upload.close());
el.chooseFiles.addEventListener("click", (event) => { event.preventDefault(); el.fileInput.click(); });
el.fileInput.addEventListener("change", () => {
  uploadFiles(el.fileInput.files);
  el.fileInput.value = "";  // so re-picking the same file fires change again
});

for (const type of ["dragenter", "dragover"]) {
  el.dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    el.dropzone.classList.add("over");
  });
}
for (const type of ["dragleave", "drop"]) {
  el.dropzone.addEventListener(type, () => el.dropzone.classList.remove("over"));
}
el.dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  uploadFiles(event.dataTransfer.files);
});
// A file dropped anywhere else would otherwise navigate away from the player.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());

/* Keep page padding equal to the player's real height: the transport wraps
 * onto two or three rows on a phone, and a guessed constant leaves content
 * stranded underneath it. */
function syncPlayerHeight() {
  const height = el.player.hidden ? 40 : el.player.offsetHeight + 24;
  document.documentElement.style.setProperty("--player-height", `${height}px`);
}
if (window.ResizeObserver) new ResizeObserver(syncPlayerHeight).observe(el.player);
window.addEventListener("resize", syncPlayerHeight);

/* --------------------------------------------------------------- sign in */

async function showSignIn(message) {
  el.signin.hidden = false;
  el.app.hidden = true;
  try {
    const first = await (await fetch("/api/first-run")).json();
    el.signinHint.hidden = !first.defaultPassword;
    el.signinState.hidden = first.stateWritable !== false;
  } catch {
    el.signinHint.hidden = true;
  }
  el.loginError.hidden = !message;
  el.loginError.textContent = message || "";
  el.loginUsername.focus();
}

el.signinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  el.loginSubmit.disabled = true;
  el.loginError.hidden = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: el.loginUsername.value.trim(),
        password: el.loginPassword.value,
      }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "could not sign in");
    location.reload();      // start clean, with the session cookie in place
  } catch (error) {
    el.loginError.hidden = false;
    el.loginError.textContent = error.message;
    el.loginPassword.value = "";
    el.loginPassword.focus();
  } finally {
    el.loginSubmit.disabled = false;
  }
});

/* ------------------------------------------------------------------- boot */

syncPlayerHeight();
el.speed.value = localStorage.getItem("shortlist.speed") || "1";

(async () => {
  try {
    const who = await api("/api/users");
    el.signin.hidden = true;
    el.app.hidden = false;
    applyRole(who.you, who.accountsConfigured);
    await loadLibrary();
  } catch (error) {
    if (/sign in required|401/i.test(error.message)) {
      showSignIn("");            // no library data is rendered while signed out
    } else {
      el.signin.hidden = true;
      el.app.hidden = false;
      el.empty.hidden = false;
      el.empty.textContent = `Could not reach the server: ${error.message}`;
    }
  }
})();

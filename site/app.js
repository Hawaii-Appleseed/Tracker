(() => {
  const COUNCIL_LABEL = {
    honolulu: "Honolulu",
    maui: "Maui",
    hawaii: "Hawaii County",
    kauai: "Kauai",
  };
  const SUBJECT_LABEL = {
    tax: "Tax",
    transportation: "Transportation",
    food_security: "Food Security",
    affordable_housing: "Affordable Housing",
  };
  const MATTER_CLASS_LABEL = {
    legislation: "Bills & resolutions",
    communication: "Communications",
    procedural: "Procedural",
  };

  // ---- Search ---------------------------------------------------------------
  // A field-weighted inverted index over the whole corpus, built once per
  // ingest. It replaces a substring test against one concatenated haystack,
  // which could not do multi-term queries ("maui housing"), rank results, or
  // match a word still being typed.
  //
  // Hawaiian orthography is folded before indexing, so "Kauai", "Kaua'i" and
  // "Kauaʻi" are one token and a macron never hides a match.

  const SEARCH_FIELDS = [
    ["bill_number", 12],
    ["title", 6],
    ["introducer", 3],
    ["raw_subject", 2],
    ["last_action", 1],
    ["committee", 1],
    ["status", 1],
  ];
  const SUBJECT_TOKEN_WEIGHT = 2;
  const PHRASE_SCORE = 25;
  // Vowels that a folded query character should still match in display text,
  // so highlighting survives the same folding the index applied.
  const FOLD_VARIANTS = { a: "aā", e: "eē", i: "iī", o: "oō", u: "uū" };
  const OKINA_CLASS = "[\\u02bb\\u02bc\\u2018\\u2019']";

  function fold(s) {
    return (s || "")
      .toLowerCase()
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")              // macrons & other diacritics
      .replace(/[ʻʼ‘’']/g, ""); // ʻokina & apostrophes
  }
  function tokenize(s) {
    return fold(s).match(/[a-z0-9]+/g) || [];
  }

  // Repeats count for something, but with sharply diminishing returns: a term
  // appearing 10 times in one field is worth 1.5x one mention, not 10x. Without
  // this, field weights stop meaning anything on long text.
  function tfBoost(n) {
    return 1 + Math.min(n - 1, 2) * 0.25;
  }

  const searchIndex = { postings: new Map(), tokens: [], blobs: [] };

  function buildSearchIndex(bills) {
    const postings = new Map();
    const blobs = new Array(bills.length);
    const add = (tok, i, w) => {
      let p = postings.get(tok);
      if (!p) postings.set(tok, (p = new Map()));
      p.set(i, (p.get(i) || 0) + w);
    };
    for (let i = 0; i < bills.length; i++) {
      const b = bills[i];
      b._idx = i;
      const parts = [];
      for (const [field, weight] of SEARCH_FIELDS) {
        const val = b[field];
        if (!val) continue;
        parts.push(val);
        // Count occurrences first, then contribute once per distinct token with
        // saturating term frequency. Adding weight per occurrence let a long
        // summary that repeats "housing" outrank a bill with it in the title.
        const tf = new Map();
        for (const tok of tokenize(val)) tf.set(tok, (tf.get(tok) || 0) + 1);
        for (const [tok, n] of tf) add(tok, i, weight * tfBoost(n));
      }
      // Subject tags are labels, not prose: indexing them lets "housing" find a
      // bill tagged Affordable Housing whose title never says the word — but at
      // a low weight, so a real title match always wins.
      for (const s of b.subjects || []) {
        for (const tok of new Set(tokenize(SUBJECT_LABEL[s] || s))) {
          add(tok, i, SUBJECT_TOKEN_WEIGHT);
        }
      }
      blobs[i] = fold(parts.join(" "));
    }
    searchIndex.postings = postings;
    searchIndex.tokens = [...postings.keys()].sort();
    searchIndex.blobs = blobs;
  }

  // Every indexed token starting with `pre`, found by binary search.
  function prefixMatches(pre) {
    const toks = searchIndex.tokens;
    let lo = 0, hi = toks.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (toks[mid] < pre) lo = mid + 1; else hi = mid;
    }
    const out = [];
    for (let i = lo; i < toks.length && toks[i].startsWith(pre); i++) out.push(toks[i]);
    return out;
  }

  // "quoted text" must appear verbatim; bare words are ANDed together.
  function parseQuery(raw) {
    const phrases = [];
    const rest = fold(raw).replace(/"([^"]*)"/g, (_, p) => {
      const t = p.trim();
      if (t) phrases.push(t);
      return " ";
    });
    return { phrases, terms: rest.match(/[a-z0-9]+/g) || [] };
  }

  // Map(billIndex -> score) for a query, or null when the query is empty.
  function searchScores(raw) {
    const { phrases, terms } = parseQuery(raw);
    if (!phrases.length && !terms.length) return null;

    let acc = null;
    const intersect = (hits) => {
      if (acc === null) { acc = hits; return; }
      const next = new Map();
      for (const [i, s] of hits) {
        const prev = acc.get(i);
        if (prev !== undefined) next.set(i, prev + s);
      }
      acc = next;
    };

    terms.forEach((term, n) => {
      const hits = new Map();
      const exact = searchIndex.postings.get(term);
      if (exact) for (const [i, w] of exact) hits.set(i, w * 2); // exact beats prefix
      // Only the final word expands by prefix — it's the one still being typed.
      // Expanding earlier words too would keep "tax bill" matching "taxonomy
      // billing" long after the reader moved past it.
      if (n === terms.length - 1) {
        for (const t of prefixMatches(term)) {
          if (t === term) continue;
          for (const [i, w] of searchIndex.postings.get(t)) {
            hits.set(i, (hits.get(i) || 0) + w);
          }
        }
      }
      intersect(hits);
    });

    if (phrases.length) {
      const hits = new Map();
      for (let i = 0; i < searchIndex.blobs.length; i++) {
        const blob = searchIndex.blobs[i];
        if (phrases.every((p) => blob.includes(p))) hits.set(i, PHRASE_SCORE * phrases.length);
      }
      intersect(hits);
    }
    return acc || new Map();
  }

  // Regex source matching `term` in *display* text, tolerating the ʻokina and
  // macrons that fold() stripped when indexing — otherwise a hit on "kauai"
  // would never highlight the "Kauaʻi" that produced it.
  function termRegexSource(term) {
    return term.split("").map((ch) => {
      const v = FOLD_VARIANTS[ch];
      return v ? `[${v}]` : ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join(OKINA_CLASS + "*");
  }

  // Full set powers the glossary panel; the conservative INLINE subset is
  // auto-linked inside titles/summaries.
  const GLOSSARY = {
    TAT: "Transient Accommodations Tax (hotel/short-term rental tax)",
    GET: "General Excise Tax (Hawaii's broad tax on business gross income)",
    ADU: "Accessory Dwelling Unit (a secondary home on a residential lot)",
    ohana: "Ohana unit — a secondary dwelling for extended family",
    TOD: "Transit-Oriented Development (denser housing near transit stops)",
    LIHTC: "Low-Income Housing Tax Credit",
    HUD: "U.S. Department of Housing and Urban Development",
    RPT: "Real Property Tax",
    TMK: "Tax Map Key (parcel identifier)",
    CZO: "Comprehensive Zoning Ordinance",
    ROH: "Revised Ordinances of Honolulu",
    HRS: "Hawaii Revised Statutes (state law)",
    CIP: "Capital Improvement Program (public construction budget)",
    DPP: "Department of Planning and Permitting (Honolulu)",
    SNAP: "Supplemental Nutrition Assistance Program (food benefits)",
    WIC: "Special Supplemental Nutrition Program for Women, Infants, and Children",
    EBT: "Electronic Benefits Transfer (how SNAP/WIC benefits are paid)",
    SMA: "Special Management Area (coastal land-use zone)",
    "HD#": "House Draft — a revised version of a state bill in the House",
    "SD#": "Senate Draft — a revised version of a state bill in the Senate",
    "CD#": "Conference Draft — a version reconciled between House and Senate",
  };

  const INLINE = [
    "TAT", "ADU", "TOD", "LIHTC", "HUD", "RPT", "TMK",
    "CZO", "ROH", "HRS", "CIP", "DPP", "SNAP", "WIC", "EBT", "SMA",
  ];
  const INLINE_RE = new RegExp("\\b(" + INLINE.join("|") + ")\\b", "g");
  const DRAFT_RE = /\b([HSC]D)(\d+)\b/g;
  const DRAFT_TITLE = {
    HD: "House Draft — a revised version of a state bill in the House",
    SD: "Senate Draft — a revised version of a state bill in the Senate",
    CD: "Conference Draft — reconciled between House and Senate",
  };

  // Favorites are persisted client-side (localStorage) — no backend, so they
  // live per-browser/device. Keyed by council|bill_number, which is stable
  // across re-scrapes and DB rebuilds (unlike the numeric id).
  const FAV_STORE = "tracker:favorites";
  function loadFavSet() {
    try { return new Set(JSON.parse(localStorage.getItem(FAV_STORE) || "[]")); }
    catch { return new Set(); }
  }
  function favKey(b) { return b.council + "|" + b.bill_number; }

  // Optional live-list backend (Cloudflare Worker + KV; see worker/DEPLOY.md).
  // Empty string = feature OFF: the app uses only the compressed URL-hash
  // snapshot links below. Set this to the deployed Worker URL — or define
  // window.TRACKER_LIST_API — to make shared links/bookmarks update live as
  // their owner edits. Every server call falls back to a snapshot link on error.
  const LIST_API = (typeof window !== "undefined" && window.TRACKER_LIST_API) || "";
  const LIVE_STORE = "tracker:livelist";   // {id, token} for THIS browser's list
  let live = (() => {
    try { return JSON.parse(localStorage.getItem(LIVE_STORE) || "null"); }
    catch { return null; }
  })();
  function saveLive(v) {
    live = v;
    try { v ? localStorage.setItem(LIVE_STORE, JSON.stringify(v)) : localStorage.removeItem(LIVE_STORE); }
    catch { /* storage off */ }
  }
  function listPayload() { return { f: [...state.favorites], o: state.favoritesOnly ? 1 : 0 }; }

  async function liveCreate() {
    const r = await fetch(LIST_API + "/lists", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(listPayload()),
    });
    if (!r.ok) throw new Error("create " + r.status);
    return r.json();   // {id, token}
  }
  async function liveFetch(id) {
    const r = await fetch(LIST_API + "/lists/" + encodeURIComponent(id));
    if (!r.ok) throw new Error("fetch " + r.status);
    return (await r.json()).data;
  }
  // Ensure this browser has a live list (create on first share), then return it
  // — or null if the backend is off/unreachable (callers fall back to a hash link).
  async function ensureLive() {
    if (!LIST_API) return null;
    if (live) return live;
    try { saveLive(await liveCreate()); return live; }
    catch { return null; }
  }
  // Debounced PUT so a bound list stays current as the owner stars/unstars,
  // without a write per click.
  let livePushTimer = 0;
  function scheduleLivePush() {
    if (!LIST_API || !live) return;
    clearTimeout(livePushTimer);
    livePushTimer = setTimeout(() => {
      fetch(LIST_API + "/lists/" + encodeURIComponent(live.id), {
        method: "PUT",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + live.token },
        body: JSON.stringify(listPayload()),
      }).catch(() => { /* offline; the next edit retries */ });
    }, 1200);
  }

  const state = {
    bills: [],
    councils: new Set(),
    subjects: new Set(),
    years: new Set(),
    types: new Set(),
    statuses: new Set(),
    // Everything the councils publish is ingested and searchable, but the
    // default view is legislation: Maui alone files ~4,300 county
    // communications and ~1,600 committee reports, which would otherwise bury
    // actual bills in every result set.
    matterClasses: new Set(["legislation"]),
    search: "",
    // Map(billIndex -> relevance score) for the active query, recomputed once
    // per filter pass rather than per bill.
    searchHits: null,
    onlyStalled: false,
    favorites: loadFavSet(),
    favoritesOnly: false,
    sort: "recent",
  };

  // A bill is "stalled" when it's parked in the early/middle pipeline and hasn't
  // moved in a long while. Excludes terminal bills (enacted / failed) and ones
  // that already reached final reading — those aren't "stuck in committee".
  const STALLED_DAYS = 120;
  function isStalled(b) {
    const prog = billProgress(b);
    if (prog.state === "done" || prog.state === "failed") return false;
    if (prog.step >= 3) return false; // reached/ passed final reading
    const d = Date.parse(b.last_action_date || b.introduced_date || "");
    if (!d) return false;
    return Date.now() - d > STALLED_DAYS * 86400e3;
  }

  // Sort the filtered list. "recent"/"oldest" key off the latest action date
  // (falling back to introduced / first-seen); "number" is a natural sort.
  // Relevance only means anything while a query is active, so it becomes the
  // effective order the moment someone searches — unless they've explicitly
  // picked a sort. Ranking by date instead would bury the best match under
  // whatever happened to move most recently.
  function effectiveSort() {
    if (state.search && state.searchHits && !state.sortExplicit) return "relevance";
    return state.sort;
  }

  function sortBills(arr) {
    const mode = effectiveSort();
    if (mode === "relevance" && state.searchHits) {
      const hits = state.searchHits;
      return arr.slice().sort((a, b) => {
        const d = (hits.get(b._idx) || 0) - (hits.get(a._idx) || 0);
        if (d) return d;
        // Equally-scored matches still read newest-first.
        const ka = a.last_action_date || a.introduced_date || "";
        const kb = b.last_action_date || b.introduced_date || "";
        return ka === kb ? 0 : (ka < kb ? 1 : -1);
      });
    }
    if (mode === "number") {
      return arr.slice().sort((a, b) =>
        (a.bill_number || "").localeCompare(b.bill_number || "", undefined, { numeric: true }));
    }
    const dir = mode === "oldest" ? 1 : -1;
    const key = (b) => b.last_action_date || b.introduced_date || b.first_seen || "";
    return arr.slice().sort((a, b) => {
      const ka = key(a), kb = key(b);
      return ka === kb ? 0 : (ka < kb ? -dir : dir);
    });
  }

  function saveFavs() {
    try { localStorage.setItem(FAV_STORE, JSON.stringify([...state.favorites])); }
    catch { /* storage disabled/full — favorites just won't persist this session */ }
  }
  function toggleFav(b) {
    const k = favKey(b);
    if (state.favorites.has(k)) state.favorites.delete(k);
    else state.favorites.add(k);
    saveFavs();
    syncListHash();
    updateFavCount();
  }
  function updateFavCount() {
    const el = document.getElementById("fav-count");
    if (!el) return;
    const n = state.favorites.size;
    el.textContent = n ? String(n) : "";
    el.hidden = !n;
  }
  // Reflect the favorites-only toggle button state (replaces the old checkbox).
  function setFavoritesOnly(on) {
    state.favoritesOnly = on;
    const btn = document.getElementById("f-favorites");
    if (!btn) return;
    btn.classList.toggle("is-on", on);
    btn.setAttribute("aria-pressed", String(on));
    const use = btn.querySelector(".tb-star use");
    if (use) use.setAttribute("href", on ? "#i-star" : "#i-star-o");
  }
  function favButtonHtml(b) {
    const on = state.favorites.has(favKey(b));
    return `<button class="fav-btn${on ? " is-fav" : ""}" type="button" aria-pressed="${on}" ` +
      `title="${on ? "Remove from favorites" : "Save to favorites"}" ` +
      `aria-label="${on ? "Remove bill from favorites" : "Save bill to favorites"}">` +
      `<svg aria-hidden="true"><use href="#i-star${on ? "" : "-o"}"/></svg></button>`;
  }

  // Mirror the starred set (and the favorites-only view) into the URL hash, so
  // bookmarking or copying the page link reopens the exact list anywhere — no
  // backend. localStorage remains the everyday convenience cache; the URL is
  // the durable, device-independent copy. The payload is deflate-compressed
  // (#l=…, base64url) where CompressionStream exists, with the legacy
  // plain-JSON #list=… form as both fallback and backward-compat reader.
  const B64URL = { to: (s) => btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
                   from: (s) => atob(s.replace(/-/g, "+").replace(/_/g, "/")) };
  async function packList(payload) {
    const json = JSON.stringify(payload);
    if (typeof CompressionStream === "undefined") {
      return "list=" + encodeURIComponent(json);
    }
    const stream = new Blob([json]).stream().pipeThrough(new CompressionStream("deflate-raw"));
    const buf = new Uint8Array(await new Response(stream).arrayBuffer());
    let bin = "";
    for (const b of buf) bin += String.fromCharCode(b);
    return "l=" + B64URL.to(bin);
  }
  async function unpackList(hash) {
    let m = /[#&]l=([^&]+)/.exec(hash);
    if (m && typeof DecompressionStream !== "undefined") {
      try {
        const bin = B64URL.from(m[1]);
        const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
        const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
        return JSON.parse(await new Response(stream).text());
      } catch { /* corrupted/truncated link — fall through */ }
    }
    m = /[#&]list=([^&]+)/.exec(hash);
    if (m) {
      try { return JSON.parse(decodeURIComponent(m[1])); } catch { /* ignore */ }
    }
    return null;
  }
  let hashSyncSeq = 0;
  async function syncListHash() {
    const seq = ++hashSyncSeq;
    let hash = "";
    if (LIST_API && live) {
      // Bound to a server-backed list: keep the short, stable #id= link and
      // push the edit (debounced) so the live link stays current.
      hash = "#id=" + live.id;
      scheduleLivePush();
    } else if (state.favorites.size) {
      hash = "#" + await packList({ f: [...state.favorites], o: state.favoritesOnly ? 1 : 0 });
    }
    if (seq !== hashSyncSeq) return; // a newer sync superseded this one
    history.replaceState(null, "", hash || location.pathname + location.search);
  }
  function mergeFavorites(payload) {
    let changed = false;
    for (const k of payload.f || []) {
      if (!state.favorites.has(k)) { state.favorites.add(k); changed = true; }
    }
    if (changed) saveFavs();
    if (payload.o) state.favoritesOnly = true;
  }
  // On load, fold any list from the URL into the local favorites (union — never
  // drops stars already saved on this device) and restore the favorites-only view.
  async function restoreListFromHash() {
    // Server-backed live link (#id=…): fetch the current contents. The owner's
    // own browser stays bound (it has the token); a recipient just merges and
    // remains a viewer (no token → their later edits fork to a snapshot link).
    const idm = /[#&]id=([A-Za-z0-9]+)/.exec(location.hash);
    if (idm && LIST_API) {
      try { mergeFavorites({ f: [], ...(await liveFetch(idm[1])) }); return; }
      catch { /* server down / 404 — fall through to hash forms */ }
    }
    const payload = await unpackList(location.hash);
    if (payload) mergeFavorites(payload);
  }

  // ---- Shareable filter URLs ------------------------------------------------
  // The active filter combination is mirrored into the query string so a
  // filtered view is itself a shareable, bookmarkable link. The list hash
  // (#l=/#id=) and bill deep-link hash (#bill=) are independent and preserved
  // alongside it. Only dimensions that deviate from their defaults are emitted.
  function serializeFilters() {
    const it = state._items || {};
    const p = new URLSearchParams();
    const all = (set, items) => items && set.size === items.length;
    const dimParam = (key, set, items) => {
      if (!items || all(set, items)) return;            // at default "all" → omit
      p.set(key, set.size ? [...set].join(",") : "none");
    };
    if (state.search) p.set("q", state.search);
    dimParam("county", state.councils, it.council);
    dimParam("type", state.types, it.type);
    dimParam("subject", state.subjects, it.subject);
    // Status and year now default to "everything", so they only reach the URL
    // once the reader has actually narrowed them.
    if (!all(state.statuses, it.status)) {
      p.set("status", state.statuses.size ? [...state.statuses].join(",") : "none");
    }
    if (!all(state.years, it.year)) {
      p.set("year", state.years.size ? [...state.years].join(",") : "none");
    }
    const classDefault = state.matterClasses.size === 1 && state.matterClasses.has("legislation");
    if (!classDefault) {
      p.set("class", all(state.matterClasses, it.matterClass) ? "all"
        : state.matterClasses.size ? [...state.matterClasses].join(",") : "none");
    }
    if (state.onlyStalled) p.set("stalled", "1");
    if (state.favoritesOnly) p.set("fav", "1");
    return p.toString();
  }
  // Write the filter query string without disturbing the hash (lists / #bill=).
  function syncFilterUrl() {
    if (!state._items) return;
    const qs = serializeFilters();
    history.replaceState(null, "",
      location.pathname + (qs ? "?" + qs : "") + location.hash);
  }
  // Restore filter state from the query string on load (before first render).
  function applyFilterParams() {
    const it = state._items || {};
    const p = new URLSearchParams(location.search);
    if (![...p.keys()].length) return;
    const setDim = (key, set, items) => {
      if (!p.has(key) || !items) return;
      const v = p.get(key);
      set.clear();
      if (v === "none") return;
      if (v === "all") { items.forEach((x) => set.add(x.value)); return; }
      const valid = new Set(items.map((x) => x.value));
      v.split(",").forEach((x) => { if (valid.has(x)) set.add(x); });
    };
    if (p.has("q")) {
      state.search = p.get("q").toLowerCase().trim();
      const s = document.getElementById("f-search");
      if (s) s.value = p.get("q");
      const sc = document.getElementById("f-search-clear");
      if (sc) sc.hidden = !state.search;
    }
    setDim("county", state.councils, it.council);
    setDim("type", state.types, it.type);
    setDim("subject", state.subjects, it.subject);
    setDim("status", state.statuses, it.status);
    setDim("year", state.years, it.year);
    setDim("class", state.matterClasses, it.matterClass);
    if (p.get("stalled") === "1") {
      state.onlyStalled = true;
      const sf = document.getElementById("f-stalled"); if (sf) sf.checked = true;
    }
    if (p.get("fav") === "1") state.favoritesOnly = true;
    renderFilterGroups();
  }

  // Bill types collapse into 3 plain buckets. Councils emit ~9 raw types, most
  // of them procedural noise a regular reader doesn't care about.
  const TYPE_BUCKETS = ["Laws", "Resolutions", "Procedural & ceremonial"];
  function typeBucket(t) {
    const x = (t || "").toLowerCase().trim();
    if (x === "bill" || x === "ordinance") return "Laws";
    if (x === "resolution") return "Resolutions";
    return "Procedural & ceremonial"; // committee reports, Rule 7(B), ceremonial, etc.
  }

  // The 10 normalized status labels collapse into 3 outcome buckets for the
  // filter dropdown. The per-row stepper still shows the precise stage.
  const STATUS_BUCKETS = ["Active", "Passed", "Dead"];
  const STATUS_BUCKET_OF = {
    "Introduced": "Active", "Scheduled": "Active", "In committee": "Active",
    "In progress": "Active", "Passed 1st reading": "Active",
    "Passed final reading": "Active", "Tracking": "Active",
    "Adopted / enacted": "Passed",
    "Stalled": "Dead", "Failed": "Dead",
  };
  function statusBucket(b) {
    return STATUS_BUCKET_OF[normalizeStatus(b).label] || "Active";
  }

  // Year a bill belongs to: prefer introduced date, fall back to last action.
  function billYear(b) {
    const d = b.introduced_date || b.last_action_date || "";
    return /^\d{4}/.test(d) ? d.slice(0, 4) : null;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Each council reports status differently (Honolulu uses committee codes,
  // Maui says "Agenda Ready", Hawaii County has real reading stages, Kauai is
  // sparse). Collapse the mess into a small set of plain-language stages that
  // a regular person can follow, derived from the status field + last action.
  // Order matters: terminal/late stages are checked before earlier ones.
  function normalizeStatus(b) {
    const s = (b.status || "").toLowerCase().trim();
    const a = (b.last_action || "").toLowerCase();
    // Strong signals from the status field first.
    if (/adopt|enact|became law|approved/.test(s)) return { label: "Adopted / enacted", cls: "st-law" };
    if (/fail|defeat|withdraw|died|reject/.test(s)) return { label: "Failed", cls: "st-failed" };
    if (/postpone|defer|tabled|held/.test(s)) return { label: "Stalled", cls: "st-stalled" };
    if (/second.*reading|final reading/.test(s)) return { label: "Passed final reading", cls: "st-pass" };
    if (/first reading/.test(s)) return { label: "Passed 1st reading", cls: "st-progress" };
    if (/committee/.test(s)) return { label: "In committee", cls: "st-progress" };
    // Then the descriptive last-action text.
    if (/became law|enacted|signed by the mayor/.test(a)) return { label: "Adopted / enacted", cls: "st-law" };
    if (/second\s*(?:&|and)?\s*(?:final\s*)?reading|passes second|final reading/.test(a)) return { label: "Passed final reading", cls: "st-pass" };
    if (/first reading|passes first/.test(a)) return { label: "Passed 1st reading", cls: "st-progress" };
    if (/postpone|deferred|tabled|recommit|continued/.test(a)) return { label: "Stalled", cls: "st-stalled" };
    if (/\bfail|defeat|withdraw|killed|not adopt/.test(a)) return { label: "Failed", cls: "st-failed" };
    if (/adopts? res|\badopted\b/.test(a)) return { label: "Adopted / enacted", cls: "st-law" };
    if (/committee|referred|forwarded|recommend/.test(a)) return { label: "In committee", cls: "st-progress" };
    if (/introduc/.test(a)) return { label: "Introduced", cls: "st-early" };
    if (/agenda|scheduled/.test(a) || /agenda ready/.test(s)) return { label: "Scheduled", cls: "st-early" };
    if (s) return { label: "In progress", cls: "st-progress" };
    return { label: "Tracking", cls: "st-unknown" };
  }

  // The 5 steps of a county bill's life. We place each bill on this track and
  // mark whether it's still moving (active), done (enacted), or off-track
  // (stalled / failed).
  const STEP_NAMES = ["Introduced", "Committee", "1st reading", "Final reading", "Enacted"];

  // Step derived from the normalized label so the stepper always agrees with
  // the stage badge (council status fields can disagree with last-action text).
  const STEP_OF = {
    "Introduced": 0, "Scheduled": 0, "Tracking": 0,
    "In committee": 1, "In progress": 1,
    "Passed 1st reading": 2,
    "Passed final reading": 3,
    "Adopted / enacted": 4,
    "Stalled": 1, "Failed": 1,
  };

  function billProgress(b) {
    const st = normalizeStatus(b);
    let step = STEP_OF[st.label] ?? 0;
    let state = "active";
    if (st.cls === "st-law") { state = "done"; step = 4; }
    else if (st.cls === "st-failed") state = "failed";
    else if (st.cls === "st-stalled") state = "stalled";
    else if (st.cls === "st-unknown") state = "unknown";
    // For off-track bills, place the marker at the furthest stage reached.
    if (state === "failed" || state === "stalled") {
      const t = ((b.last_action || "") + " " + (b.status || "")).toLowerCase();
      if (/second.*reading|final reading|passes second/.test(t)) step = 3;
      else if (/first reading|passes first/.test(t)) step = 2;
      else if (/committee|referred|forwarded|recommend/.test(t)) step = 1;
    }
    return { step, state, label: st.label };
  }

  function renderStepper(prog, labeled) {
    const { step, state } = prog;
    let html = `<div class="stepper${labeled ? " stepper-labeled" : ""}">`;
    for (let i = 0; i < 5; i++) {
      let dot;
      if (state === "done" || i < step) dot = "done";
      else if (i === step) dot = "current-" + state;
      else dot = "future";
      const seg = (state === "done" || i < step) && i < 4 ? " seg-done" : "";
      html += `<div class="step ${dot}${seg}"><span class="dot"></span>${
        labeled ? `<span class="step-name">${STEP_NAMES[i]}</span>` : ""
      }</div>`;
    }
    return html + "</div>";
  }

  // A readable headline for a bill — what it's actually ABOUT, not the legal
  // framing. Built from, in order of preference:
  //   1. the subject of the "RELATING TO …" clause (the legal statement of
  //      what the measure covers), combined with
  //   2. a trailing parenthetical short-title when the council tacked one on
  //      (e.g. "(Long-Term Affordable Rental Requirements)") — trailing only:
  //      mid-title parentheticals are legal asides like "(2016 EDITION, AS
  //      AMENDED)" or "(Public Laws 93-383 And 100-242)", not short titles;
  //   3. otherwise the full title with lead-in boilerplate trimmed.
  // ALL-CAPS results are converted to readable title case (known acronyms kept).
  const PAREN_NOISE_RE = new RegExp(
    "^draft\\s*\\d|^\\d+$|^\\d{4}\\b|public hearing|edition|as amended" +
    "|public law|^see\\b|^memo|^comm\\b|\\bno\\.\\s*\\d|^for condemnation$",
    "i"
  );
  const SMALL_WORDS = new Set([
    "a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with",
  ]);
  const KNOWN_ACRONYMS = new Set([
    ...Object.keys(GLOSSARY),
    "USA", "US", "HSAC", "CDBG", "PEG", "YWCA", "YMCA", "FY", "II", "III", "IV",
  ]);

  // A string counts as ALL-CAPS when it has no real lowercase or Title-case
  // word — isolated lowercase letters inside codes ("A-20a", "RS-10a") don't
  // disqualify it.
  function isAllCaps(s) {
    return !/\b[a-z]{2,}|[A-Z][a-z]{2,}/.test(s);
  }

  // Convert an ALL-CAPS legal title to title case; mixed-case input is
  // returned untouched. Words the glossary knows (HUD, RPT, …) stay caps so
  // the acronym tooltips still match, and mixed-case codes ("A-20a") are kept.
  function readableCase(s) {
    if (!s || !isAllCaps(s)) return s;
    let first = true;
    return s.replace(/[\p{L}\p{N}'’‘ʻ-]+/gu, (w) => {
      if (KNOWN_ACRONYMS.has(w)) return w;
      if (/[a-z]/.test(w)) return w; // zoning/code token like "A-20a"
      const lw = w.toLowerCase();
      if (!first && SMALL_WORDS.has(lw)) return lw;
      first = false;
      return lw
        .split("-")
        .map((seg) => (SMALL_WORDS.has(seg) ? seg : seg.charAt(0).toUpperCase() + seg.slice(1)))
        .join("-");
    });
  }

  // Records scraped before summary/title separation carry the Title-case staff
  // summary glued onto the ALL-CAPS legal title ("…AND GARAGES Removes
  // commercial parking lots…"); keep only the caps title.
  function capsCore(t) {
    const m = t.match(/[A-Z][a-z]{2,}/);
    if (!m || m.index < 30) return t;
    const prefix = t.slice(0, m.index);
    return isAllCaps(prefix) ? prefix.trim() : t;
  }

  // A short-title tag the council appended at the END of the title; mid-title
  // parentheticals are statutory asides and never used as headlines.
  function shortTitleTag(t) {
    const m = t.match(/\(([^()]{5,90})\)\s*\.?\s*$/);
    if (!m) return "";
    const p = m[1].replace(/\s{2,}/g, " ").trim();
    if (PAREN_NOISE_RE.test(p)) return "";
    if (p.split(/\s+/).length < 2 || !/[A-Za-z]{3}/.test(p)) return "";
    return p;
  }

  function billHeadline(b) {
    const raw = (b.title || "").trim();
    if (!raw) return "";
    const tag = shortTitleTag(raw);
    // A trailing short-title tag means the title is already clean; otherwise
    // shear off any staff summary glued onto an ALL-CAPS title.
    const t = tag ? raw : capsCore(raw);
    // "RELATING TO X" / "RELATES TO X": X is the substance. Stop at a
    // parenthetical, sentence end, or a trailing fiscal-year span.
    const rel = t.match(
      /\bRELAT(?:ING|ES|ED)\s+TO\s+(?:THE\s+)?(.+?)(?=\s*\(|\s*[.;]|\s+FOR\s+THE\s+FISCAL\s+YEAR\b|\s*$)/i
    );
    const subject = rel && rel[1].trim().replace(/,\s*$/, "");
    if (subject && tag) return `${readableCase(subject)} — ${readableCase(tag)}`;
    if (tag) return readableCase(tag);
    if (subject && (subject.split(/\s+/).length >= 2 || subject.length >= 6)) {
      return readableCase(subject);
    }
    const trimmed = t
      .replace(/^A BILL FOR AN ORDINANCE\s+/i, "")
      .replace(/^AN?\s+ORDINANCE\s+/i, "")
      .replace(/^A BILL\s+(?:TO|FOR)\s+/i, "")
      .replace(/^A RESOLUTION\s+/i, "")
      // "RESOLUTION 26-84, …" / "BILL 61 (2026), …" — the number repeats the
      // Number column; keep only what follows.
      .replace(/^(?:BILL|RESOLUTION)\s+(?:NO\.?\s*)?\d[\d-]*\s*(?:\(\d{4}\))?\s*,\s*/i, "")
      .replace(/^RESOLUTION\s+/i, "")
      // trailing committee file code, e.g. "(BFED-60)" or "(WASSP-1(20))"
      .replace(/\s*\(\s*[A-Za-z]{2,8}-\d+(?:\([^)]*\))?[^)]*\)\s*$/, "")
      .replace(/\s{2,}/g, " ")
      .trim();
    return readableCase(trimmed) || t;
  }

  // Escape, then wrap recognized acronyms in <abbr> tooltips. One pass per
  // pattern over already-escaped text — no nesting or double substitution.
  function annotate(raw) {
    let s = escapeHtml(raw);
    s = s.replace(INLINE_RE, (m) => `<abbr class="gloss" title="${GLOSSARY[m]}">${m}</abbr>`);
    s = s.replace(DRAFT_RE, (full, kind) =>
      `<abbr class="gloss" title="${DRAFT_TITLE[kind]}">${full}</abbr>`
    );
    return s;
  }

  // Wrap occurrences of the search term in <mark>, operating only on the text
  // between tags so it never corrupts the <abbr> markup that annotate() emits.
  function highlightHtml(html, query) {
    if (!query) return html;
    const { phrases, terms } = parseQuery(query);
    const all = [...phrases, ...terms].filter(Boolean);
    if (!all.length) return html;
    // Longest first so a quoted phrase wins over its own component words.
    all.sort((a, b) => b.length - a.length);
    const re = new RegExp("(" + all.map(termRegexSource).join("|") + ")", "ig");
    return html.split(/(<[^>]+>)/).map((seg) =>
      seg.startsWith("<") ? seg : seg.replace(re, "<mark>$1</mark>")
    ).join("");
  }

  function relTime(ts) {
    if (!ts) return "";
    const secs = (Date.now() - new Date(ts).getTime()) / 1000;
    if (secs < 90) return "just now";
    if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
    if (secs < 86400) return `${Math.round(secs / 3600)} hr ago`;
    return `${Math.round(secs / 86400)} days ago`;
  }

  // ---- Motion ---------------------------------------------------------------
  // One global switch: every JS-driven animation checks this, and styles.css
  // has a matching @media (prefers-reduced-motion) kill block for CSS ones.
  const REDUCED_MOTION = !!(window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  function countUp(el, target) {
    const fmt = (n) => n.toLocaleString();
    if (REDUCED_MOTION || target < 10) { el.textContent = fmt(target); return; }
    const t0 = performance.now();
    const dur = 650;
    const tick = (t) => {
      const p = Math.min(1, (t - t0) / dur);
      el.textContent = fmt(Math.round(target * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  // Discrete filter changes (chips, segments, checkboxes, sort) crossfade the
  // list via the View Transitions API where available; continuous input
  // (typing in search) never animates. Handlers opt in by calling animateNext()
  // right before their applyFilters().
  let animateNextApply = false;
  function animateNext() { animateNextApply = true; }

  // Progress steppers fill in the first time they scroll into view.
  let stepObserver = null;
  function observeSteppers(tbody) {
    if (REDUCED_MOTION || !("IntersectionObserver" in window)) return;
    if (!stepObserver) {
      stepObserver = new IntersectionObserver((entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.remove("pre");
            stepObserver.unobserve(e.target);
          }
        }
      }, { threshold: 0.4 });
    }
    for (const s of tbody.querySelectorAll(".stepper")) {
      s.classList.add("pre");
      stepObserver.observe(s);
    }
  }

  function setMeta(payload) {
    const ts = payload.last_scrape?.completed_at || payload.generated_at;
    const exact = ts ? new Date(ts).toLocaleString() : "—";
    const rel = ts ? relTime(ts) : exact;
    const el = document.getElementById("meta");
    el.innerHTML =
      `<span class="live-dot" aria-hidden="true"></span>` +
      `<span>updated ${escapeHtml(rel)}</span>`;
    el.title = `Data current as of ${exact} — refreshes Mon, Wed & Fri`;
    state.dataTs = ts ? Date.parse(ts) : 0;
  }

  // Masthead stat strip. Counts animate up on the first load only — the daily
  // background refetch and tab-refocus reloads just swap the numbers.
  let statsAnimated = false;
  function setStats(payload) {
    const totalEl = document.getElementById("stat-total");
    const activeEl = document.getElementById("stat-active");
    if (!totalEl || !activeEl) return;
    const newest = [...new Set(payload.bills.map(billYear).filter(Boolean))].sort().at(-1);
    const active = payload.bills.filter(
      (b) => statusBucket(b) === "Active" && billYear(b) === newest
    ).length;
    if (statsAnimated) {
      totalEl.textContent = payload.bills.length.toLocaleString();
      activeEl.textContent = active.toLocaleString();
      return;
    }
    statsAnimated = true;
    countUp(totalEl, payload.bills.length);
    countUp(activeEl, active);
  }

  // "What changed?" at a glance: flag bills first seen in the latest scrapes
  // as New, and bills whose status/action moved recently as Updated. Anchored
  // to the data's own timestamp, not the wall clock, so a stale open tab
  // doesn't silently drop its badges.
  const NEW_WINDOW_MS = 5 * 86400e3;       // ~2 scrape cycles (Mon/Wed/Fri)
  const UPDATED_WINDOW_MS = 3 * 86400e3;   // ~1 scrape cycle
  function rowBadgeHtml(b) {
    if (!state.dataTs) return "";
    const fs = Date.parse(b.first_seen || "") || 0;
    const lu = Date.parse(b.last_updated || "") || 0;
    if (fs && state.dataTs - fs < NEW_WINDOW_MS) return '<span class="row-badge new">New</span>';
    if (lu && state.dataTs - lu < UPDATED_WINDOW_MS) return '<span class="row-badge upd">Updated</span>';
    // Recent movement takes precedence; a stalled bill is by definition not one.
    if (isStalled(b)) return '<span class="row-badge stalled" title="No action in 120+ days">Stalled</span>';
    return "";
  }

  function buildGlossaryPanel() {
    const dl = document.getElementById("glossary-list");
    dl.innerHTML = "";
    for (const [term, def] of Object.entries(GLOSSARY)) {
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = def;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
  }

  // Multi-select checkbox group with a leading "All" box. "All" checks/unchecks
  // every option; checking all options re-checks "All". `opts.pill` renders each
  // option as its colored subject pill (used for the Subject group).
  function renderCheckGroup(containerId, items, selectedSet, opts = {}) {
    const c = document.getElementById(containerId);
    c.innerHTML = "";
    const itemBoxes = [];

    const allLabel = document.createElement("label");
    allLabel.className = "chk-all";
    const allCb = document.createElement("input");
    allCb.type = "checkbox";
    allCb.checked = selectedSet.size === items.length;
    const allTxt = document.createElement("span");
    allTxt.textContent = "All";
    allLabel.append(allCb, allTxt);
    c.appendChild(allLabel);

    for (const it of items) {
      const label = document.createElement("label");
      label.className = opts.pill ? "subj-check" : "county-check";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = selectedSet.has(it.value);
      cb.addEventListener("change", () => {
        if (cb.checked) selectedSet.add(it.value);
        else selectedSet.delete(it.value);
        allCb.checked = selectedSet.size === items.length;
        animateNext();
        applyFilters();
      });
      const node = document.createElement("span");
      node.className = opts.pill ? "subject-pill " + it.value : "county-badge";
      node.textContent = it.label;
      label.append(cb, node);
      if (opts.counts && opts.counts[it.value] != null) {
        const n = document.createElement("span");
        n.className = "opt-count";
        n.textContent = opts.counts[it.value].toLocaleString();
        label.append(n);
      }
      c.appendChild(label);
      itemBoxes.push({ cb, value: it.value });
    }

    allCb.addEventListener("change", () => {
      selectedSet.clear();
      if (allCb.checked) for (const it of items) selectedSet.add(it.value);
      for (const { cb, value } of itemBoxes) cb.checked = selectedSet.has(value);
      animateNext();
      applyFilters();
    });
  }

  function uniqueSorted(arr) {
    return [...new Set(arr.filter(Boolean))].sort();
  }

  function populateSelect(id, values) {
    const sel = document.getElementById(id);
    const current = sel.value;
    sel.innerHTML = '<option value="">All</option>';
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      sel.appendChild(opt);
    }
    if (values.includes(current)) sel.value = current;
  }

  // Column-header filter popovers (County, Subjects). The menu inside each is
  // the same renderCheckGroup() checkbox group that used to sit in the filter
  // card — just rendered into a popover anchored under the column header.
  let openColPop = null;
  function closeColPop(restoreFocus) {
    if (!openColPop) return;
    const btn = openColPop.btn;
    openColPop.pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    openColPop = null;
    if (restoreFocus) btn.focus();
  }
  function positionColPop(btn, pop) {
    const r = btn.getBoundingClientRect();
    pop.style.top = Math.round(r.bottom + 4) + "px";
    let left = r.left;
    if (left + pop.offsetWidth > window.innerWidth - 8) {
      left = window.innerWidth - pop.offsetWidth - 8;
    }
    pop.style.left = Math.max(8, Math.round(left)) + "px";
  }
  function wireColumnFilter(btnId, popId) {
    const btn = document.getElementById(btnId);
    const pop = document.getElementById(popId);
    if (!btn || !pop) return;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = openColPop && openColPop.pop === pop;
      closeColPop();
      if (!wasOpen) {
        pop.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        openColPop = { btn, pop };
        positionColPop(btn, pop);
        pop.querySelector("input")?.focus(); // a11y: move focus into the menu
      }
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    // a11y: tabbing (or clicking) out of the menu closes it
    pop.addEventListener("focusout", (e) => {
      if (e.relatedTarget && e.relatedTarget !== btn && !pop.contains(e.relatedTarget)) {
        closeColPop(false);
      }
    });
  }
  // Reflect an applied (non-"All") filter on the header: highlight + a count badge.
  function updateColumnFilterIndicators() {
    for (const [id, n, total] of [
      ["cf-council-btn", state.councils.size, state.councilUniverse],
      ["cf-subject-btn", state.subjects.size, state.subjectUniverse],
      ["cf-type-btn", state.types.size, state.typeUniverse],
      ["cf-status-btn", state.statuses.size, state.statusUniverse],
      ["cf-class-btn", state.matterClasses.size, state.matterClassUniverse],
    ]) {
      const btn = document.getElementById(id);
      if (!btn) continue;
      const active = !!(total && n < total);
      btn.classList.toggle("is-active", active);
      const badge = btn.querySelector(".cf-badge");
      if (badge) {
        badge.hidden = !active;
        badge.textContent = active ? String(n) : "";
      }
    }
  }

  // Render all five checkbox groups from the stashed item lists + current Sets.
  // Used on load and after a chip removal / reset re-syncs the Sets.
  // How many bills fall under each filter option, across the whole dataset —
  // shown beside each checkbox so the size of a choice is visible before
  // picking it. (Totals, not cross-filtered, so the numbers stay stable.)
  function filterCounts() {
    const c = { council: {}, subject: {}, type: {}, status: {}, matterClass: {} };
    for (const b of state.bills) {
      c.council[b.council] = (c.council[b.council] || 0) + 1;
      const tb = typeBucket(b.bill_type); c.type[tb] = (c.type[tb] || 0) + 1;
      const sb = statusBucket(b); c.status[sb] = (c.status[sb] || 0) + 1;
      const mc = b.matter_class || "legislation";
      c.matterClass[mc] = (c.matterClass[mc] || 0) + 1;
      for (const s of b.subjects || []) c.subject[s] = (c.subject[s] || 0) + 1;
    }
    return c;
  }
  function renderFilterGroups() {
    const it = state._items;
    if (!it) return;
    const counts = filterCounts();
    // County/Type/Subject/Status are filtered only through the column-header
    // popovers (f-*). On mobile the header collapses to a compact filter bar
    // (see styles.css) but the same buttons/popovers stay the single source.
    const groups = [
      ["council", it.council, state.councils, { counts: counts.council }],
      ["subject", it.subject, state.subjects, { pill: true, counts: counts.subject }],
      ["type", it.type, state.types, { counts: counts.type }],
      ["status", it.status, state.statuses, { counts: counts.status }],
      ["class", it.matterClass, state.matterClasses, { counts: counts.matterClass }],
    ];
    for (const [name, items, set, opts] of groups) {
      if (document.getElementById("f-" + name)) renderCheckGroup("f-" + name, items, set, opts);
    }
    renderYearControl();
  }

  // Year is a single-select segmented control (newest year selected by default,
  // plus an "All" segment) — clearer than a checkbox list for a 3-value filter.
  // A .seg-pill element slides behind the active label (transform/width
  // transition in CSS); buttons stay plain so keyboard/AT semantics are simple.
  function positionSegPill(c) {
    const pill = c.querySelector(".seg-pill");
    const active = c.querySelector(".seg.active");
    if (!pill) return;
    if (!active) { pill.style.opacity = "0"; return; }
    pill.style.opacity = "1";
    pill.style.width = active.offsetWidth + "px";
    pill.style.transform = `translateX(${active.offsetLeft}px)`;
  }
  function selectYear(value) {
    const years = state._items?.year || [];
    state.years.clear();
    if (value === null) years.forEach((y) => state.years.add(y.value));
    else state.years.add(value);
    renderYearControl();
    animateNext();
    applyFilters();
  }
  function renderYearControl() {
    const c = document.getElementById("f-year");
    const years = state._items?.year;
    if (!c || !years) return;
    // Build the buttons once per year-set; clicks only re-sync classes, so the
    // pill actually slides instead of being torn down and rebuilt in place.
    const key = years.map((y) => y.value).join(",");
    if (c.dataset.key !== key) {
      c.dataset.key = key;
      c.innerHTML = '<span class="seg-pill" aria-hidden="true"></span>';
      const mk = (label, value) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "seg";
        if (value !== null) b.dataset.year = value;
        b.textContent = label;
        b.addEventListener("click", () => selectYear(value));
        c.appendChild(b);
      };
      mk("All", null);
      for (const y of years) mk(y.label, y.value);
    }
    const allActive = state.years.size === years.length;
    for (const b of c.querySelectorAll(".seg")) {
      const v = b.dataset.year;
      const active = v != null
        ? (!allActive && state.years.size === 1 && state.years.has(v))
        : allActive;
      b.classList.toggle("active", active);
      b.setAttribute("aria-pressed", String(active));
    }
    requestAnimationFrame(() => positionSegPill(c));
  }

  // Overflow "⋯" menu in the toolbar (classified toggle, copy link, help).
  // Light / dark / system theme. The <head> script applies the stored choice
  // before paint; here we wire the overflow-menu control that cycles it and
  // keep "system" live to OS changes.
  function wireThemeToggle() {
    const btn = document.getElementById("theme-btn");
    if (!btn) return;
    const label = btn.querySelector(".mi-label");
    const order = ["system", "light", "dark"];
    const names = { system: "System", light: "Light", dark: "Dark" };
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const stored = () => {
      try { return localStorage.getItem("tracker:theme") || "system"; } catch { return "system"; }
    };
    const apply = (pref) => {
      const dark = pref === "dark" || (pref === "system" && mq.matches);
      document.documentElement.dataset.theme = dark ? "dark" : "light";
      if (label) label.textContent = "Theme: " + names[pref];
    };
    apply(stored());
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); // cycle without closing the menu
      const next = order[(order.indexOf(stored()) + 1) % order.length];
      try { localStorage.setItem("tracker:theme", next); } catch { /* ignore */ }
      apply(next);
    });
    mq.addEventListener?.("change", () => { if (stored() === "system") apply("system"); });
  }

  function wireToolbarMenu() {
    const btn = document.getElementById("more-btn");
    const pop = document.getElementById("more-pop");
    if (!btn || !pop) return;
    const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.hidden;
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => { if (!pop.hidden) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  }

  // Sort dropdown — a custom popover (matching the other toolbar menus) in
  // place of a native <select>, so its options inherit the toolbar styling.
  function wireSortMenu() {
    const btn = document.getElementById("sort-btn");
    const pop = document.getElementById("sort-pop");
    const label = document.getElementById("sort-label");
    if (!btn || !pop) return;
    const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.hidden;
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => { if (!pop.hidden) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
    for (const opt of pop.querySelectorAll(".sort-opt")) {
      opt.addEventListener("click", () => {
        state.sort = opt.dataset.sort;
        // An explicit pick sticks: after this, typing a query no longer
        // silently promotes relevance over the order they asked for.
        state.sortExplicit = true;
        for (const o of pop.querySelectorAll(".sort-opt")) {
          const sel = o === opt;
          o.classList.toggle("is-sel", sel);
          o.setAttribute("aria-checked", String(sel));
        }
        if (label) label.textContent = opt.querySelector(".mi-label").textContent;
        close();
        animateNext();
        applyFilters();
      });
    }
  }

  // Keep the sort button honest about the order actually in effect — searching
  // switches to relevance implicitly, and the label has to say so.
  function syncSortLabel() {
    const label = document.getElementById("sort-label");
    if (!label) return;
    const mode = effectiveSort();
    const opt = document.querySelector(`#sort-pop .sort-opt[data-sort="${mode}"]`);
    label.textContent = mode === "relevance" && !opt
      ? "Best match"
      : (opt?.querySelector(".mi-label")?.textContent || label.textContent);
  }

  // ---- Saved panel: share / subscribe to the starred list -------------------
  // Per-bill Atom feeds are generated at scrape time under feeds/bill/<slug>.xml
  // (tracker/legislative/feeds.py). billSlug() must mirror feeds.bill_slug().
  const SITE_BASE = "https://dtomkatsu.github.io/Tracker/";
  function billSlug(b) {
    return (b.council + "-" + b.bill_number).toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }
  function favBills() {
    return state.bills.filter((b) => state.favorites.has(favKey(b)));
  }
  function escapeXml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&apos;");
  }
  // OPML: one feed outline per starred bill. Any RSS reader imports this in
  // one step, subscribing the user to exactly their bills — updates then
  // arrive wherever their reader delivers (app, push, or email).
  function opmlForFavorites() {
    const outlines = favBills().map((b) =>
      `    <outline type="rss" text="${escapeXml(`${b.bill_number} — ${COUNCIL_LABEL[b.council] || b.council}`)}" ` +
      `title="${escapeXml(billHeadline(b) || b.bill_number)}" ` +
      `xmlUrl="${SITE_BASE}feeds/bill/${billSlug(b)}.xml" htmlUrl="${escapeXml(b.url)}"/>`
    ).join("\n");
    return `<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n  <head>\n    <title>My Hawaiʻi county bill list</title>\n  </head>\n  <body>\n${outlines}\n  </body>\n</opml>\n`;
  }
  function renderSavedPanel() {
    const list = document.getElementById("sp-bills");
    const empty = document.getElementById("sp-empty");
    const count = document.getElementById("sp-count");
    const qrHost = document.getElementById("sp-qr");
    const bills = favBills();
    count.textContent = String(bills.length);
    count.hidden = !bills.length;
    empty.hidden = !!bills.length;
    list.innerHTML = "";
    for (const b of bills.slice(0, 30)) {
      const li = document.createElement("li");
      li.innerHTML = `<span class="sp-num">${escapeHtml(b.bill_number)}</span>` +
        `<span class="sp-head">${escapeHtml(billHeadline(b) || b.title || "")}</span>`;
      list.appendChild(li);
    }
    if (bills.length > 30) {
      const li = document.createElement("li");
      li.className = "sp-more";
      li.textContent = `…and ${bills.length - 30} more`;
      list.appendChild(li);
    }
    // QR of the list URL — scan with a phone to move the whole list across.
    qrHost.innerHTML = "";
    qrHost.hidden = !bills.length;
    if (bills.length && typeof qrcode === "function") {
      (async () => {
        // Prefer the short, stable live link when the backend is on; otherwise
        // the compressed snapshot hash. Either way encode the actual address bar.
        if (LIST_API && await ensureLive()) {
          history.replaceState(null, "", "#id=" + live.id);
        } else {
          await syncListHash();
        }
        try {
          const qr = qrcode(0, "M");
          qr.addData(location.href);
          qr.make();
          qrHost.innerHTML = qr.createSvgTag({ cellSize: 3, margin: 2, scalable: true });
        } catch { qrHost.hidden = true; } // list too large for one QR — link still works
      })();
    }
  }
  function wireSavedPanel() {
    const btn = document.getElementById("saved-menu-btn");
    const pop = document.getElementById("saved-pop");
    if (!btn || !pop) return;

    // When the live backend is configured, reframe the share copy from
    // "snapshot" to "live, auto-updating".
    if (LIST_API) {
      const lbl = document.querySelector("#sp-copy .mi-label");
      if (lbl) lbl.textContent = "Copy live link";
      const note = pop.querySelector(".sp-note");
      if (note) {
        note.innerHTML =
          "Your link (and QR code) stays <strong>live</strong> — edit your list and " +
          "anyone holding the link or bookmark sees the update, no account needed. " +
          "The OPML file subscribes any RSS reader (Feedly, NetNewsWire, Inoreader) " +
          'to each bill’s update feed — or follow <a href="feeds/all.xml">every ' +
          "update</a>. For email alerts, paste a feed into a free RSS-to-email service " +
          "like Blogtrottr.";
      }
    }
    const close = () => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = pop.hidden;
      if (open) renderSavedPanel();
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    });
    pop.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => { if (!pop.hidden) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

    const flash = (id, text, revert) => {
      const el = document.querySelector(`#${id} .mi-label`);
      if (!el) return;
      el.textContent = text;
      setTimeout(() => { el.textContent = revert; }, 2000);
    };
    // With the backend on, leading copy uses a live link (updates as you edit);
    // off, it's a snapshot. The button label/note are adjusted in renderSavedPanel.
    const copyDefault = () => (LIST_API ? "Copy live link" : "Copy link to this list");
    document.getElementById("sp-copy")?.addEventListener("click", async () => {
      if (!state.favorites.size) return flash("sp-copy", "Star some bills first", copyDefault());
      if (LIST_API && await ensureLive()) {
        history.replaceState(null, "", "#id=" + live.id);
        scheduleLivePush(); // make sure the server has the current contents
        try {
          await navigator.clipboard.writeText(location.href);
          return flash("sp-copy", "✓ Live link copied — it updates as you edit", copyDefault());
        } catch {
          return flash("sp-copy", "Copy the URL from the address bar", copyDefault());
        }
      }
      await syncListHash();
      try {
        await navigator.clipboard.writeText(location.href);
        flash("sp-copy", "✓ Copied — bookmark it anywhere", copyDefault());
      } catch {
        flash("sp-copy", "Copy the URL from the address bar", copyDefault());
      }
    });
    const shareBtn = document.getElementById("sp-share");
    if (shareBtn && navigator.share) {
      shareBtn.hidden = false;
      shareBtn.addEventListener("click", async () => {
        await syncListHash();
        try { await navigator.share({ title: "My Hawaiʻi county bill list", url: location.href }); }
        catch { /* user cancelled */ }
      });
    }
    document.getElementById("sp-opml")?.addEventListener("click", () => {
      if (!state.favorites.size) return flash("sp-opml", "Star some bills first", "Follow in an RSS reader (OPML)");
      const blob = new Blob([opmlForFavorites()], { type: "text/x-opml" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "hawaii-bill-tracker-list.opml";
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    });
  }

  // Legend & glossary modal, opened from the overflow menu.
  function wireHelpDialog() {
    const dlg = document.getElementById("help-dialog");
    if (!dlg) return;
    const open = () => { if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", ""); };
    document.getElementById("help-btn")?.addEventListener("click", () => {
      document.getElementById("more-pop")?.setAttribute("hidden", "");
      document.getElementById("more-btn")?.setAttribute("aria-expanded", "false");
      open();
    });
    document.getElementById("help-close")?.addEventListener("click", () => dlg.close());
    // Click on the backdrop (outside the dialog box) closes it.
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  }

  // One-time onboarding hint; dismissed state persists per browser.
  function wireHint() {
    const hint = document.getElementById("hint");
    if (!hint) return;
    let dismissed = false;
    try { dismissed = !!localStorage.getItem("tracker:hint-dismissed"); } catch { /* storage off */ }
    if (!dismissed) hint.hidden = false;
    document.getElementById("hint-dismiss")?.addEventListener("click", () => {
      hint.hidden = true;
      try { localStorage.setItem("tracker:hint-dismissed", "1"); } catch { /* storage off */ }
    });
  }

  function setAll(set, items) {
    set.clear();
    (items || []).forEach((x) => set.add(x.value));
  }

  // Removable chips summarizing the applied (non-default) filters, with Reset all.
  function renderActiveFilters() {
    const host = document.getElementById("active-filters");
    if (!host) return;
    const it = state._items || {};
    const labelOf = (arr, v) => (arr?.find((x) => x.value === v)?.label) ?? v;
    const chips = [];
    const dimChip = (dim, set, items, name) => {
      if (!items || set.size === items.length) return; // "all" → no chip
      const text = set.size === 1 ? `${name}: ${labelOf(items, [...set][0])}`
        : set.size === 0 ? `${name}: none` : `${name}: ${set.size}`;
      chips.push({ dim, text });
    };
    // Year, Favorites, and Search each have a dedicated always-visible control
    // (segmented control, toggle button, search box), so they're not echoed as
    // removable chips. County/Type/Subject/Status live in column-header popovers
    // that aren't always on screen, so those get chips.
    dimChip("council", state.councils, it.council, "County");
    dimChip("type", state.types, it.type, "Type");
    dimChip("subject", state.subjects, it.subject, "Subject");
    dimChip("status", state.statuses, it.status, "Status");
    dimChip("class", state.matterClasses, it.matterClass, "Records");

    host.innerHTML = "";
    if (!chips.length) { host.hidden = true; return; }
    host.hidden = false;
    for (const c of chips) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "af-chip";
      b.dataset.dim = c.dim;
      b.setAttribute("aria-label", `Remove filter ${c.text}`);
      b.innerHTML = `${escapeHtml(c.text)}<svg class="af-x" aria-hidden="true"><use href="#i-x"/></svg>`;
      host.appendChild(b);
    }
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "af-reset";
    reset.textContent = "Reset all";
    host.appendChild(reset);
  }
  function clearFilterDim(dim) {
    const it = state._items || {};
    if (dim === "council") setAll(state.councils, it.council);
    else if (dim === "type") setAll(state.types, it.type);
    else if (dim === "subject") setAll(state.subjects, it.subject);
    else if (dim === "status") setAll(state.statuses, it.status);
    else if (dim === "year") setAll(state.years, it.year);
    else if (dim === "class") setAll(state.matterClasses, it.matterClass);
    else if (dim === "fav") {
      setFavoritesOnly(false);
      syncListHash();
    } else if (dim === "search") {
      state.search = "";
      const s = document.getElementById("f-search"); if (s) s.value = "";
    const sc = document.getElementById("f-search-clear"); if (sc) sc.hidden = true;
    }
    renderFilterGroups();
    applyFilters();
  }
  // Clicking a subject pill on a row narrows the Subject filter to just it.
  function filterToSubject(value) {
    const it = state._items || {};
    if (!it.subject?.some((x) => x.value === value)) return;
    state.subjects.clear();
    state.subjects.add(value);
    renderFilterGroups();
    animateNext();
    applyFilters();
  }
  // Wire the masthead numbers to the filters that actually produce them, so the
  // "225 active this year" stat and the table's result count visibly agree when
  // you click through (resolves the stat-vs-count confusion). Reuses
  // showEverything() to open every dimension, then narrows as needed.
  function wireStatStrip() {
    const it = state._items || {};
    const newest = it.year?.[0]?.value;
    const makeLink = (el, title, go) => {
      if (!el) return;
      el.classList.add("stat-link");
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      el.title = title;
      el.addEventListener("click", () => { animateNext(); go(); });
      el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); animateNext(); go(); }
      });
    };
    makeLink(document.getElementById("stat-active")?.closest(".stat"),
      newest ? `Show all active ${newest} bills` : "Show active bills", () => {
        showEverything();
        state.statuses.clear(); state.statuses.add("Active");
        if (newest) { state.years.clear(); state.years.add(newest); }
        renderFilterGroups();
        applyFilters();
      });
    makeLink(document.getElementById("stat-total")?.closest(".stat"),
      "Show every tracked bill", () => { showEverything(); applyFilters(); });
  }
  // Arrow-key navigation between rows once a row has focus.
  function wireKeyboardNav() {
    document.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const cur = document.activeElement;
      if (!cur || !cur.classList?.contains("bill-row")) return;
      e.preventDefault();
      const rows = [...document.querySelectorAll("#results tbody tr.bill-row")];
      const next = rows[rows.indexOf(cur) + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) next.focus();
    });
  }
  function resetAllFilters() {
    const it = state._items || {};
    setAll(state.councils, it.council);
    setAll(state.subjects, it.subject);
    setAll(state.types, it.type);
    setAll(state.statuses, it.status);
    setAll(state.years, it.year);
    state.matterClasses.clear(); state.matterClasses.add("legislation");
    state.search = "";
    const s = document.getElementById("f-search"); if (s) s.value = "";
    const sc = document.getElementById("f-search-clear"); if (sc) sc.hidden = true;
    setFavoritesOnly(false);
    state.sortExplicit = false;
    state.onlyStalled = false;
    const sf = document.getElementById("f-stalled"); if (sf) sf.checked = false;
    syncListHash();
    renderFilterGroups();
    applyFilters();
  }

  let filtersWired = false;

  function buildFilters(payload) {
    // Default to everything selected ("All" checked) on first load.
    // Years present in the data, newest first.
    const years = [...new Set(payload.bills.map(billYear).filter(Boolean))].sort().reverse();
    const typesPresent = TYPE_BUCKETS.filter((t) => payload.bills.some((b) => typeBucket(b.bill_type) === t));
    const statusesPresent = STATUS_BUCKETS.filter((s) => payload.bills.some((b) => statusBucket(b) === s));
    const classesPresent = (payload.matter_classes || ["legislation"])
      .filter((k) => payload.bills.some((b) => (b.matter_class || "legislation") === k));
    if (!filtersWired) {
      payload.councils.forEach((c) => state.councils.add(c));
      payload.subjects.forEach((s) => state.subjects.add(s));
      typesPresent.forEach((t) => state.types.add(t)); // default: all types (no filter)
      // This is a search tool over the councils' whole published record, so
      // every year and every status is in scope by default. Narrowing to the
      // current year / active bills (the old defaults) silently hid most of
      // the corpus from a query that should have found it.
      statusesPresent.forEach((s) => state.statuses.add(s));
      years.forEach((y) => state.years.add(y));
    }
    state.councilUniverse = payload.councils.length;
    state.subjectUniverse = payload.subjects.length;
    state.yearUniverse = years.length;
    state.typeUniverse = typesPresent.length;
    state.statusUniverse = statusesPresent.length;
    state.matterClassUniverse = classesPresent.length;

    state._items = {
      matterClass: classesPresent.map((k) => ({ value: k, label: MATTER_CLASS_LABEL[k] || k })),
      council: payload.councils.map((c) => ({ value: c, label: COUNCIL_LABEL[c] || c })),
      year: years.map((y) => ({ value: y, label: y })),
      subject: payload.subjects.map((s) => ({ value: s, label: SUBJECT_LABEL[s] || s })),
      type: typesPresent.map((t) => ({ value: t, label: t })),
      status: statusesPresent.map((s) => ({ value: s, label: s })),
    };
    renderFilterGroups();

    if (filtersWired) return;
    filtersWired = true;
    const searchInput = document.getElementById("f-search");
    const searchClear = document.getElementById("f-search-clear");
    searchInput.addEventListener("input", (e) => {
      state.search = e.target.value.toLowerCase().trim();
      if (searchClear) searchClear.hidden = !e.target.value;
      applyFilters();
    });
    if (searchClear) searchClear.addEventListener("click", () => {
      searchInput.value = "";
      state.search = "";
      searchClear.hidden = true;
      applyFilters();
      searchInput.focus();
    });
    wireSortMenu();
    document.getElementById("f-stalled")?.addEventListener("change", (e) => {
      state.onlyStalled = e.target.checked;
      animateNext();
      applyFilters();
    });
    document.getElementById("export-csv")?.addEventListener("click", () => {
      document.getElementById("more-pop")?.setAttribute("hidden", "");
      document.getElementById("more-btn")?.setAttribute("aria-expanded", "false");
      exportCsv();
    });
    const favBtn = document.getElementById("f-favorites");
    if (favBtn) favBtn.addEventListener("click", () => {
      setFavoritesOnly(!state.favoritesOnly);
      syncListHash();
      animateNext();
      applyFilters();
    });
    // "/" focuses search from anywhere; Esc inside it clears and blurs.
    document.addEventListener("keydown", (e) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = document.activeElement?.tagName || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;
      e.preventDefault();
      searchInput.focus();
    });
    searchInput.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      searchInput.value = "";
      state.search = "";
      if (searchClear) searchClear.hidden = true;
      applyFilters();
      searchInput.blur();
    });
    window.addEventListener("resize", () => {
      const c = document.getElementById("f-year");
      if (c) positionSegPill(c);
    });
    const copyBtn = document.getElementById("copy-list");
    if (copyBtn) {
      const copyLabel = copyBtn.querySelector(".mi-label");
      const setCopy = (t) => { if (copyLabel) copyLabel.textContent = t; };
      copyBtn.addEventListener("click", async () => {
        await syncListHash();
        const restore = () => setCopy("Copy link to my list");
        if (!state.favorites.size) {
          setCopy("Star some bills first");
          setTimeout(restore, 1800);
          return;
        }
        try {
          await navigator.clipboard.writeText(location.href);
          setCopy("✓ Link copied — bookmark it");
        } catch {
          setCopy("Copy the page URL from the address bar");
        }
        setTimeout(restore, 2200);
      });
    }
    wireToolbarMenu();
    wireThemeToggle();
    wireStatStrip();
    wireKeyboardNav();
    wireSavedPanel();
    wireHelpDialog();
    wireHint();
    // Delegated row action: clickable subject pills narrow the Subject filter.
    const tbody = document.querySelector("#results tbody");
    if (tbody) tbody.addEventListener("click", (e) => {
      const pill = e.target.closest(".subject-pill[data-subject]");
      if (pill) filterToSubject(pill.dataset.subject);
    });
    wireColumnFilter("cf-council-btn", "cf-council-pop");
    wireColumnFilter("cf-subject-btn", "cf-subject-pop");
    wireColumnFilter("cf-type-btn", "cf-type-pop");
    wireColumnFilter("cf-status-btn", "cf-status-pop");
    wireColumnFilter("cf-class-btn", "cf-class-pop");
    const af = document.getElementById("active-filters");
    if (af) af.addEventListener("click", (e) => {
      animateNext();
      if (e.target.closest(".af-reset")) return resetAllFilters();
      const chip = e.target.closest(".af-chip");
      if (chip) clearFilterDim(chip.dataset.dim);
    });
    document.addEventListener("click", () => closeColPop(false));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeColPop(true); });
    window.addEventListener("scroll", () => closeColPop(false), true);
    window.addEventListener("resize", () => closeColPop(false));
    updateFavCount();
  }

  function filterBills() {
    const countyAll = state.councils.size === state.councilUniverse;
    const subjectAll = state.subjects.size === state.subjectUniverse;
    const yearAll = state.years.size === state.yearUniverse;
    const typeAll = state.types.size === state.typeUniverse;
    const statusAll = state.statuses.size === state.statusUniverse;
    const classAll = state.matterClasses.size === state.matterClassUniverse;
    // Resolve the query once for the whole pass instead of per bill. The score
    // map is kept on state so sortBills() can rank by relevance.
    const hits = state.search ? searchScores(state.search) : null;
    state.searchHits = hits;
    return state.bills.filter((b) => {
      if (state.favoritesOnly && !state.favorites.has(favKey(b))) return false;
      // "All" checked → no constraint on that dimension; otherwise the bill
      // must match a checked box (empty selection → nothing).
      if (!countyAll && !state.councils.has(b.council)) return false;
      if (!yearAll && !state.years.has(billYear(b))) return false;
      if (!subjectAll && !b.subjects?.some((s) => state.subjects.has(s))) return false;
      if (!classAll && !state.matterClasses.has(b.matter_class || "legislation")) return false;
      if (!typeAll && !state.types.has(typeBucket(b.bill_type))) return false;
      if (!statusAll && !state.statuses.has(statusBucket(b))) return false;
      if (state.onlyStalled && !isStalled(b)) return false;
      if (hits && !hits.has(b._idx)) return false;
      return true;
    });
  }

  // Returns [mainRow, detailRow]. The detail row spans all columns so the
  // description gets the full table width instead of the narrow Title column.
  function renderRows(b) {
    const council = COUNCIL_LABEL[b.council] || b.council;
    const pills = (b.subjects || [])
      .map(
        (s) =>
          `<button type="button" class="subject-pill ${s}" data-subject="${escapeHtml(s)}" ` +
          `title="Show only ${SUBJECT_LABEL[s] || s} bills">${SUBJECT_LABEL[s] || s}</button>`
      )
      .join("");

    let lastAction = "";
    if (b.last_action) {
      lastAction = b.last_action + (b.last_action_date ? ` (${b.last_action_date})` : "");
    } else if (b.last_action_date) {
      lastAction = b.last_action_date;
    }
    const prog = billProgress(b);
    const fullTitle = (b.title || "").trim();
    const head = billHeadline(b);
    let sub = "";
    if (head && fullTitle && head !== fullTitle) sub = fullTitle;
    else if (b.raw_subject && b.raw_subject !== fullTitle) sub = b.raw_subject;

    const tr = document.createElement("tr");
    tr.className = "bill-row";
    tr.dataset.key = favKey(b);
    tr.tabIndex = 0;
    tr.setAttribute("role", "button");
    tr.setAttribute("aria-expanded", "false");
    tr.innerHTML = `
      <td class="col-fav">${favButtonHtml(b)}</td>
      <td class="col-council" data-label="County"><span class="county-tag county-${escapeHtml(b.council)}">${escapeHtml(council)}</span></td>
      <td class="col-num" data-label="Number"><a class="bill-link" href="${escapeHtml(b.url)}" target="_blank" rel="noopener">${highlightHtml(escapeHtml(b.bill_number), state.search)}</a></td>
      <td class="col-type" data-label="Type">${escapeHtml(b.bill_type)}</td>
      <td class="col-title" data-label="Title">
        <div class="title-line"><span class="caret" aria-hidden="true">▸</span><span class="title-text">${highlightHtml(annotate(head || fullTitle || ""), state.search)}</span>${rowBadgeHtml(b)}</div>
        ${sub ? `<div class="title-preview">${highlightHtml(annotate(sub), state.search)}</div>` : ""}
      </td>
      <td class="col-subj" data-label="Subjects">${pills || '<span class="muted">—</span>'}</td>
      <td class="col-status" data-label="Progress" title="${escapeHtml(b.last_action || b.status || "")}">
        ${renderStepper(prog, false)}
        <div class="stage-label">${prog.label}</div>
        ${b.last_action_date ? `<div class="status-date">${escapeHtml(b.last_action_date)}</div>` : ""}
      </td>
    `;

    const detail = document.createElement("tr");
    detail.className = "detail-row";
    detail.hidden = true;
    // Maui's raw_subject is the referring committee; everywhere else it is a
    // staff summary — or, when the council source has no separate summary
    // (Kauai), the full legal title, which is itself the best description.
    const summaryLabel =
      b.council === "maui" ? "Committee / body"
      : b.raw_subject && b.raw_subject !== fullTitle ? "Summary"
      : "Full title";
    const st = normalizeStatus(b);
    const parts = [];

    // Header band — the panel's own identity (county, type, current stage) so it
    // reads on its own instead of leaning on the collapsed row above it.
    parts.push(
      `<div class="dx-head">` +
        `<span class="county-tag county-${escapeHtml(b.council)}">${escapeHtml(council)}</span>` +
        `<span class="dx-type">${escapeHtml(b.bill_type)}</span>` +
        `<span class="dx-status ${st.cls}">${escapeHtml(st.label)}</span>` +
      `</div>`
    );

    // Progress is already shown in the row's Progress column, so the expanded
    // view skips the (redundant) labeled stepper and leads with the summary.
    // When the council gives no separate summary, fall back to the full legal
    // title (the row truncates it; here it shows in full) before giving up.
    if (b.raw_subject) {
      parts.push(
        `<div class="detail-summary"><span class="detail-label">${summaryLabel}</span>${annotate(b.raw_subject)}</div>`
      );
    } else if (fullTitle) {
      parts.push(
        `<div class="detail-summary"><span class="detail-label">Full title</span>${annotate(fullTitle)}</div>`
      );
    } else {
      parts.push(`<div class="detail-summary muted">No description available from the council source.</div>`);
    }

    // Meta chips — labelled facts with inline icons, each in its own card.
    const trackedSince = (b.first_seen || "").slice(0, 10);
    const chip = (icon, label, value) =>
      `<div class="dx-chip"><svg class="dx-chip-ic" aria-hidden="true"><use href="#${icon}"/></svg>` +
      `<div class="dx-chip-body"><span class="dx-chip-label">${label}</span>` +
      `<span class="dx-chip-val">${value}</span></div></div>`;
    const chips = [];
    if (b.introducer) chips.push(chip("i-user", "Introducer", escapeHtml(b.introducer)));
    if (b.introduced_date) chips.push(chip("i-calendar", "Introduced", escapeHtml(b.introduced_date)));
    if (trackedSince) chips.push(chip("i-clock", "Tracked since", escapeHtml(trackedSince)));
    if (chips.length) parts.push(`<div class="dx-meta">${chips.join("")}</div>`);

    // Subjects — re-shown here with a label so the panel documents how the bill
    // was classified (the row's pills carry no header).
    if (pills) {
      parts.push(
        `<div class="dx-subjects"><span class="detail-label">Subjects</span>` +
        `<div class="dx-subjects-pills">${pills}</div></div>`
      );
    }

    // Action history. The scraper currently captures only the latest action, so
    // render that as a one-item timeline; when an `actions[]` array lands (see
    // the planned schema change) this fills out into the full vertical history.
    // Timelines ship in per-council shards and attach lazily, so this is built
    // as a function: until the shard lands it renders the single latest action,
    // then gets rebuilt in place once the real history arrives (rebuilding via
    // a full re-render would collapse the row the reader just opened).
    const timelineInner = () => {
      const acts = Array.isArray(b.actions) && b.actions.length
        ? b.actions
        : (lastAction ? [{ action: b.last_action || lastAction, date: b.last_action_date || "" }] : []);
      if (!acts.length) return "";
      // Long histories (Honolulu bills run 14+ steps) dominate the panel's
      // height, so show only the most recent few and tuck the rest behind a
      // toggle. The item just above the fold drops its connector so the line
      // doesn't dangle into the hidden region.
      const TL_CAP = 4;
      const capped = acts.length > TL_CAP;
      const items = acts.map((a, i) => {
        const when = a.date ? `<span class="dx-tl-date">${escapeHtml(a.date)}</span>` : "";
        const text = escapeHtml(a.action || "");
        const cls = [
          "dx-tl-item",
          i === 0 ? "is-latest" : "",
          capped && i >= TL_CAP ? "dx-tl-extra" : "",
          capped && i === TL_CAP - 1 ? "dx-tl-foldedge" : "",
        ].filter(Boolean).join(" ");
        return `<li class="${cls}">` +
          `<span class="dx-tl-dot" aria-hidden="true"></span>` +
          `<div class="dx-tl-body">${when}<span class="dx-tl-text">${text}</span></div></li>`;
      }).join("");
      const toggle = capped
        ? `<button type="button" class="dx-tl-toggle" aria-expanded="false">` +
          `Show all ${acts.length} actions</button>`
        : "";
      return `<span class="detail-label">` +
        `${acts.length > 1 ? "Action history" : "Latest action"}</span>` +
        `<ul class="dx-tl">${items}</ul>${toggle}`;
    };
    const tlInner = timelineInner();
    if (tlInner) parts.push(`<div class="dx-timeline">${tlInner}</div>`);

    // Actions — primary link out to the council source plus quick utilities.
    const isFav = state.favorites.has(favKey(b));
    parts.push(
      `<div class="dx-actions">` +
        `<a class="dx-btn dx-btn-primary" href="${escapeHtml(b.url)}" target="_blank" rel="noopener">` +
          `<svg aria-hidden="true"><use href="#i-external"/></svg>View on council site</a>` +
        `<button type="button" class="dx-btn dx-copy">` +
          `<svg aria-hidden="true"><use href="#i-copy"/></svg>Copy reference</button>` +
        `<button type="button" class="dx-btn dx-link">` +
          `<svg aria-hidden="true"><use href="#i-link"/></svg>Copy link</button>` +
        `<button type="button" class="dx-btn dx-fav${isFav ? " is-fav" : ""}" aria-pressed="${isFav}">` +
          `<svg aria-hidden="true"><use href="#i-star${isFav ? "" : "-o"}"/></svg>` +
          `<span class="dx-fav-txt">${isFav ? "Saved" : "Save"}</span></button>` +
      `</div>`
    );

    // Gutter cells occupy the star+county columns so the expanded content lines
    // up under the bill's Number/Title block instead of floating at the far left.
    // .detail-anim is the grid 0fr→1fr wrapper that animates the open/close.
    detail.innerHTML = `<td class="detail-gutter" colspan="2"></td>` +
      `<td colspan="5"><div class="detail-anim"><div class="detail-inner">${parts.join("")}</div></div></td>`;

    // Copy a plain-text reference (council, number, title, source URL) — handy
    // for the analyst pasting a bill into notes or a message.
    const copyBtn = detail.querySelector(".dx-copy");
    copyBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const ref = `${council} ${b.bill_number} — ${(head || fullTitle || "").trim()}\n${b.url}`;
      try {
        await navigator.clipboard.writeText(ref);
        copyBtn.classList.add("is-done");
        copyBtn.querySelector("use").setAttribute("href", "#i-check");
        setTimeout(() => {
          copyBtn.classList.remove("is-done");
          copyBtn.querySelector("use").setAttribute("href", "#i-copy");
        }, 1400);
      } catch { /* clipboard blocked — no-op */ }
    });
    // Copy a shareable dashboard link that re-opens this bill expanded.
    const linkBtn = detail.querySelector(".dx-link");
    linkBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      try {
        await navigator.clipboard.writeText(billDeepLink(b));
        linkBtn.classList.add("is-done");
        linkBtn.querySelector("use").setAttribute("href", "#i-check");
        setTimeout(() => {
          linkBtn.classList.remove("is-done");
          linkBtn.querySelector("use").setAttribute("href", "#i-link");
        }, 1400);
      } catch { /* clipboard blocked — no-op */ }
    });
    // Expand/collapse the capped action timeline. Re-wired after a lazy refill,
    // since that replaces the button along with the rest of the markup.
    const wireTimelineToggle = () => {
      const tlToggle = detail.querySelector(".dx-tl-toggle");
      if (!tlToggle) return;
      const total = (b.actions || []).length;
      tlToggle.addEventListener("click", (e) => {
        e.stopPropagation();
        const tl = tlToggle.closest(".dx-timeline");
        const open = tl.classList.toggle("show-all");
        tlToggle.setAttribute("aria-expanded", String(open));
        tlToggle.textContent = open ? "Show less" : `Show all ${total} actions`;
      });
    };
    wireTimelineToggle();
    // Called once this bill's council shard has loaded.
    detail._refillTimeline = () => {
      const host = detail.querySelector(".dx-timeline");
      if (!host) return;
      host.innerHTML = timelineInner();
      wireTimelineToggle();
    };
    // The in-panel Save button mirrors the row's star (and vice-versa via re-render).
    const dxFav = detail.querySelector(".dx-fav");
    dxFav.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFav(b);
      const on = state.favorites.has(favKey(b));
      dxFav.classList.toggle("is-fav", on);
      dxFav.setAttribute("aria-pressed", String(on));
      dxFav.querySelector("use").setAttribute("href", on ? "#i-star" : "#i-star-o");
      dxFav.querySelector(".dx-fav-txt").textContent = on ? "Saved" : "Save";
      // Keep the row's star in sync.
      const star = tr.querySelector(".fav-btn");
      if (star) {
        star.classList.toggle("is-fav", on);
        star.setAttribute("aria-pressed", String(on));
        star.querySelector("use")?.setAttribute("href", on ? "#i-star" : "#i-star-o");
        star.title = on ? "Remove from favorites" : "Save to favorites";
      }
    });

    function toggle() {
      const open = !tr.classList.contains("open");
      tr.classList.toggle("open", open);
      tr.setAttribute("aria-expanded", String(open));
      // The full history may still be in flight (or never prefetched, on a slow
      // connection). Fill it in when it lands rather than making the row wait.
      if (open) ensureActions(b).then((got) => { if (got) detail._refillTimeline?.(); });
      if (REDUCED_MOTION) {
        detail.classList.toggle("expanded", open);
        detail.hidden = !open;
        return;
      }
      if (open) {
        detail.hidden = false;
        // two frames so the 0fr state paints before the transition to 1fr
        requestAnimationFrame(() => requestAnimationFrame(() => detail.classList.add("expanded")));
      } else {
        detail.classList.remove("expanded");
        const anim = detail.querySelector(".detail-anim");
        anim.addEventListener("transitionend", () => {
          if (!tr.classList.contains("open")) detail.hidden = true;
        }, { once: true });
      }
    }
    tr.addEventListener("click", (e) => {
      // Links, the star, and the clickable subject pills handle their own clicks.
      if (e.target.closest("a") || e.target.closest(".fav-btn") || e.target.closest(".subject-pill")) return;
      toggle();
    });
    tr.addEventListener("keydown", (e) => {
      if (e.target.closest(".fav-btn")) return; // star handles its own keys
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    });

    // Star toggles the favorite without expanding the row.
    const favBtn = tr.querySelector(".fav-btn");
    favBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleFav(b);
      const on = state.favorites.has(favKey(b));
      favBtn.classList.toggle("is-fav", on);
      favBtn.setAttribute("aria-pressed", String(on));
      favBtn.querySelector("use")?.setAttribute("href", on ? "#i-star" : "#i-star-o");
      favBtn.title = on ? "Remove from favorites" : "Save to favorites";
      // Keep the in-panel Save button in sync if the detail row is open.
      const dxFav = detail.querySelector(".dx-fav");
      if (dxFav) {
        dxFav.classList.toggle("is-fav", on);
        dxFav.setAttribute("aria-pressed", String(on));
        dxFav.querySelector("use")?.setAttribute("href", on ? "#i-star" : "#i-star-o");
        dxFav.querySelector(".dx-fav-txt").textContent = on ? "Saved" : "Save";
      }
      if (on && !REDUCED_MOTION) {
        favBtn.classList.add("pop");
        favBtn.addEventListener("animationend", () => favBtn.classList.remove("pop"), { once: true });
        const badge = document.getElementById("fav-count");
        if (badge) {
          badge.classList.remove("bump");
          void badge.offsetWidth; // restart the animation
          badge.classList.add("bump");
        }
      }
      if (state.favoritesOnly) applyFilters(); // drop it from the filtered view
    });

    return [tr, detail];
  }

  let firstRender = true;
  function applyFilters() {
    const filtered = sortBills(filterBills());
    updateColumnFilterIndicators();
    renderActiveFilters();
    syncSortLabel();
    const render = () => {
      const tbody = document.querySelector("#results tbody");
      tbody.innerHTML = "";
      if (!filtered.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7" class="empty-state">` +
          `<svg class="es-icon" aria-hidden="true"><use href="#i-search"/></svg>` +
          `<span class="es-title">No bills match your filters</span>` +
          `<span class="es-sub">Try widening the year, county, or status — or ` +
          `<button type="button" id="empty-reset" class="link-btn">reset all filters</button>.</span></td>`;
        tbody.appendChild(tr);
        tbody.querySelector("#empty-reset").addEventListener("click", () => { animateNext(); resetAllFilters(); });
      } else {
        const frag = document.createDocumentFragment();
        let i = 0;
        for (const b of filtered.slice(0, 1000)) {
          const [row, detail] = renderRows(b);
          // Entrance stagger on the initial load only, capped to the first
          // screenful — later re-renders crossfade via the view transition.
          if (firstRender && !REDUCED_MOTION && i < 12) {
            row.classList.add("enter");
            row.style.animationDelay = `${i * 25}ms`;
          }
          frag.appendChild(row);
          frag.appendChild(detail);
          i++;
        }
        tbody.appendChild(frag);
        observeSteppers(tbody);
      }
      document.getElementById("result-count").textContent =
        `${filtered.length} bill${filtered.length === 1 ? "" : "s"}` +
        (filtered.length > 1000 ? " (showing first 1000)" : "");
      firstRender = false;
    };
    const useVT = animateNextApply && !REDUCED_MOTION && typeof document.startViewTransition === "function";
    animateNextApply = false;
    if (useVT) document.startViewTransition(render);
    else render();
    syncFilterUrl(); // keep the shareable query string in step with the filters
  }

  // ---- Export & deep-linking ------------------------------------------------

  function csvCell(v) {
    const s = String(v == null ? "" : v);
    return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }

  // Download the currently filtered + sorted view as CSV (what you see, in the
  // order you see it). BOM prefix so Excel reads UTF-8 names correctly.
  function exportCsv() {
    const rows = sortBills(filterBills());
    const cols = [
      ["County", (b) => COUNCIL_LABEL[b.council] || b.council],
      ["Number", (b) => b.bill_number],
      ["Type", (b) => b.bill_type],
      ["Record kind", (b) => MATTER_CLASS_LABEL[b.matter_class] || b.matter_class || ""],
      ["Title", (b) => b.title || billHeadline(b) || ""],
      ["Introducer", (b) => b.introducer],
      ["Introduced", (b) => b.introduced_date],
      ["Status", (b) => normalizeStatus(b).label],
      ["Last action", (b) => b.last_action],
      ["Last action date", (b) => b.last_action_date],
      ["Subjects", (b) => (b.subjects || []).map((s) => SUBJECT_LABEL[s] || s).join("; ")],
      ["URL", (b) => b.url],
    ];
    const lines = [cols.map((c) => csvCell(c[0])).join(",")];
    for (const b of rows) lines.push(cols.map((c) => csvCell(c[1](b))).join(","));
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `hawaii-county-bills-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // A shareable URL that re-opens the dashboard with one bill expanded.
  function billDeepLink(b) {
    return location.origin + location.pathname +
      "#bill=" + encodeURIComponent(b.council) + ":" + encodeURIComponent(b.bill_number);
  }

  // Clear every filter so a deep-linked bill can't be hidden from view.
  function showEverything() {
    const it = state._items || {};
    setAll(state.councils, it.council);
    setAll(state.subjects, it.subject);
    setAll(state.types, it.type);
    setAll(state.statuses, it.status);
    setAll(state.years, it.year);
    setAll(state.matterClasses, it.matterClass);
    state.onlyStalled = false;
    state.search = "";
    setFavoritesOnly(false);
    const sf = document.getElementById("f-stalled"); if (sf) sf.checked = false;
    const s = document.getElementById("f-search"); if (s) s.value = "";
    renderFilterGroups();
  }

  // On load, honor a #bill=<council>:<number> link: reveal, expand and scroll to it.
  function openBillFromHash() {
    const m = /[#&]bill=([^:&]+):([^&]+)/.exec(location.hash);
    if (!m) return;
    const council = decodeURIComponent(m[1]);
    const number = decodeURIComponent(m[2]);
    const bill = state.bills.find((b) => b.council === council && b.bill_number === number);
    if (!bill) return;
    showEverything();
    applyFilters();
    requestAnimationFrame(() => {
      const key = favKey(bill);
      for (const tr of document.querySelectorAll("tr.bill-row")) {
        if (tr.dataset.key !== key) continue;
        if (!tr.classList.contains("open")) tr.click();
        tr.scrollIntoView({ behavior: "smooth", block: "center" });
        tr.classList.add("deep-target");
        setTimeout(() => tr.classList.remove("deep-target"), 2200);
        break;
      }
    });
  }

  // ---- Action timelines (lazy) ----------------------------------------------
  // Timelines live in per-council shards rather than inside bills.json: they're
  // only needed once a row is expanded, and inlining them was a large share of
  // the payload that has to arrive before the table can render at all.
  const actionShards = new Map();
  function loadActionShard(council) {
    if (!actionShards.has(council)) {
      actionShards.set(
        council,
        fetch(`actions/${council}.json`)
          .then((r) => (r.ok ? r.json() : {}))
          // Offline or missing shard: rows keep the latest-action fallback.
          .catch(() => ({}))
      );
    }
    return actionShards.get(council);
  }

  // Attach this bill's history if it isn't already loaded. Resolves true when
  // something new arrived, so the caller knows a redraw is worthwhile.
  async function ensureActions(b) {
    if (Array.isArray(b.actions)) return false;
    if (!b.action_count) { b.actions = []; return false; }
    const shard = await loadActionShard(b.council);
    b.actions = shard[String(b.id)] || [];
    return b.actions.length > 0;
  }

  // Fold action text into the index once the shards land, so queries like
  // "first reading" or a name appearing only in a vote tally still match. The
  // histories aren't in bills.json any more, so without this they'd drop out of
  // search entirely — the weight is low, well under any title match.
  const ACTION_TOKEN_WEIGHT = 1;
  function indexActionText(bills) {
    const { postings } = searchIndex;
    let added = false;
    for (const b of bills) {
      if (!Array.isArray(b.actions) || !b.actions.length || b._actionsIndexed) continue;
      b._actionsIndexed = true;
      const text = b.actions.map((a) => `${a.action || ""} ${a.committee || ""}`).join(" ");
      searchIndex.blobs[b._idx] += " " + fold(text);
      for (const tok of new Set(tokenize(text))) {
        let p = postings.get(tok);
        if (!p) postings.set(tok, (p = new Map()));
        p.set(b._idx, (p.get(b._idx) || 0) + ACTION_TOKEN_WEIGHT);
        added = true;
      }
    }
    // prefixMatches() binary-searches this, so it has to stay sorted.
    if (added) searchIndex.tokens = [...postings.keys()].sort();
    return added;
  }

  // Warm every shard once the table is on screen so expanding a row is instant.
  // Deliberately after first paint — this must never block it.
  function prefetchActions() {
    const councils = [...new Set(state.bills.map((b) => b.council))];
    const run = async () => {
      await Promise.all(councils.map(async (c) => {
        const shard = await loadActionShard(c);
        for (const b of state.bills) {
          if (b.council === c && !Array.isArray(b.actions)) {
            b.actions = shard[String(b.id)] || [];
          }
        }
      }));
      const grew = indexActionText(state.bills);
      // Refill only rows the reader already has open; the rest pick the history
      // up whenever they next render.
      for (const d of document.querySelectorAll("#results tbody tr.detail-row:not([hidden])")) {
        d._refillTimeline?.();
      }
      // A query typed before the shards arrived was answered without action
      // text; re-run it now that there's more to match.
      if (grew && state.search) applyFilters();
    };
    if ("requestIdleCallback" in window) requestIdleCallback(run, { timeout: 3000 });
    else setTimeout(run, 500);
  }

  async function ingest(payload) {
    state.bills = payload.bills;
    state.matterClassUniverse = (payload.matter_classes || ["legislation"]).length;
    // One inverted index over the whole corpus, built once per ingest rather
    // than a haystack string rebuilt per keystroke.
    buildSearchIndex(state.bills);
    setMeta(payload);
    setStats(payload);
    buildFilters(payload);
    await restoreListFromHash();
    applyFilterParams();           // restore any filters carried in the URL query
    setFavoritesOnly(state.favoritesOnly);
    updateFavCount();
    applyFilters();
    openBillFromHash();
    prefetchActions();
  }

  // Placeholder shimmer rows shown while bills.json downloads (1.7MB — visible
  // on a slow phone connection). Replaced by the real rows on first ingest.
  function renderSkeleton(n) {
    const tbody = document.querySelector("#results tbody");
    if (!tbody) return;
    tbody.innerHTML = Array.from({ length: n || 8 }).map(() =>
      `<tr class="skeleton-row"><td colspan="7"><div class="sk sk-title"></div><div class="sk sk-line"></div></td></tr>`
    ).join("");
  }

  async function load() {
    try {
      const r = await fetch("bills.json?t=" + Date.now(), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      await ingest(await r.json());
    } catch (e) {
      if (!state.bills.length) {
        document.querySelector("#results tbody").innerHTML = "";
        document.getElementById("result-count").textContent =
          "Failed to load bills.json: " + e.message;
      }
    }
  }

  buildGlossaryPanel();
  renderSkeleton();
  load();

  // Scrape runs Mon/Wed/Fri, so a daily client poll is plenty; also re-fetch
  // whenever the tab regains focus — preserving active filters.
  setInterval(load, 24 * 60 * 60 * 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
  // Pasting a #bill=… link while already on the page (no reload) should still
  // open it.
  window.addEventListener("hashchange", () => {
    if (/[#&]bill=/.test(location.hash) && state.bills.length) openBillFromHash();
  });
})();

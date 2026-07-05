/**
 * api.js  –  API-Layer für die Todo-App
 * Lädt user_tag + role IMMER über /auth/me nach dem Login –
 * nie aus dem Login-Response allein (der kann user_tag:null haben).
 */

const API = {
  // Basis-Pfad: App läuft unter /todo/ (nginx leitet /todo/* → Backend)
  _BASE: '/todo',

  // ── Session-Keys ──────────────────────────────────────────────────────────
  // sessionStorage ist tab-isoliert → verhindert Cross-Tab-Verschmutzung wenn
  // mehrere User gleichzeitig im selben Browser eingeloggt sind.
  // Fallback auf localStorage beim ersten Laden (bestehende Sessions bleiben gültig).
  _K: { token: 'jwt_token', tag: 'user_tag', role: 'user_role' },

  _get(key) {
    return sessionStorage.getItem(key) || localStorage.getItem(key) || null;
  },
  _set(key, val) {
    sessionStorage.setItem(key, val);
    // localStorage NICHT beschreiben: Tab-Isolation soll greifen
  },
  _del(key) {
    sessionStorage.removeItem(key);
    localStorage.removeItem(key);
  },

  getToken()    { return this._get(this._K.token); },
  getUserTag()  { return this._get(this._K.tag);   },
  getUserRole() { return this._get(this._K.role);  },

  /** Compat: index.html liest noch 'username' */
  getUsername() { return this.getUserTag(); },

  setSession(token, userTag, role) {
    if (token) this._set(this._K.token, token);

    // Guard: niemals undefined / null / "undefined" / "null" speichern
    const tag = (userTag && userTag !== 'undefined' && userTag !== 'null')
                ? String(userTag) : null;
    if (tag) {
      this._set(this._K.tag, tag);
      sessionStorage.setItem('username', tag);   // Compat für index.html
    }

    const r = (role && role !== 'undefined') ? String(role) : null;
    if (r) this._set(this._K.role, r);
  },

  clearSession() {
    Object.values(this._K).forEach(k => this._del(k));
    sessionStorage.removeItem('username');
    localStorage.removeItem('username');
  },

  isLoggedIn() { return !!this.getToken(); },

  requireAuth() {
    if (!this.isLoggedIn()) { window.location.href = this._BASE + '/login.html'; return false; }
    return true;
  },

  logout() {
    this.clearSession();
    window.location.href = this._BASE + '/login.html';
  },

  // ── HTTP-Kern ─────────────────────────────────────────────────────────────
  async request(method, path, body) {
    const token = this.getToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;

    let res;
    try {
      res = await fetch(this._BASE + '/api' + path, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (_) {
      throw new Error('Netzwerkfehler – Server erreichbar?');
    }

    if (res.status === 401) { this.logout(); return null; }

    if (!res.ok) {
      let detail = 'HTTP ' + res.status;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }

    return res.json();
  },

  get(path)        { return this.request('GET',    path); },
  put(path, body)  { return this.request('PUT',    path, body); },
  post(path, body) { return this.request('POST',   path, body); },
  del(path, body)  { return this.request('DELETE', path, body); },

  // ── Auth ──────────────────────────────────────────────────────────────────
  async login(username, password) {
    const res = await fetch(this._BASE + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Login fehlgeschlagen');
    }
    const data = await res.json();

    // ── Schritt 1: Token sofort speichern damit /auth/me funktioniert ────────
    localStorage.setItem(this._K.token, data.access_token);

    // ── Schritt 2: /auth/me – einzige verlässliche Quelle für user_tag + role
    //    (Login-Response kann user_tag:null liefern wenn DB-Migration noch läuft)
    try {
      const me = await this.get('/auth/me');
      if (me?.user_tag) {
        this.setSession(data.access_token, me.user_tag, me.role);
        return data;
      }
    } catch (_) { /* Fallback unten */ }

    // ── Fallback: Login-Response direkt nutzen ───────────────────────────────
    this.setSession(data.access_token, data.user_tag || null, data.role || null);
    return data;
  },

  async register(username, password, token = '') {
    const url = this._BASE + '/api/auth/register' + (token ? `?token=${encodeURIComponent(token)}` : '');
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Registrierung fehlgeschlagen');
    }
    return res.json();
  },

  async checkInvite(token) {
    const res = await fetch(`${this._BASE}/api/auth/check-invite?token=${encodeURIComponent(token)}`);
    if (!res.ok) return false;
    const data = await res.json().catch(() => ({}));
    return data.valid === true;
  },

  // ── Daten ─────────────────────────────────────────────────────────────────
  loadData()                          { return this.get('/data'); },
  saveData(todos, archiv, berichte, n){ return this.put('/data', { todos, archiv, berichte, kundenNotizen: n }); },

  // ── Benutzer ──────────────────────────────────────────────────────────────
  getUsers() { return this.get('/users'); },

  // ── Posteingang ───────────────────────────────────────────────────────────
  getInbox()               { return this.get('/inbox'); },
  getInboxCount()          { return this.get('/inbox/count'); },
  acceptInbox(id, comment) { return this.post(`/inbox/${id}/accept`, { comment }); },
  rejectInbox(id, comment) { return this.post(`/inbox/${id}/reject`, { comment }); },
  assignTodo(todoId, toUserTag, comment) {
    return this.post(`/assign/${todoId}`, { to_user_tag: toUserTag, comment });
  },
  retractAssign(todoId) {
    return this.post(`/assign/${todoId}/retract`, {});
  },
};

/**
 * extra.js  –  Erweiterungen für index.html:
 *   • Username#1234 im Header
 *   • Delegiert-Badge auf Todos ("📤 bei max#0042")
 *   • Freigegebene Bereiche als extra Tabs
 *   • Freigabe-Verwaltung (Modal)
 *   • api.js: to_user_tag statt to_username (Anpassung für neues Backend)
 */

// ══════════════════════════════════════════════════════════════════════════════
// State (ergänzt die globalen Arrays aus index.html)
// ══════════════════════════════════════════════════════════════════════════════
let _delegations   = [];  // [{todo_id, to_user_tag}]
let _sharesWithMe   = [];       // [{owner_id, owner_tag, permission}]
let _sharesOutgoing = [];       // [{id, viewer_tag, permission}]
let _sharedData     = {};       // owner_id → {todos, archiv, ...}
let _activeSharedOwners = new Set();  // welche Owner-Sektionen aktuell eingeblendet sind

// ══════════════════════════════════════════════════════════════════════════════
// _loadData überschreiben – lädt Delegierungen + Freigaben mit
// ══════════════════════════════════════════════════════════════════════════════
const _origLoadData = window._loadData || function(){};

// Background-Polling State
let _bgPollInterval   = null;
let _prevDelegHash      = '';
let _prevSharePendingN  = -1;  // Anzahl offener Freigabe-Anfragen
let _prevTodosLen     = 0;
let _prevInboxCount   = -1;
let _prevKommHash     = '';   // Fingerprint für isSharedEdit-Kommentar-Änderungen

// Letzter Checkin-Schlüssel eines Todos (val + Datum), für Hash-Vergleich
function _lastCheckinKey(todo) {
  const log = (todo && (todo.checkinLog || todo.checkinHistory)) || [];
  const last = log[log.length - 1];
  return last ? (last.val + '|' + last.date) : '';
}

function _delegHash() {
  // Hash deckt ab: Delegierungs-Status + letzter Checkin jedes delegierten Todos
  const delegSet = new Set(_delegations.map(d => d.todo_id));
  return [
    ..._delegations.map(d => d.todo_id + ':' + (d.status || '')),
    ...todos
      .filter(t => delegSet.has(String(t.id)))
      .map(t => 'ci:' + String(t.id) + ':' + _lastCheckinKey(t)),
  ].sort().join('|');
}

function _updateInboxBadge(count) {
  const el = document.getElementById('count-inbox');
  if (!el || count < 0) return;
  el.textContent = count;
  el.classList.toggle('badge-alert', count > 0);
}

function _applyData(data) {
  todos         = data.todos         || [];
  archiv        = data.archiv        || [];
  berichte      = data.berichte      || [];
  kundenNotizen = data.kundenNotizen || {};
  _sharesWithMe = data.sharesWithMe  || [];

  // Delegierungen: todo_id als String normalisieren + doppelte todo_ids entfernen
  const seen = new Set();
  _delegations = (data.delegations || []).filter(d => {
    const key = String(d.todo_id);
    if (seen.has(key)) return false;
    seen.add(key);
    d.todo_id = key;   // normalisiert auf String
    return true;
  });
}

async function _bgPollData() {
  console.log('[poll]', new Date().toLocaleTimeString(), '– starte Hintergrund-Update');
  const ae = document.activeElement;
  const userTyping = ae && (ae.contentEditable === 'true' || ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA');

  try {
    // /api/data, /api/inbox/count und /api/shares/pending parallel abrufen
    const [data, countRes, pendingSharesRes] = await Promise.all([
      API.loadData(),
      API.getInboxCount().catch(() => null),
      API.get('/shares/pending').catch(() => null),
    ]);
    if (!data) return;

    // ── Inbox-Badge: Todo-Zuweisungen + Freigabe-Anfragen ────────────────────
    const newInboxCount   = (countRes && countRes.count != null) ? countRes.count : -1;
    const newSharePending = Array.isArray(pendingSharesRes) ? pendingSharesRes.length : -1;

    const badgeTotal = (newInboxCount >= 0 ? newInboxCount : _prevInboxCount < 0 ? 0 : _prevInboxCount)
                     + (newSharePending >= 0 ? newSharePending : _prevSharePendingN < 0 ? 0 : _prevSharePendingN);

    if (newInboxCount >= 0 && newInboxCount !== _prevInboxCount) {
      _prevInboxCount = newInboxCount;
      // Neue Zuweisung eingegangen → aktuelles inbox-Array laden, dann rendern
      try {
        const freshInbox = await API.getInbox();
        if (Array.isArray(freshInbox)) inbox = freshInbox;
      } catch (_) {}
      if (typeof renderInbox === 'function') renderInbox();
    }

    if (newSharePending >= 0 && newSharePending !== _prevSharePendingN) {
      _prevSharePendingN = newSharePending;
      // Posteingang-Inhalt live aktualisieren wenn er gerade angezeigt wird
      if (Array.isArray(pendingSharesRes)) {
        pendingShares = pendingSharesRes;
        if (document.getElementById('section-inbox')?.classList.contains('visible')) {
          if (typeof renderInbox === 'function') renderInbox();
        }
      }
    }
    _updateInboxBadge(badgeTotal);

    // Pool-Tab immer aktualisieren, auch wenn eigene Todos unverändert
    if (typeof _refreshPoolTab === 'function') await _refreshPoolTab();

    // ── Hashes berechnen: Delegierungen + Checkins + Kommentare ─────────────────
    const delegSetNew = new Set((data.delegations || []).map(d => String(d.todo_id)));
    const newDelegHash = [
      ...(data.delegations || []).map(d => d.todo_id + ':' + (d.status || '')),
      ...(data.todos || [])
        .filter(t => delegSetNew.has(String(t.id)))
        .map(t => 'ci:' + String(t.id) + ':' + _lastCheckinKey(t)),
    ].sort().join('|');
    // Fingerprint: todos mit isSharedEdit-Kommentaren → erkennt Beschreibungsänderungen
    const newKommHash = (data.todos || [])
      .filter(t => (t.kommentare || []).some(k => k.isSharedEdit))
      .map(t => String(t.id) + ':' + (t.kommentare || []).filter(k => k.isSharedEdit).length)
      .sort().join('|');
    const newTodosLen = (data.todos || []).length;

    const nothingChanged = newDelegHash === _prevDelegHash
      && newTodosLen === _prevTodosLen
      && newKommHash === _prevKommHash;
    if (nothingChanged) return;

    if (newTodosLen !== _prevTodosLen) {
      // Todo-Anzahl geändert → vollständiger Reload nötig (nur wenn User nicht tippt)
      if (userTyping) return;
      _applyData(data);
      _prevDelegHash = _delegHash();
      _prevTodosLen  = newTodosLen;
      _prevKommHash  = newKommHash;
      try { collapsedKunden = JSON.parse(localStorage.getItem('collapsedKunden') || '{}'); } catch(_){}
      _rebuildSharedTabs();
      render();
      if (typeof _refreshPoolTab === 'function') await _refreshPoolTab();
      return;
    }

    // ── Gleiche Anzahl, aber Inhalt geändert → chirurgisches DOM-Update ──────────
    // Alten Zustand vor applyData sichern
    const oldStatusMap  = {};
    const oldCheckinMap = {};
    const oldKommMap    = {};  // todo_id → Anzahl isSharedEdit-Kommentare
    _delegations.forEach(d => {
      oldStatusMap[d.todo_id]  = d.status;
      const t = todos.find(x => String(x.id) === d.todo_id);
      oldCheckinMap[d.todo_id] = _lastCheckinKey(t);
    });
    todos.forEach(t => {
      oldKommMap[String(t.id)] = (t.kommentare || []).filter(k => k.isSharedEdit).length;
    });

    _applyData(data);
    _prevDelegHash = _delegHash();
    _prevKommHash  = newKommHash;

    // Welche todo_ids haben sich geändert?
    const changedIds = new Set();
    // 1. Delegierungs-Status geändert
    _delegations.forEach(d => {
      if (oldStatusMap[d.todo_id] !== d.status) changedIds.add(d.todo_id);
    });
    // 2. Retracted (waren in _delegations, jetzt weg)
    Object.keys(oldStatusMap).forEach(id => {
      if (!_delegations.find(d => d.todo_id === id)) changedIds.add(id);
    });
    // 3. Checkin-Status geändert
    _delegations.forEach(d => {
      const t = todos.find(x => String(x.id) === d.todo_id);
      if (oldCheckinMap[d.todo_id] !== _lastCheckinKey(t)) changedIds.add(d.todo_id);
    });
    // 4. isSharedEdit-Kommentare geändert (auch bei eigenen Todos, nicht nur delegierten)
    todos.forEach(t => {
      const newCount = (t.kommentare || []).filter(k => k.isSharedEdit).length;
      if (oldKommMap[String(t.id)] !== newCount) changedIds.add(String(t.id));
    });

    // Betroffene DOM-Elemente chirurgisch ersetzen
    for (const tid of changedIds) {
      const todo = todos.find(t => String(t.id) === tid);
      const el   = document.getElementById('item-' + tid);
      if (!todo || !el) continue;
      el.outerHTML = todoHTML(todo);
    }

    // Stats-Leiste aktualisieren (Delegiert-Zähler etc.)
    if (typeof renderStats === 'function') renderStats();

    // ── Aktive Shared-Sections auf Änderungen prüfen ──────────────────────────
    if (_activeSharedOwners.size > 0) {
      const _shrHash = d => (d.todos || []).map(t =>
        t.id + ':' + (t.desc || '') + ':' + (t.prio || '') + ':' + (t.kommentare || []).length
      ).join('|');
      await Promise.all([..._activeSharedOwners].map(async oid => {
        try {
          const sdata = await API.get(`/shared/${oid}/data`);
          const old   = _sharedData[oid];
          if (!old || _shrHash(sdata) !== _shrHash(old)) {
            _sharedData[oid] = sdata;
            const share = _sharesWithMe.find(s => s.owner_id === oid);
            _renderSharedSection(oid, sdata.todos || [], share ? share.permission : 'read');
          }
        } catch (_) {}
      }));
    }

    if (typeof _refreshPoolTab === 'function') await _refreshPoolTab();

  } catch (e) { console.error('[poll] Fehler:', e); }
}

window._loadData = async function () {
  const data = await API.loadData();
  if (!data) return;
  _applyData(data);

  // Einmalige Bereinigung: Beschreibungen die durch alten Badge-Bug korrumpiert wurden
  let _dirtyDesc = false;
  todos.forEach(t => {
    if (t.desc && t.desc.includes('contenteditable')) {
      const m = t.desc.match(/["']\s*>\s*([\s\S]+)$/);
      t.desc = m ? m[1].trim() : '';
      _dirtyDesc = true;
    }
  });
  if (_dirtyDesc) save();

  try { collapsedKunden = JSON.parse(localStorage.getItem('collapsedKunden') || '{}'); }
  catch (_) { collapsedKunden = {}; }

  _rebuildSharedTabs();

  // Background-Poller starten (einmalig)
  _prevDelegHash = _delegHash();
  _prevTodosLen  = todos.length;
  _prevKommHash  = todos
    .filter(t => (t.kommentare || []).some(k => k.isSharedEdit))
    .map(t => String(t.id) + ':' + (t.kommentare || []).filter(k => k.isSharedEdit).length)
    .sort().join('|');
  if (!_bgPollInterval) {
    _bgPollInterval = setInterval(_bgPollData, 30000);
  }
};

// ══════════════════════════════════════════════════════════════════════════════
// todoHTML überschreiben – fügt Delegiert-Badge hinzu
// ══════════════════════════════════════════════════════════════════════════════
const _origTodoHTML = window.todoHTML;

window.todoHTML = function (todo) {
  let html = _origTodoHTML(todo);

  // ── An mich delegiert (empfänger-Seite) ────────────────────────────────────
  if (todo._delegated_from_tag) {
    const badge = `<span class="tag-delegiert tag-delegiert--from">📥 von ${escHtml(todo._delegated_from_tag)}</span>`;
    html = html.replace('<div class="todo-desc-edit"', badge + '<div class="todo-desc-edit"');
    return html;
  }

  // ── Von mir delegiert (sender-Seite) ───────────────────────────────────────
  const deleg = _delegations.find(d => d.todo_id === String(todo.id));
  if (deleg) {
    // ── Zurückgegeben: Geschichte anzeigen, Todo ist wieder normal editierbar ──
    if (deleg.status === 'returned') {
      const chain = deleg.chain && deleg.chain.length ? deleg.chain : [];
      const chainStr = chain.map(t => `<strong>${escHtml(t)}</strong>`).join(' → ');
      const badge = `<div class="tag-delegiert" style="background:rgba(100,100,100,.07);border-color:#bbb;color:#888">📤 War gesendet: ${chainStr} → zurück</div>`;
      html = html.replace('<div class="todo-desc-edit"', badge + '<div class="todo-desc-edit"');
      return html;
    }

    // ── Aktiv delegiert: gesperrt ─────────────────────────────────────────────
    const chain      = deleg.chain && deleg.chain.length ? deleg.chain : [deleg.to_user_tag];
    const chainStr   = chain.map(t => `<strong>${escHtml(t)}</strong>`).join(' → ');
    const isPending  = deleg.status === 'pending';
    const lastStatus = chain.length > 1 ? '⏳ weiter gesendet' : (isPending ? '⏳ wartet auf Bestätigung' : '✅ angenommen');
    const retractBtn = isPending
      ? `<button class="btn btn-sm tag-retract-btn"
             onclick="retractAssign('${escAttr(todo.id)}',event)" title="Zuweisung zurückziehen">✕ Zurückziehen</button>`
      : '';
    const badge = `<div class="tag-delegiert">🔒 Gesendet an ${chainStr} · ${lastStatus}${retractBtn}</div>`;
    html = html.replace('<div class="todo-desc-edit"', badge + '<div class="todo-desc-edit"');

    // Gesperrt: nicht editierbar, kein Drag
    html = html.replace('contenteditable="true"', 'contenteditable="false"');
    html = html.replace('draggable="true"', 'draggable="false"');

    // Drag-Handle verstecken
    html = html.replace('<span class="drag-handle">⠇</span>', '<span class="drag-handle" style="visibility:hidden"></span>');

    // Prio-Dot: nicht klickbar
    html = html.replace(/onclick="cyclePrio\('[^']+',event\)"/, 'style="pointer-events:none;opacity:.4"');

    // Checkbox: Schloss-Icon, kein Click
    html = html.replace(
      /(<div class="checkbox") onclick="checkTodo\('[^']+'\)">/,
      '$1 style="pointer-events:none;opacity:.4;font-size:14px">🔒'
    );

    // Alle Edit-Buttons entfernen (todo-edit-actions komplett raus)
    html = html.replace(/<div class="todo-edit-actions">[\s\S]*?<\/div>/, '');

    // Ganzes Item als delegiert markieren
    html = html.replace('class="todo-item ', 'class="todo-item todo-delegated ');
    return html;
  }

  // ── Abgelehnt (rotes Badge, Todo wieder editierbar) ───────────────────────
  if (todo._rejected_by_tag) {
    const comment = todo._rejection_comment
      ? `: „${escHtml(todo._rejection_comment)}"`
      : '';
    const badge = `<span class="tag-rejected">❌ Abgelehnt von ${escHtml(todo._rejected_by_tag)}${comment}</span>`;
    html = html.replace('<div class="todo-desc-edit"', badge + '<div class="todo-desc-edit"');
    html = html.replace('class="todo-item ', 'class="todo-item todo-rejected ');
    return html;
  }

  return html;
};

// ══════════════════════════════════════════════════════════════════════════════
// renderStats überschreiben – 📤 Gesendet Counter + korrekter Unkontrolliert
// ══════════════════════════════════════════════════════════════════════════════
window.renderStats = function () {
  const delegatedIds = new Set(_delegations.map(d => d.todo_id));
  const delegCount   = delegatedIds.size;

  // "Meine" Todos: eigene (ohne delegierte-to-me), für Stat-Berechnungen
  const myTodos = todos.filter(t => !t._delegated_from_tag);

  const hoch      = myTodos.filter(t => t.prio === 'hoch').length;
  const zeitOffen = archiv.filter(t => t.zeitBuchen).length;
  // Unkontrolliert: delegierte (pending/accepted) rausnehmen
  const unk = myTodos.filter(t =>
    !delegatedIds.has(t.id) && todayStatus(t) === null && !isBlocked(t)
  ).length;
  const esk = myTodos.filter(t => isEskaliert(t)).length;
  const isA = (type, val) => activeFilter && activeFilter.type === type && activeFilter.value === val;

  document.getElementById('stats-bar').innerHTML =
    `<div class="stat" onclick="clearFilter()"><div class="stat-val">${myTodos.length}</div><div class="stat-label">Alle offen</div></div>` +
    (delegCount
      ? `<div class="stat" style="cursor:default"><div class="stat-val" style="color:#2A5298">${delegCount}</div><div class="stat-label">📤 Gesendet</div></div>`
      : '') +
    (esk
      ? `<div class="stat ${isA('eskaliert','ja')?'active-filter':''}" onclick="setFilter('eskaliert','ja')"><div class="stat-val" style="color:#d94f4f">${esk}</div><div class="stat-label">🚨 Eskaliert</div></div>`
      : '') +
    `<div class="stat ${isA('prio','hoch')?'active-filter':''}" onclick="setFilter('prio','hoch')"><div class="stat-val" style="color:var(--red)">${hoch}</div><div class="stat-label">🔴 Hohe Prio</div></div>` +
    `<div class="stat ${isA('checkin','offen')?'active-filter':''}" onclick="setFilter('checkin','offen')"><div class="stat-val" style="color:var(--amber)">${unk}</div><div class="stat-label">👁 Unkontrolliert</div></div>` +
    `<div class="stat" onclick="openZeitBuchen()" style="cursor:pointer"><div class="stat-val" style="color:var(--amber)">${zeitOffen}</div><div class="stat-label">⏱ Zeit buchen</div></div>` +
    `<div class="stat" style="cursor:default"><div class="stat-val" style="color:var(--green)">${archiv.length}</div><div class="stat-label">✅ Erledigt</div></div>`;

  const hint = document.getElementById('filter-hint');
  if (activeFilter) {
    hint.classList.add('show');
    const labels = {'prio:hoch':'Hohe Priorität','checkin:offen':'Heute unkontrolliert','eskaliert:ja':'Eskaliert'};
    document.getElementById('filter-hint-text').textContent = labels[`${activeFilter.type}:${activeFilter.value}`] || '';
  } else {
    hint.classList.remove('show');
  }
};

// ══════════════════════════════════════════════════════════════════════════════
// Geteilte Tabs dynamisch aufbauen
// ══════════════════════════════════════════════════════════════════════════════
// ══════════════════════════════════════════════════════════════════════════════
// Freigaben – Dropdown + inline Sektionen im Aktiv-Tab
// ══════════════════════════════════════════════════════════════════════════════

function _rebuildSharedTabs() {
  document.querySelectorAll('.tab-shared, .section-shared, #shares-dropdown-wrap').forEach(el => el.remove());
  if (!_sharesWithMe.length) return;

  // ── Dropdown-Button in der Tab-Bar ────────────────────────────────────────
  const tabsBar = document.querySelector('.tabs');
  const wrap = document.createElement('div');
  wrap.id = 'shares-dropdown-wrap';
  wrap.style.cssText = 'position:relative;display:inline-flex;align-items:stretch;';
  const activeCount = _activeSharedOwners.size;
  wrap.innerHTML = `
    <div class="tab tab-shared" id="tab-shares-btn"
         onclick="_toggleSharesDropdown(event)"
         style="display:flex;align-items:center;gap:5px;user-select:none;cursor:pointer">
      👁 Freigaben
      ${activeCount ? `<span style="background:var(--accent,#4FC06A);color:#fff;border-radius:10px;
        font-size:10px;padding:1px 6px;min-width:16px;text-align:center">${activeCount}</span>` : ''}
      ▾
    </div>
    <div id="shares-dropdown-menu"
         style="display:none;position:absolute;top:calc(100% + 4px);left:0;min-width:220px;
                background:var(--bg2,#1e1e2e);border:1px solid var(--border,#333);
                border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.35);z-index:9000;overflow:hidden">
      ${_sharesWithMe.map(s => {
        const checked = _activeSharedOwners.has(s.owner_id);
        return `<div class="shares-dd-item"
             onclick="_toggleSharedOwner(${s.owner_id},event)"
             style="padding:9px 14px;cursor:pointer;display:flex;align-items:center;gap:9px;
                    font-size:13px;color:var(--text,#ccc);
                    ${checked ? 'background:rgba(79,192,106,.12)' : ''}">
          <input type="checkbox" id="shr-chk-${s.owner_id}" ${checked ? 'checked' : ''}
                 onclick="event.stopPropagation()" style="pointer-events:none;accent-color:var(--accent,#4FC06A)">
          ${s.permission === 'write' ? '✏️' : '👁'}
          <span style="flex:1">${escHtml(s.owner_tag)}</span>
          ${s.permission === 'write' ? '<span style="font-size:10px;color:var(--text2)">Schreibrecht</span>' : ''}
        </div>`;
      }).join('')}
    </div>`;
  tabsBar.appendChild(wrap);
}

function _toggleSharesDropdown(e) {
  if (e) e.stopPropagation();
  const menu = document.getElementById('shares-dropdown-menu');
  if (!menu) return;
  const open = menu.style.display !== 'none';
  menu.style.display = open ? 'none' : 'block';
  if (!open) {
    const close = ev => {
      if (!ev.target.closest('#shares-dropdown-wrap')) {
        menu.style.display = 'none';
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

async function _toggleSharedOwner(ownerId, e) {
  if (e) e.stopPropagation();
  const chk = document.getElementById('shr-chk-' + ownerId);
  const item = chk && chk.closest('.shares-dd-item');

  if (_activeSharedOwners.has(ownerId)) {
    // Ausblenden
    _activeSharedOwners.delete(ownerId);
    if (chk) chk.checked = false;
    if (item) item.style.background = '';
    document.getElementById('shared-section-' + ownerId)?.remove();
  } else {
    // Einblenden
    _activeSharedOwners.add(ownerId);
    if (chk) chk.checked = true;
    if (item) item.style.background = 'rgba(79,192,106,.12)';
    await _loadAndShowSharedSection(ownerId);
  }
  // Badge im Button aktualisieren, Dropdown danach wieder öffnen
  _rebuildSharedTabs();
  const newMenu = document.getElementById('shares-dropdown-menu');
  if (newMenu) newMenu.style.display = 'block';
}

async function _loadAndShowSharedSection(ownerId) {
  const share = _sharesWithMe.find(s => s.owner_id === ownerId);
  if (!share) return;

  // Sektion erstellen / Platzhalter anzeigen
  _ensureSharedSection(ownerId, share, true);

  try {
    const data = await API.get(`/shared/${ownerId}/data`);
    _sharedData[ownerId] = data;
    _renderSharedSection(ownerId, data.todos || [], share.permission);
  } catch (err) {
    const body = document.getElementById('shr-body-' + ownerId);
    if (body) body.innerHTML =
      `<div style="color:var(--red,#e55);padding:12px">Fehler: ${escHtml(err.message)}</div>`;
  }
}

function _ensureSharedSection(ownerId, share, loading) {
  if (document.getElementById('shared-section-' + ownerId)) return;
  const canWrite = share.permission === 'write';

  const section = document.createElement('div');
  section.id = 'shared-section-' + ownerId;
  section.style.cssText = 'margin-top:32px';

  section.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <div style="height:2px;flex:1;background:linear-gradient(90deg,var(--accent,#4FC06A),transparent)"></div>
      <span style="font-size:13px;font-weight:700;color:var(--text);white-space:nowrap">
        ${canWrite ? '✏️' : '👁'} ${escHtml(share.owner_tag.split('#')[0])}'s Todos
      </span>
      <span style="font-size:11px;color:var(--text2)">${escHtml(share.owner_tag)}</span>
      <div style="height:2px;flex:1;background:linear-gradient(270deg,var(--accent,#4FC06A),transparent)"></div>
      <button onclick="_toggleSharedOwner(${ownerId})" title="Ausblenden"
              style="background:none;border:none;color:var(--text2);cursor:pointer;font-size:14px;padding:0 2px">✕</button>
    </div>
    ${canWrite ? `<div id="shr-quickadd-${ownerId}" style="margin-bottom:16px">${_shrQuickAddFormHTML(ownerId)}</div>` : ''}
    <div id="shr-body-${ownerId}">
      ${loading ? '<div style="text-align:center;padding:24px;color:var(--text2)">Laden…</div>' : ''}
    </div>`;

  // Anhängen an #section-aktiv (nach #todo-list)
  const aktiv = document.getElementById('section-aktiv');
  if (aktiv) aktiv.appendChild(section);
}

function _shrQuickAddFormHTML(ownerId) {
  return `
    <div style="background:var(--bg3,#f8f9fc);border:1px solid var(--border);border-radius:8px;padding:10px 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input type="text" id="shr-inp-title-${ownerId}" placeholder="Aufgabe" tabindex="-1"
             style="flex:2;min-width:140px;background:var(--bg2,#fff);border:1px solid var(--border);
                    border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text);outline:none">
      <input type="text" id="shr-inp-kunde-${ownerId}" placeholder="Kundenname" tabindex="-1"
             style="flex:1;min-width:100px;background:var(--bg2,#fff);border:1px solid var(--border);
                    border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text);outline:none">
      <input type="url" id="shr-inp-link-${ownerId}" placeholder="🔗 URL (optional)" tabindex="-1"
             style="flex:1;min-width:120px;background:var(--bg2,#fff);border:1px solid var(--border);
                    border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text);outline:none"
             onkeydown="if(event.key==='Enter')_shrSubmitQuickAdd(${ownerId})">
      <select id="shr-inp-prio-${ownerId}" tabindex="-1"
              style="background:var(--bg2,#fff);border:1px solid var(--border);border-radius:6px;
                     padding:6px 8px;font-size:13px;color:var(--text);outline:none">
        <option value="mittel">Mittel</option>
        <option value="hoch">Hoch</option>
        <option value="niedrig">Niedrig</option>
      </select>
      <button onclick="_shrSubmitQuickAdd(${ownerId})"
              style="background:var(--accent,#4FC06A);color:#fff;border:none;border-radius:6px;
                     padding:6px 14px;font-size:13px;cursor:pointer;white-space:nowrap">+ Hinzufügen</button>
    </div>`;
}

function _renderSharedSection(ownerId, todoList, permission) {
  const body = document.getElementById('shr-body-' + ownerId);
  if (!body) return;
  const canWrite = permission === 'write';
  const active = todoList.filter(t => !t.is_archived && t.is_archived !== 1);

  if (!active.length) {
    body.innerHTML = '<div style="text-align:center;padding:24px;color:var(--text2)">📭 Keine offenen Todos.</div>';
    return;
  }

  const grouped = {};
  active.forEach(t => { const k = t.kunde || '–'; (grouped[k] = grouped[k] || []).push(t); });

  const todayISO = new Date().toISOString().slice(0, 10);
  const CI_CLS = { done:'s-done', waiting:'s-waiting', blocked:'s-blocked' };
  const CI_LBL = { done:'✅ Erledigt', waiting:'⏳ Wartet', blocked:'🔴 Blockiert' };

  function todayVal(t) {
    const log = t.checkinLog || t.checkinHistory || [];
    const last = log[log.length - 1];
    return (last && last.date === todayISO) ? last.val : null;
  }

  body.innerHTML = Object.entries(grouped).sort(([a],[b]) => a.localeCompare(b)).map(([kunde, items]) => `
    <div style="margin-bottom:18px">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;
                  color:var(--text2);border-bottom:1px solid var(--border);padding-bottom:3px;margin-bottom:6px">
        ${escHtml(kunde)}
      </div>
      ${items.map(t => {
        const val     = todayVal(t);
        const btnCls  = val ? (CI_CLS[val] || '') : '';
        const btnTxt  = val ? (CI_LBL[val] || val) : '👁 Check-in';
        const uid     = 'shr-' + ownerId + '-' + t.id;
        const hasDesc = t.desc && t.desc.trim();
        const komms   = (t.kommentare || []).filter(k => k.isSharedEdit).slice(0, 3);

        const prioDot = canWrite
          ? `<div class="prio-dot prio-${t.prio||'mittel'}"
                  style="cursor:pointer;margin-top:3px;flex-shrink:0" title="Priorität wechseln"
                  onclick="_sharedCyclePrio(${ownerId},'${t.id}',this)"></div>`
          : `<div class="prio-dot prio-${t.prio||'mittel'}" style="cursor:default;margin-top:3px;flex-shrink:0"></div>`;

        // Beschreibung: bei Schreibrecht klares Eingabefeld
        const descBlock = canWrite
          ? `<div contenteditable="true"
                  onpaste="event.preventDefault();document.execCommand('insertText',false,event.clipboardData.getData('text/plain'))"
                  onfocus="this.style.borderColor='var(--accent,#4FC06A)'"
                  onblur="_sharedSaveDesc(${ownerId},'${t.id}',this);this.style.borderColor='var(--border)'"
                  class="shr-desc-edit"
                  data-empty-label="Beschreibung hinzufügen..."
                  style="font-size:12px;color:var(--text);margin-top:5px;min-height:30px;
                         white-space:pre-wrap;outline:none;cursor:text;
                         user-select:text;-webkit-user-select:text;
                         background:var(--bg2,#fff);border:1px solid var(--border);
                         border-radius:5px;padding:5px 8px"
             >${escHtml(t.desc || '')}</div>`
          : (hasDesc
              ? `<div style="margin-top:5px;font-size:12px;color:var(--text2);
                             background:var(--bg3,rgba(0,0,0,.04));border:1px solid var(--border);
                             border-radius:5px;padding:5px 8px;white-space:pre-wrap">${escHtml(t.desc)}</div>`
              : '');

        // Alle isSharedEdit-Kommentare anzeigen (neueste zuerst, max 5)
        const allKomms = (t.kommentare || []).filter(k => k.isSharedEdit).slice(0, 5);
        const kommsBlock = allKomms.length ? `
          <div style="margin-top:6px;border-top:1px solid var(--border);padding-top:4px">
            ${allKomms.map(k => {
              const ts = k.ts || k.datum || '';
              const tsStr = ts ? new Date(ts).toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
              return `<div style="font-size:11px;padding:4px 8px;margin-bottom:2px;border-radius:4px;
                          background:rgba(42,82,152,.1);border-left:2px solid #4a82d4;color:var(--text2)">
                <strong style="color:#4a82d4">${escHtml(k.autor||'')}</strong>
                <span style="margin-left:4px">${escHtml(k.text)}</span>
                <span style="float:right;opacity:.55">${tsStr}</span>
              </div>`;
            }).join('')}
          </div>` : '';

        // Link-Button
        const linkBtn = t.link
          ? `<a href="${escHtml(t.link)}" target="_blank" rel="noopener"
                class="btn btn-sm"
                style="flex-shrink:0;font-size:11px;padding:3px 8px;text-decoration:none"
                title="${escHtml(t.link)}">🔗</a>`
          : '';

        return `
        <div style="background:var(--bg2,#fff);border:1px solid var(--border);border-radius:7px;
                    padding:9px 11px;margin-bottom:4px">
          <div style="display:flex;align-items:flex-start;gap:7px">
            ${prioDot}
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;font-weight:600;color:var(--text)">${escHtml(t.title)}</div>
              ${t.sub ? `<div style="font-size:11px;color:var(--text2);margin-top:1px">${escHtml(t.sub)}</div>` : ''}
              ${descBlock}
              ${kommsBlock}
            </div>
            <div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end;flex-shrink:0">
              <button class="checkin-btn ${btnCls}"
                      style="pointer-events:none;font-size:11px;padding:3px 8px"
                      title="Heutiger Status (read-only)">${btnTxt} ▾</button>
              ${linkBtn}
            </div>
          </div>
        </div>`;
      }).join('')}
    </div>`).join('');
}

function _toggleSharedDesc(uid) {
  const el = document.getElementById(uid + '-desc');
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  const btn = el.previousElementSibling;
  if (btn) btn.textContent = (open ? '▶' : '▼') + ' Beschreibung';
}

// ── Schreibrecht-Helfer ────────────────────────────────────────────────────────

async function _sharedCyclePrio(ownerId, todoId, el) {
  const data = _sharedData[ownerId];
  if (!data) return;
  const todo = data.todos.find(t => String(t.id) === String(todoId));
  if (!todo) return;
  const prios = ['niedrig', 'mittel', 'hoch'];
  todo.prio = prios[(prios.indexOf(todo.prio || 'mittel') + 1) % prios.length];
  el.className = `prio-dot prio-${todo.prio}`;
  el.style.cursor = 'pointer';
  await _saveSharedData(ownerId);
}

async function _sharedSaveDesc(ownerId, todoId, el) {
  el.style.borderColor = 'var(--border)';
  const val = el.textContent.trim();
  if (!val) return;   // leer → nicht speichern
  const data = _sharedData[ownerId];
  if (!data) return;
  const todo = data.todos.find(t => String(t.id) === String(todoId));
  if (!todo || todo.desc === val) return;             // keine Änderung
  const oldDesc = todo.desc;
  todo.desc = val;
  // Kommentar mit Autor + Vorher/Jetzt
  const tag = API.getUserTag() || '?';
  const _trunc = (s, n) => s ? (s.length > n ? s.slice(0, n) + '…' : s) : '';
  const komm = {
    autor:        tag,
    text:         oldDesc
      ? `Beschreibung geändert\nVorher: "${_trunc(oldDesc, 80)}"\nJetzt: "${_trunc(val, 80)}"`
      : `Beschreibung hinzugefügt: "${_trunc(val, 120)}"`,
    ts:           new Date().toISOString(),
    isSharedEdit: true,
  };
  todo.kommentare = todo.kommentare || [];
  todo.kommentare.unshift(komm);


  await _saveSharedData(ownerId);
  // Kommentar-Block im DOM nachziehen
  const share = _sharesWithMe.find(s => s.owner_id === ownerId);
  _renderSharedSection(ownerId, data.todos, share ? share.permission : 'read');
}

async function _shrSubmitQuickAdd(ownerId) {
  const titleEl = document.getElementById('shr-inp-title-' + ownerId);
  const kundeEl = document.getElementById('shr-inp-kunde-' + ownerId);
  const linkEl  = document.getElementById('shr-inp-link-'  + ownerId);
  const prioEl  = document.getElementById('shr-inp-prio-'  + ownerId);
  const title = titleEl ? titleEl.value.trim() : '';
  if (!title) { if (titleEl) titleEl.focus(); return; }
  const kunde = kundeEl ? kundeEl.value.trim() : '';
  const link  = linkEl  ? linkEl.value.trim()  : '';
  const prio  = prioEl  ? prioEl.value         : 'mittel';
  const data  = _sharedData[ownerId];
  if (!data) return;
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2, 5);
  data.todos.push({
    id, title, kunde, sub: '', subsub: '', prio, umgebung: 'prod',
    desc: '', link, recur: null, blockedBy: null,
    created: new Date().toISOString(), checkinLog: [], kommentare: [],
  });
  if (titleEl) titleEl.value = '';
  if (kundeEl) kundeEl.value = '';
  if (linkEl)  linkEl.value  = '';
  await _saveSharedData(ownerId);
  const share = _sharesWithMe.find(s => s.owner_id === ownerId);
  _renderSharedSection(ownerId, data.todos, share ? share.permission : 'write');
}

async function _saveSharedData(ownerId) {
  const data = _sharedData[ownerId];
  if (!data) return;
  try {
    await API.put(`/shared/${ownerId}/data`, {
      todos: data.todos || [], archiv: data.archiv || [],
      berichte: data.berichte || [], kundenNotizen: data.kundenNotizen || {},
    });
  } catch (e) { showToast('Speichern fehlgeschlagen: ' + e.message); }
}

async function saveShared(ownerId) {
  await _saveSharedData(ownerId);
  const el = document.getElementById('save-indicator');
  if (el) { el.textContent = '💾 Freigabe gespeichert'; el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 1800); }
}

// ══════════════════════════════════════════════════════════════════════════════
// Freigabe-Verwaltung Modal
// ══════════════════════════════════════════════════════════════════════════════
function openShareModal() {
  _loadShareModal();
  document.getElementById('share-modal').classList.add('show');
}
function closeShareModal() {
  document.getElementById('share-modal').classList.remove('show');
}

async function _loadShareModal() {
  try {
    const res = await API.get('/shares');
    _sharesOutgoing = res.outgoing || [];
    _renderShareList();
  } catch (e) { console.error(e); }
}

function _renderShareList() {
  const el = document.getElementById('share-list');
  if (!_sharesOutgoing.length) {
    el.innerHTML = '<div style="font-size:13px;color:var(--text2);font-style:italic">Noch keine Freigaben.</div>';
    return;
  }
  el.innerHTML = _sharesOutgoing.map(s => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:13px">
        <strong>${escHtml(s.viewer_tag)}</strong>
        <span style="font-size:11px;color:var(--text2);margin-left:6px">${s.permission === 'write' ? '✏️ Bearbeiten' : '👁 Lesen'}</span>
      </span>
      <button class="btn btn-sm" style="border-color:var(--red);color:var(--red)" onclick="deleteShare(${s.id})">Entfernen</button>
    </div>
  `).join('');
}

async function addShare() {
  const tag  = document.getElementById('share-user-tag').value.trim();
  const perm = document.getElementById('share-perm').value;
  if (!tag) return;
  try {
    await API.post('/shares', { viewer_tag: tag, permission: perm });
    document.getElementById('share-user-tag').value = '';
    await _loadShareModal();
    await window._loadData();
    render();
    showToast('Freigabe erstellt ✓');
  } catch (e) { showToast('Fehler: ' + e.message); }
}

async function deleteShare(shareId) {
  try {
    await API.request('DELETE', `/shares/${shareId}`);
    await _loadShareModal();
    await window._loadData();
    render();
    showToast('Freigabe entfernt');
  } catch (e) { showToast('Fehler: ' + e.message); }
}

// ══════════════════════════════════════════════════════════════════════════════
// Assign-Modal: openAssignModal + submitAssign überschreiben
//
// WICHTIG: Diese Overrides müssen in DOMContentLoaded gesetzt werden!
// Grund: function-Deklarationen in script-Block 2 von index.html (openAssignModal,
// submitAssign) werden nach extra.js ausgeführt und würden window.* Overrides
// die hier global gesetzt werden sofort wieder überschreiben.
// DOMContentLoaded feuert NACH allen Script-Blöcken → Override bleibt erhalten.
// ══════════════════════════════════════════════════════════════════════════════
let _assignTodoIdExt = null;  // globale Variable, kein Konflikt mit index.html

// ══════════════════════════════════════════════════════════════════════════════
// Header: user_tag + Admin-Link + Freigabe-Button
//
// _setupHeader() wird sowohl bei DOMContentLoaded als auch direkt aufgerufen
// (für den Fall dass DOMContentLoaded schon gefeuert hat als extra.js lädt).
// ══════════════════════════════════════════════════════════════════════════════
function _setupHeader() {
  const actions = document.querySelector('.header-actions');
  if (!actions || document.getElementById('extra-share-btn')) return; // guard: nicht doppelt

  // ── Freigabe-Button (vor dem ⚙️-Button einfügen) ─────────────────────────
  const shareBtn = document.createElement('button');
  shareBtn.id = 'extra-share-btn';
  shareBtn.className = 'btn btn-sm';
  shareBtn.innerHTML = '🔗 Freigaben';
  shareBtn.onclick = openShareModal;
  const settingsWrap = document.querySelector('.settings-wrap');
  actions.insertBefore(shareBtn, settingsWrap || null);

  // ── Username + Admin-Link aus Cache befüllen ──────────────────────────────
  _applyUserInfoToHeader(API.getUserTag(), API.getUserRole());
}

function _applyUserInfoToHeader(userTag, role) {
  if (userTag && userTag !== 'undefined' && userTag !== 'null') {
    const hdr = document.getElementById('header-username');
    if (hdr) hdr.textContent = '👤 ' + userTag;
  }
  const lnk = document.getElementById('settings-admin-link');
  if (lnk) lnk.style.display = (role === 'admin') ? '' : 'none';
}

// Extra.js lädt synchron kurz vor </body> – DOM ist bereits geparst,
// aber DOMContentLoaded ist noch nicht gefeuert (es wartet auf alle Scripts).
// Beide Pfade absichern:
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _setupHeader);
} else {
  _setupHeader(); // DOM schon bereit
}

async function _refreshHeaderFromAPI() {
  if (!API.isLoggedIn()) return;
  try {
    const me = await API.get('/auth/me');
    if (!me?.user_tag) return;

    // Stufe 2: Server-Wahrheit einsetzen
    _applyUserInfoToHeader(me.user_tag, me.role);

    // localStorage aktuell halten (für Seitenreloads)
    API.setSession(API.getToken(), me.user_tag, me.role);
  } catch (_) {
    // Netzwerkfehler → Stufe-1-Werte (aus localStorage) bleiben im Header
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// DOM: Share-Modal + CSS anhängen
// ══════════════════════════════════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  // CSS
  const style = document.createElement('style');
  style.textContent = `
    /* Delegierungs-Badges */
    .tag-delegiert{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:11px;background:rgba(42,82,152,.09);color:#2A5298;border:1px solid rgba(42,82,152,.25);padding:5px 10px;border-radius:6px;margin-bottom:6px;border-left:3px solid #2A5298;}
    .tag-delegiert--from{background:rgba(79,192,106,.1);color:#2a8a4a;border-color:rgba(79,192,106,.3);border-left-color:#3aaa56;}
    .tag-rejected{display:flex;align-items:center;gap:5px;flex-wrap:wrap;font-size:11px;background:rgba(217,79,79,.08);color:#c03030;border:1px solid rgba(217,79,79,.3);padding:5px 10px;border-radius:6px;margin-bottom:6px;border-left:3px solid #d94f4f;}
    .tag-retract-btn{font-size:10px!important;padding:1px 7px!important;border-color:rgba(42,82,152,.4)!important;color:#2A5298!important;background:transparent!important;}
    .tag-erledigt-von{font-size:11px;background:rgba(79,192,106,.1);color:#3aaa56;border:1px solid rgba(79,192,106,.3);padding:2px 8px;border-radius:4px;}
    /* Delegiertes/gesperrtes Todo-Item – kompletter grauer Look */
    .todo-item.todo-delegated{background:#f5f5f5!important;border-color:#d8d8d8!important;cursor:default!important;}
    .todo-item.todo-delegated .todo-title{color:#888!important;}
    .todo-item.todo-delegated .todo-desc-edit{color:#aaa!important;}
    .todo-item.todo-delegated .todo-meta *{color:#bbb!important;border-color:#ddd!important;}
    .todo-item.todo-delegated .tag-age,.todo-item.todo-delegated .tag-prod,.todo-item.todo-delegated .tag-beta,.todo-item.todo-delegated .tag-intern{opacity:.5;}
    .todo-item.todo-delegated .last-checkin{color:#bbb!important;}
    /* Abgelehntes Todo-Item */
    .todo-item.todo-rejected{background:rgba(217,79,79,.03)!important;border-color:rgba(217,79,79,.18)!important;border-left:3px solid #d94f4f!important;}
    .tab-shared{background:rgba(255,255,255,.08);}
    #share-modal .modal{max-width:460px;}
    .settings-menu-item{display:block;width:100%;text-align:left;background:none;border:none;padding:10px 16px;font-size:13px;color:var(--text,#ccc);cursor:pointer;white-space:nowrap;}
    .settings-menu-item:hover{background:var(--bg3,#2a2a3e);}
    .shares-dd-item:hover{background:var(--bg3,#2a2a3e)!important;}
    #shares-view-modal .modal{overflow:hidden;}
  `;
  document.head.appendChild(style);

  // Share-Modal HTML
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'share-modal';
  overlay.innerHTML = `
    <div class="modal" style="max-width:460px">
      <h3>🔗 Freigaben verwalten</h3>
      <p style="font-size:12px;color:var(--text2);margin-bottom:14px">
        Erlaube anderen Usern, deinen Bereich zu sehen oder zu bearbeiten.
      </p>
      <div style="display:flex;gap:8px;margin-bottom:16px;align-items:flex-end">
        <div style="flex:2">
          <div class="form-label">Benutzer-Tag (z.B. max#0042)</div>
          <input id="share-user-tag" type="text" placeholder="username#1234"
                 style="width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;"
                 onkeydown="if(event.key==='Enter')addShare()">
        </div>
        <div style="flex:1">
          <div class="form-label">Berechtigung</div>
          <select id="share-perm"
                  style="width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;">
            <option value="read">👁 Lesen</option>
            <option value="write">✏️ Bearbeiten</option>
          </select>
        </div>
        <button class="btn btn-primary" onclick="addShare()">+ Hinzufügen</button>
      </div>
      <div id="share-list" style="min-height:40px"></div>
      <div class="modal-actions">
        <button class="btn modal-btn" onclick="closeShareModal()">Schließen</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  // Passwort-ändern Modal
  const pwOverlay = document.createElement('div');
  pwOverlay.className = 'modal-overlay';
  pwOverlay.id = 'changepw-modal';
  pwOverlay.innerHTML = `
    <div class="modal" style="max-width:380px">
      <h3>🔑 Passwort ändern</h3>
      <div class="form-group" style="margin-bottom:12px">
        <div class="form-label">Altes Passwort</div>
        <input id="cpw-old" type="password" class="form-input" placeholder="Aktuelles Passwort"
               style="width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;">
      </div>
      <div class="form-group" style="margin-bottom:12px">
        <div class="form-label">Neues Passwort</div>
        <input id="cpw-new" type="password" class="form-input" placeholder="Mindestens 4 Zeichen"
               style="width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;">
      </div>
      <div class="form-group" style="margin-bottom:16px">
        <div class="form-label">Wiederholen</div>
        <input id="cpw-new2" type="password" class="form-input" placeholder="Neues Passwort wiederholen"
               style="width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:8px;font-size:13px;outline:none;"
               onkeydown="if(event.key==='Enter')doChangePw()">
      </div>
      <div id="cpw-error" style="color:#e55;font-size:12px;margin-bottom:10px;display:none"></div>
      <div id="cpw-success" style="color:#4c4;font-size:12px;margin-bottom:10px;display:none"></div>
      <div class="modal-actions">
        <button class="btn modal-btn btn-primary" onclick="doChangePw()">Passwort ändern</button>
        <button class="btn modal-btn" onclick="closeChangePwModal()">Abbrechen</button>
      </div>
    </div>`;
  document.body.appendChild(pwOverlay);

  // ── Assign-Modal Overrides (hier: NACH allen Script-Blöcken) ──────────────
  window.openAssignModal = async function (todoId, event) {
    if (event) event.stopPropagation();
    _assignTodoIdExt = todoId;
    const todo = todos.find(t => t.id === todoId);
    if (!todo) { console.warn('[extra.js] Todo nicht gefunden:', todoId); return; }
    document.getElementById('assign-todo-preview').textContent = todo.title;
    document.getElementById('assign-comment').value = '';
    try {
      const users = await API.getUsers();
      if (!users || !users.length) { showToast('Keine anderen Benutzer vorhanden'); return; }
      document.getElementById('assign-user-select').innerHTML = users
        .map(u => `<option value="${escAttr(u.user_tag)}">${escHtml(u.user_tag)}</option>`)
        .join('');
    } catch (e) { showToast('Benutzer laden fehlgeschlagen: ' + e.message); return; }
    document.getElementById('assign-modal').classList.add('show');
  };

  window.submitAssign = async function () {
    const toUserTag = document.getElementById('assign-user-select').value;
    const comment   = document.getElementById('assign-comment').value.trim() || null;
    if (!toUserTag || !_assignTodoIdExt) {
      console.warn('[extra.js] submitAssign abgebrochen – toUserTag:', toUserTag, '_assignTodoIdExt:', _assignTodoIdExt);
      return;
    }
    try {
      await API.post(`/assign/${_assignTodoIdExt}`, { to_user_tag: toUserTag, comment });
      document.getElementById('assign-modal').classList.remove('show');
      _assignTodoIdExt = null;
      await window._loadData();
      render();
      showToast(`Todo an ${toUserTag} zugewiesen ✉️`);
    } catch (e) { showToast('Zuweisung fehlgeschlagen: ' + e.message); }
  };
});

// ── Zuweisung zurückziehen ─────────────────────────────────────────────────────
async function retractAssign(todoId, event) {
  if (event) event.stopPropagation();
  if (!confirm('Zuweisung zurückziehen?')) return;
  try {
    await API.retractAssign(todoId);
    await window._loadData();
    render();
    showToast('Zuweisung zurückgezogen');
  } catch (e) { showToast('Fehler: ' + e.message); }
}

// ── Passwort-ändern Modal ─────────────────────────────────────────────────────
function openChangePwModal() {
  document.getElementById('cpw-old').value = '';
  document.getElementById('cpw-new').value = '';
  document.getElementById('cpw-new2').value = '';
  document.getElementById('cpw-error').style.display = 'none';
  document.getElementById('cpw-success').style.display = 'none';
  document.getElementById('changepw-modal').style.display = 'flex';
  setTimeout(() => document.getElementById('cpw-old').focus(), 50);
}

function closeChangePwModal() {
  document.getElementById('changepw-modal').style.display = 'none';
}

async function doChangePw() {
  const oldPw  = document.getElementById('cpw-old').value;
  const newPw  = document.getElementById('cpw-new').value;
  const newPw2 = document.getElementById('cpw-new2').value;
  const errEl  = document.getElementById('cpw-error');
  const okEl   = document.getElementById('cpw-success');

  errEl.style.display = 'none';
  okEl.style.display  = 'none';

  if (!oldPw || !newPw || !newPw2) { errEl.textContent = 'Bitte alle Felder ausfüllen.'; errEl.style.display = ''; return; }
  if (newPw !== newPw2)            { errEl.textContent = 'Neue Passwörter stimmen nicht überein.'; errEl.style.display = ''; return; }
  if (newPw.length < 4)           { errEl.textContent = 'Neues Passwort muss mindestens 4 Zeichen haben.'; errEl.style.display = ''; return; }

  try {
    await API.post('/auth/change-password', { old_password: oldPw, new_password: newPw });
    okEl.textContent = '✓ Passwort erfolgreich geändert.';
    okEl.style.display = '';
    setTimeout(closeChangePwModal, 1500);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.style.display = '';
  }
}

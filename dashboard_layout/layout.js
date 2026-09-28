/* View preferences only. Never sends requests or changes trading controls. */
(() => {
  'use strict';
  function init() {
    const desk = document.getElementById('desk');
    if (!desk || document.getElementById('layoutEditor')) return;
    const key = 'kadima.celestial.layout.v1';
    const records = new Map(), defaults = [];
    let order = [], draft = [], reset = false, drag = null, frame = 0;
    const toolbar = document.createElement('div'); toolbar.className = 'layout-toolbar';
    toolbar.innerHTML = '<span>Make this desk yours</span><button type="button" data-view id="editLayout">↕ Edit layout</button><span id="layoutStatus" role="status" aria-live="polite">Row positions are saved in this browser.</span>';
    desk.prepend(toolbar);
    const dialog = document.createElement('dialog'); dialog.id = 'layoutEditor';
    dialog.setAttribute('aria-labelledby', 'layoutTitle');
    dialog.innerHTML = '<div class="layout-dialog-head"><div><div class="eyebrow">Your workspace</div><h2 id="layoutTitle">Arrange your dashboard</h2></div><button data-view type="button" id="layoutClose" aria-label="Cancel layout editing">✕</button></div><p id="layoutHelp">Drag the grip to move a row, or use the arrow buttons. Keyboard: focus a grip and use ↑ / ↓ or Home / End. Changes appear when you save.</p><ol id="layoutList" aria-label="Dashboard row order" aria-describedby="layoutHelp"></ol><p id="layoutAnnounce" role="status" aria-live="polite"></p><div class="layout-actions"><button data-view type="button" id="layoutReset">Reset to default</button><div><button data-view type="button" id="layoutCancel">Cancel</button> <button data-view type="button" class="primary" id="layoutSave">Save layout</button></div></div>';
    desk.append(dialog);
    const $ = id => document.getElementById(id), list = $('layoutList');
    const rows = document.createElement('div'); rows.id = 'dashboardRows';
    rows.className = 'layout-rows'; toolbar.after(rows);
    function discover() {
      const overview = document.getElementById('legacy-panels');
      const candidates = [...desk.children].filter(n => n.matches('section') || n === overview);
      if (overview) candidates.push(...overview.querySelectorAll(':scope > .sections > section'));
      let added = false;
      for (const node of candidates) {
        if ([...records.values()].some(r => r.node === node)) continue;
        const identity = node.id || node.querySelector('[id]')?.id;
        const title = node.querySelector('h2')?.textContent.trim();
        if (!identity || !title) continue;
        const id = 'row:' + identity;
        if (records.has(id)) continue;
        const anchor = document.createComment('layout position: ' + identity);
        node.before(anchor);
        const wrapper = document.createElement('div'); wrapper.dataset.layoutRow = id;
        const legacy = node.parentElement?.classList.contains('sections');
        wrapper.className = 'layout-row' + (legacy ? ' legacy-panels' : '');
        if (legacy && !node.classList.contains('wide')) wrapper.classList.add('layout-half');
        records.set(id, {node, anchor, wrapper, title}); defaults.push(id); added = true;
      }
      return added;
    }
    function normalize(ids) {
      const seen = new Set();
      return [...ids, ...defaults].filter(id => typeof id === 'string' && records.has(id) && !seen.has(id) && seen.add(id));
    }
    function isDefault(ids) { return ids.length === defaults.length && ids.every((id, i) => id === defaults[i]); }
    function apply(ids) {
      order = normalize(ids);
      if (isDefault(order)) {
        for (const id of defaults) { const r = records.get(id); r.anchor.after(r.node); }
        rows.replaceChildren();
      } else {
        for (const id of order) {
          const r = records.get(id); r.wrapper.append(r.node); rows.append(r.wrapper);
        }
      }
    }
    discover(); order = [...defaults];
    try {
      const raw = localStorage.getItem(key);
      if (raw && raw.length <= 20000) {
        const stored = JSON.parse(raw);
        if (stored?.version === 1 && Array.isArray(stored.order)) apply(normalize(stored.order));
      }
    } catch { $('layoutStatus').textContent = 'Default layout loaded. Browser storage is unavailable or invalid.'; }
    function announce(id) {
      $('layoutAnnounce').textContent = records.get(id).title + ', position ' + (draft.indexOf(id) + 1) + ' of ' + draft.length + '.';
    }
    function updateList() {
      [...list.children].forEach((li, i) => {
        li.querySelector('.layout-number').textContent = i + 1;
        li.querySelector('[data-move="-1"]').disabled = i === 0;
        li.querySelector('[data-move="1"]').disabled = i === draft.length - 1;
        li.setAttribute('aria-posinset', i + 1); li.setAttribute('aria-setsize', draft.length);
      });
    }
    function move(id, target) {
      const from = draft.indexOf(id); target = Math.max(0, Math.min(draft.length - 1, target));
      if (from < 0 || from === target) return;
      draft.splice(from, 1); draft.splice(target, 0, id); reset = false;
      const node = [...list.children].find(n => n.dataset.row === id);
      const next = [...list.children].filter(n => n !== node)[target];
      list.insertBefore(node, next || null); updateList(); announce(id);
      if (drag) drag.grip.focus({preventScroll:true});
    }
    function renderList() {
      list.replaceChildren();
      for (const id of draft) {
        const li = document.createElement('li'); li.dataset.row = id;
        li.innerHTML = '<button data-view type="button" class="layout-grip">⠿</button><span class="layout-number"></span><span class="layout-label"></span><button data-view type="button" data-move="-1">↑</button><button data-view type="button" data-move="1">↓</button>';
        const name = records.get(id).title;
        li.querySelector('.layout-label').textContent = name;
        li.querySelector('.layout-grip').setAttribute('aria-label', 'Move ' + name);
        for (const button of li.querySelectorAll('[data-move]')) {
          const step = Number(button.dataset.move);
          button.setAttribute('aria-label', 'Move ' + name + (step < 0 ? ' up' : ' down'));
          button.onclick = () => { move(id, draft.indexOf(id) + step); li.querySelector('.layout-grip').focus(); };
        }
        const grip = li.querySelector('.layout-grip');
        grip.onkeydown = e => {
          const index = draft.indexOf(id), targets = {ArrowUp:index - 1, ArrowDown:index + 1, Home:0, End:draft.length - 1};
          if (Object.hasOwn(targets, e.key)) { e.preventDefault(); move(id, targets[e.key]); grip.focus(); li.scrollIntoView({block:'nearest'}); }
        };
        grip.onpointerdown = e => {
          if (!e.isPrimary || e.button !== 0 || drag) return;
          e.preventDefault(); grip.focus();
          drag = {id, pointer:e.pointerId, grip, before:[...draft], y:e.clientY, startY:e.clientY, moved:false};
          list.setPointerCapture(e.pointerId); li.classList.add('layout-dragging');
          frame = requestAnimationFrame(tick);
        };
        list.append(li);
      }
      updateList();
    }
    function tick() {
      if (!drag) return;
      if (drag.moved) {
        const bounds = list.getBoundingClientRect();
        if (drag.y < bounds.top + 40) list.scrollTop -= 12;
        else if (drag.y > bounds.bottom - 40) list.scrollTop += 12;
        const others = [...list.children].filter(li => li.dataset.row !== drag.id);
        const target = others.findIndex(li => { const box = li.getBoundingClientRect(); return drag.y < box.top + box.height / 2; });
        move(drag.id, target < 0 ? others.length : target);
      }
      frame = requestAnimationFrame(tick);
    }
    function endDrag(cancel) {
      const previous = drag; drag = null; cancelAnimationFrame(frame);
      if (!previous) return;
      if (list.hasPointerCapture(previous.pointer)) list.releasePointerCapture(previous.pointer);
      list.querySelector('.layout-dragging')?.classList.remove('layout-dragging');
      if (cancel) { draft = previous.before; renderList(); }
    }
    list.onpointermove = e => { if (drag?.pointer === e.pointerId) { drag.y = e.clientY; drag.moved ||= Math.abs(e.clientY - drag.startY) > 4; } };
    list.onpointerup = e => { if (drag?.pointer === e.pointerId) endDrag(false); };
    list.onpointercancel = e => { if (drag?.pointer === e.pointerId) endDrag(true); };
    list.onlostpointercapture = () => { if (drag) endDrag(true); };
    function close() { endDrag(true); dialog.close(); $('editLayout').focus(); }
    $('editLayout').onclick = () => {
      discover(); order = normalize(order); draft = [...order]; reset = false;
      renderList(); $('layoutAnnounce').textContent = 'Saved per browser. Your other devices keep their own layout.';
      dialog.showModal(); list.scrollTop = 0;
    };
    $('layoutClose').onclick = $('layoutCancel').onclick = close;
    dialog.addEventListener('cancel', e => { e.preventDefault(); close(); });
    $('layoutReset').onclick = () => { endDrag(true); draft = [...defaults]; reset = true; renderList(); $('layoutAnnounce').textContent = 'Default order selected. Save to apply, or cancel to keep your layout.'; };
    $('layoutSave').onclick = () => {
      endDrag(false); apply(reset ? defaults : draft);
      try {
        if (isDefault(order)) localStorage.removeItem(key);
        else localStorage.setItem(key, JSON.stringify({version:1, order}));
        $('layoutStatus').textContent = 'Layout saved in this browser.';
      } catch { $('layoutStatus').textContent = 'Layout applied for this visit. Browser storage could not save it.'; }
      close();
    };
    // Stock Fast may be inserted by the existing optional panel script.
    new MutationObserver(() => {
      if (desk.hidden && dialog.open) close();
      if (discover()) { apply(normalize(order)); if (dialog.open) { draft = normalize(draft); renderList(); } }
    }).observe(desk, {childList:true, attributes:true, attributeFilter:['hidden']});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true});
  else init();
})();

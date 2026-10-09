/* KADIMA Celestial: show the Hourly Forecast panel in place of "Daily Price Memory".
 *
 * Display only. The Daily Price Memory section is hidden on the page, not deleted:
 * its data keeps being collected on the server, and removing this script's <script>
 * tag from dashboard.html brings it back.
 *
 * Celestial re-renders parts of its page every few seconds. An iframe inside a
 * re-rendered area would reload each time, so the forecast frame lives once on
 * <body> and is laid over a placeholder that sits where the hidden section was.
 * The placeholder is re-created whenever the dashboard re-renders; the frame is not.
 */
(function () {
  var HIDE_TITLE = 'Daily Price Memory';
  var SRC = '/forecast.html?embed=1&v=4';
  var FRAME_ID = 'kadima-hourly-forecast-frame';
  var HOLD_ID = 'kadima-hourly-forecast-slot';
  var frameHeight = 600;

  function findSection() {
    var nodes = document.querySelectorAll('h1,h2,h3,h4,h5,h6,div,span,p,strong,b');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.children.length === 0 && n.textContent.trim() === HIDE_TITLE) {
        // Climb from the title to the card that holds its table/filters,
        // then past any wrappers that contain nothing but that card.
        var el = n;
        while (el.parentElement && el.parentElement !== document.body
               && !el.querySelector('table,input,select,button')) {
          el = el.parentElement;
        }
        while (el.parentElement && el.parentElement !== document.body
               && el.parentElement.children.length === 1) {
          el = el.parentElement;
        }
        return el;
      }
    }
    return null;
  }

  function frame() {
    var f = document.getElementById(FRAME_ID);
    if (f) return f;
    f = document.createElement('iframe');
    f.id = FRAME_ID;
    f.src = SRC;
    f.title = 'Hourly Forecast';
    f.setAttribute('scrolling', 'no');
    f.style.cssText = 'position:absolute;border:0;background:#0b0f17;color-scheme:dark;border-radius:12px;z-index:5;left:0;top:0;width:0;height:0;';
    document.body.appendChild(f);
    return f;
  }

  function holder() {
    var h = document.getElementById(HOLD_ID);
    if (!h) {
      h = document.createElement('div');
      h.id = HOLD_ID;
      h.style.cssText = 'display:block;width:100%;margin:16px 0;';
    }
    h.style.height = frameHeight + 'px';
    return h;
  }

  function place() {
    var sec = findSection();
    var h = holder();
    if (sec) {
      if (sec.style.display !== 'none') sec.style.display = 'none';
      if (h.parentElement !== sec.parentElement || h.nextElementSibling !== sec) {
        sec.parentElement.insertBefore(h, sec);
      }
    } else if (!h.isConnected) {
      (document.querySelector('main') || document.body).appendChild(h);
    }
  }

  function sync() {
    var f = frame(), h = document.getElementById(HOLD_ID);
    try {
      var ch = f.contentDocument && f.contentDocument.documentElement.scrollHeight;
      if (ch && Math.abs(ch - frameHeight) > 4) {
        frameHeight = ch;
        if (h) h.style.height = ch + 'px';
      }
    } catch (e) { /* same origin only */ }
    if (!h || !h.isConnected) { f.style.display = 'none'; return; }
    var r = h.getBoundingClientRect();
    f.style.display = 'block';
    f.style.left = (r.left + window.scrollX) + 'px';
    f.style.top = (r.top + window.scrollY) + 'px';
    f.style.width = r.width + 'px';
    f.style.height = frameHeight + 'px';
  }

  function tick() { place(); sync(); }

  function start() {
    frame();
    tick();
    var pending = false;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () { pending = false; tick(); });
    }).observe(document.body, {childList: true, subtree: true});
    window.addEventListener('resize', sync);
    setInterval(tick, 500);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();

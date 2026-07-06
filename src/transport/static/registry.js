/* fcoin — shared machine registry widget
 *
 * Renders a live "machines online" card row and a "broadcast / send to one"
 * toggle into a host element. Also patches the global fetch() so any
 * subsequent /submit_prompt POST gets target_agent_id injected into
 * its JSON body when "send to one" mode is active.
 *
 * Usage in any dashboard.html:
 *   <div id="fcoin-registry"></div>
 *   <script src="/static/registry.js"></script>
 *   <script>
 *     FcoinRegistry.init({
 *       anchor:        '#fcoin-registry',
 *       agentId:       'dashboard-jp',
 *       labels: {
 *         machines:    'machines / 機械',
 *         broadcast:   'broadcast / 配信',
 *         target:      'send to one / 単発送信',
 *         online:      'online',
 *         loading:     'loading machines…',
 *         empty:       'no machines online',
 *         error:       'could not load machines',
 *         sentToOne:   'sent to',
 *         sentAll:     'broadcast sent',
 *       },
 *     });
 *   </script>
 */
(function() {
  var FcoinRegistry = {};
  window.FcoinRegistry = FcoinRegistry;

  FcoinRegistry.init = function(opts) {
    var base = location.origin;
    var machines = [];
    var selected = null;
    var mode = 'all';
    var lbl = opts.labels || {};
    var host = document.querySelector(opts.anchor);
    if (!host) return;

    host.innerHTML =
      '<div class="fc-reg">' +
        '<div class="fc-reg-bar">' +
          '<span class="fc-reg-label">' + esc(lbl.machines || 'machines') + '</span>' +
          '<span class="fc-reg-count" id="fc-reg-count"></span>' +
        '</div>' +
        '<div class="fc-reg-toggle">' +
          '<button id="fc-mode-all" class="active">' + esc(lbl.broadcast || 'broadcast') + '</button>' +
          '<button id="fc-mode-one">' + esc(lbl.target || 'send to one') + '</button>' +
        '</div>' +
        '<div class="fc-reg-list" id="fc-reg-list">' +
          '<span class="fc-reg-loading">' + esc(lbl.loading || 'loading…') + '</span>' +
        '</div>' +
      '</div>';

    function setMode(m) {
      mode = m;
      var ba = document.getElementById('fc-mode-all');
      var bo = document.getElementById('fc-mode-one');
      if (ba) ba.classList.toggle('active', m === 'all');
      if (bo) bo.classList.toggle('active', m === 'one');
      if (m === 'all') {
        selected = null;
        var cards = document.querySelectorAll('.fc-reg-card');
        for (var i = 0; i < cards.length; i++) cards[i].classList.remove('selected');
      }
    }
    var ba = document.getElementById('fc-mode-all');
    var bo = document.getElementById('fc-mode-one');
    if (ba) ba.addEventListener('click', function() { setMode('all'); });
    if (bo) bo.addEventListener('click', function() { setMode('one'); });

    function loadMachines() {
      fetch(base + '/machines')
        .then(function(r) { return r.json(); })
        .then(function(d) { machines = d.machines || []; render(); })
        .catch(function() {
          var el = document.getElementById('fc-reg-list');
          if (el) el.innerHTML = '<span class="fc-reg-empty">' + esc(lbl.error || 'error') + '</span>';
        });
    }
    function render() {
      var list = document.getElementById('fc-reg-list');
      var count = document.getElementById('fc-reg-count');
      if (!list || !count) return;
      count.textContent = machines.length + ' ' + (lbl.online || 'online');
      if (!machines.length) {
        list.innerHTML = '<span class="fc-reg-empty">' + esc(lbl.empty || 'none') + '</span>';
        return;
      }
      list.innerHTML = '';
      machines.forEach(function(m) {
        var card = document.createElement('div');
        card.className = 'fc-reg-card' + (selected && selected.agent_id === m.agent_id ? ' selected' : '');
        var age = m.last_seen ? Math.round(Date.now() / 1000 - m.last_seen) : 0;
        var ageStr = age < 60 ? age + 's' : Math.round(age / 60) + 'm';
        card.innerHTML =
          '<div class="fc-reg-name">' + esc(m.agent_id) + '</div>' +
          '<div class="fc-reg-meta">' + esc(m.hostname || '?') + ' · ' + (m.cpu_cores || '?') + 'c' +
          (m.ram_total ? ' · ' + esc(m.ram_total) : '') +
          '</div>' +
          (m.llm_backend ? '<div class="fc-reg-be">' + esc(m.llm_backend) + '</div>' : '') +
          '<div class="fc-reg-seen">' + ageStr + '</div>';
        card.addEventListener('click', function() {
          selected = m;
          setMode('one');
          render();
        });
        list.appendChild(card);
      });
    }
    loadMachines();
    setInterval(loadMachines, 10000);

    // Patch fetch to inject target_agent_id on /submit_prompt
    var origFetch = window.fetch;
    window.fetch = function(input, init) {
      try {
        var url = (typeof input === 'string' ? input : input.url) || '';
        if (mode === 'one' && selected && url.indexOf('/submit_prompt') !== -1 && init && init.body) {
          var body = JSON.parse(init.body);
          body.target_agent_id = selected.agent_id;
          init = Object.assign({}, init, { body: JSON.stringify(body) });
        }
      } catch (e) {}
      return origFetch.apply(this, arguments);
    };
  };

  function esc(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();

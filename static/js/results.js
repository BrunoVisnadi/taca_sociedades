const alertEl   = document.getElementById('alert');
const roundSel  = document.getElementById('round');
const debateSel = document.getElementById('debate');
const positionsEl = document.getElementById('positions');
const chairBox  = document.getElementById('chair');
const wing1Box  = document.getElementById('wing1');
const wing2Box  = document.getElementById('wing2');
const saveBtn   = document.getElementById('save');
saveBtn.disabled = true;
function setBtnDisabled(disabled) {
  saveBtn.disabled = !!disabled;
  // se quiser bloquear clique visualmente mesmo sem :disabled (fallback):
  saveBtn.classList.toggle('pointer-events-none', !!disabled);
  // mantemos a classe hover mesmo desabilitado, mas o Tailwind já cuida do visual com disabled:*
}
function setSaving(isSaving) {
  saveBtn.disabled = isSaving || saveBtn.disabled; // se já estava desabilitado por validação, mantém
  if (isSaving) {
    saveBtn.dataset.prevText = saveBtn.textContent;
    saveBtn.innerHTML = `
      <span class="inline-flex items-center gap-2">
        <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor"
            d="M4 12a8 8 0 018-8v4A4 4 0 004 12z"></path>
        </svg>
        Salvando…
      </span>`;
  } else {
    saveBtn.innerHTML = saveBtn.dataset.prevText || 'Salvar resultados';
  }
}
function showAlert(msg) {
  alertEl.textContent = msg;
  alertEl.classList.remove('hidden');
}
function hideAlert() {
  alertEl.classList.add('hidden');
  alertEl.textContent = '';
}
function option(el, value, label) {
  const o = document.createElement('option');
  o.value = String(value);
  o.textContent = label;
  el.appendChild(o);
}
function validScore(v) {
  if (v === '' || v === null || v === undefined) return false;
  const n = Number(v);
  return Number.isInteger(n) && n >= 50 && n <= 100;
}

// --- Combobox pesquisável (input + lista) ---
function createCombo(containerEl, items, placeholder='— selecione —') {
  // items: [{id:number|string, label:string}]
  containerEl.classList.add('relative');
  containerEl.innerHTML = `
    <input type="text" class="combo-input w-full rounded-lg border-slate-300 focus:ring-sky-500 focus:border-sky-500"
           placeholder="${placeholder}" autocomplete="off">
    <input type="hidden" class="combo-value">
    <div class="combo-list absolute z-20 mt-1 w-full bg-white border border-slate-200 rounded-lg shadow max-h-56 overflow-auto hidden"></div>
  `;
  const inp  = containerEl.querySelector('.combo-input');
  const hid  = containerEl.querySelector('.combo-value');
  const list = containerEl.querySelector('.combo-list');

  let view = [...items];
  let active = -1;

  const render = () => {
    list.innerHTML = '';
    if (!view.length) {
      const li = document.createElement('div');
      li.className = 'px-3 py-2 text-sm text-slate-500';
      li.textContent = 'Nenhum resultado';
      list.appendChild(li);
      return;
    }
    view.forEach((it, i) => {
      const li = document.createElement('div');
      li.className = 'px-3 py-2 text-sm cursor-pointer hover:bg-sky-50';
      if (i === active) li.classList.add('bg-sky-100');
      li.textContent = it.label;
      li.dataset.id = it.id;
      li.addEventListener('mousedown', (e) => { // evita blur antes do click
        e.preventDefault();
        selectItem(it);
      });
      list.appendChild(li);
    });
  };

  const open  = () => { list.classList.remove('hidden'); };
  const close = () => { list.classList.add('hidden'); active = -1; };

  const filter = () => {
    const q = (inp.value || '').toLowerCase();
    view = items.filter(it => it.label.toLowerCase().includes(q));
    active = -1;
    render();
    open();
  };

  const selectItem = (it) => {
    inp.value = it.label;
    hid.value = it.id;
    containerEl.dispatchEvent(new CustomEvent('combo-change', { detail: { id: it.id, label: it.label }}));
    close();
  };

  // teclado / interação
  inp.addEventListener('input', filter);
  inp.addEventListener('focus', () => { filter(); open(); });
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { active = Math.min(active + 1, Math.max(view.length - 1, 0)); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active - 1, -1); render(); e.preventDefault(); }
    else if (e.key === 'Enter') { if (active >= 0 && view[active]) selectItem(view[active]); e.preventDefault(); }
    else if (e.key === 'Escape') { close(); }
  });
  inp.addEventListener('blur', () => setTimeout(close, 120)); // fecha após clique

  // API simples
  return {
    getId: () => hid.value ? String(hid.value) : '',
    setItems: (newItems) => { items = [...newItems]; filter(); },
    setPlaceholder: (ph) => { inp.placeholder = ph; },
  };
}

// Gradiente único azul — 1º mais forte -> 4º mais claro
function colorForRank(rank) {
  const shades = [
    'bg-green-100 ring-2 ring-green-300',   // 1º
    'bg-blue-100 ring-2 ring-blue-300',     // 2º
    'bg-orange-100 ring-2 ring-orange-300', // 3º
    'bg-red-100 ring-2 ring-red-300'        // 4º
  ];
  return shades[rank] || 'bg-slate-50';
}

// Inputs de nota: só 50–100, feedback visual e clamp no blur
function attachScoreInput(inputEl) {
  inputEl.type = 'number';
  inputEl.min = '50';
  inputEl.max = '100';
  inputEl.step = '1';
  inputEl.inputMode = 'numeric';
  inputEl.pattern = '[0-9]*';

  const setError = (err) => {
    inputEl.classList.toggle('ring-2', err);
    inputEl.classList.toggle('ring-red-400', err);
    inputEl.classList.toggle('bg-red-50', err);
    inputEl.classList.toggle('border-red-300', err);
  };

  const sanitize = () => {
    inputEl.value = inputEl.value.replace(/[^0-9]/g, '');
    if (inputEl.value.length > 3) inputEl.value = inputEl.value.slice(0, 3);
    const n = Number(inputEl.value || NaN);
    const inRange = Number.isInteger(n) && n >= 50 && n <= 100;
    setError(!inRange && inputEl.value !== '');
    return inRange;
  };

  inputEl.addEventListener('beforeinput', (e) => {
    if (e.data && /[^0-9]/.test(e.data)) e.preventDefault();
  });
  inputEl.addEventListener('input', () => {
    sanitize();
    calculatePreviewAndPaint();
  });
  inputEl.addEventListener('blur', () => {
    let n = Number(inputEl.value || NaN);
    if (!Number.isInteger(n)) return;
    if (n < 50) n = 50;
    if (n > 100) n = 100;
    inputEl.value = String(n);
    sanitize();
    calculatePreviewAndPaint();
  });
}

function calculatePreviewAndPaint() {
  const cards = Array.from(positionsEl.querySelectorAll('[data-position]'));
  const totals = [];
  let incomplete = false;

  // limpa classes visuais
  cards.forEach(c => c.className = 'rounded-xl border border-slate-200 p-4 space-y-2');

  // valida cada card
  for (const card of cards) {
    const s1Box = card.querySelector('.deb-s1');
    const s2Box = card.querySelector('.deb-s2');
    const sc1El = card.querySelector('.deb-s1-score');
    const sc2El = card.querySelector('.deb-s2-score');

    const s1Id = s1Box && s1Box._combo ? s1Box._combo.getId() : '';
    const s2Id = s2Box && s2Box._combo ? s2Box._combo.getId() : '';

    const raw1 = (sc1El.value || '').trim();
    const raw2 = (sc2El.value || '').trim();
    const sc1 = raw1 === '' ? NaN : parseInt(raw1, 10);
    const sc2 = raw2 === '' ? NaN : parseInt(raw2, 10);

    const scoresOk = Number.isInteger(sc1) && sc1 >= 50 && sc1 <= 100 &&
                     Number.isInteger(sc2) && sc2 >= 50 && sc2 <= 100;
    const speakersOk = Boolean(s1Id) && Boolean(s2Id);

    if (!scoresOk || !speakersOk) {
      incomplete = true;
      totals.push({ card, sum: null });
      continue;
    }

    totals.push({ card, sum: sc1 + sc2 });
  }

  // se falta algo, mantém desabilitado e sai
  if (incomplete) {
    setBtnDisabled(true);
    return;
  }

  // checa empates
  const sums = totals.map(t => t.sum);
  const hasTie = new Set(sums).size !== sums.length;
  if (hasTie) {
    showAlert('Não podem haver empates de pontuação entre equipes.');
    setBtnDisabled(true);
  } else {
    hideAlert();
    setBtnDisabled(false);
  }

  // pinta ranking 1º→4º
  const ordered = [...totals].sort((a, b) => b.sum - a.sum);
  ordered.forEach((t, idx) => {
    t.card.className = `rounded-xl border border-slate-200 p-4 space-y-2 ${colorForRank(idx)}`;
    const badge = t.card.querySelector('.rank-badge');
    if (badge) badge.textContent = `${idx + 1}º`;
  });
}


async function loadDebates() {
  const rid = roundSel.value;
  const res = await fetch(`/api/round_debates?round_id=${encodeURIComponent(rid)}`);
  const json = await res.json();
  debateSel.innerHTML = '';
  (json.data || []).forEach(d => {
    const label = `Debate ${d.number_in_round}` + (d.completed ? ' — resultados enviados' : '');
    option(debateSel, d.id, label);
  });
  await loadDebateDetail();
}

async function loadDebateDetail() {
  positionsEl.innerHTML = '';
  chairBox.innerHTML = ''; wing1Box.innerHTML = ''; wing2Box.innerHTML = '';

  const did = debateSel.value;
  const res = await fetch(`/api/debate_detail?debate_id=${encodeURIComponent(did)}`);
  const json = await res.json();
  const data = json.data || {};
  const positions = data.positions || [];   // [{position, team_short, edition_society_id}]
  const debaters  = data.debaters  || [];
  const judges    = data.judges    || [];

  // --- Juízes (combobox) ---
  const judgeItems = (judges || []).map(j => ({
    id: j.edition_member_id,
    label: `${j.soc || ''} — ${j.name}`
  }));
  const chairCombo = createCombo(chairBox, judgeItems, 'filtrar juízes…');
  const wing1Combo = createCombo(wing1Box, judgeItems, 'filtrar juízes…');
  const wing2Combo = createCombo(wing2Box, judgeItems, 'filtrar juízes…');
  chairBox._combo = chairCombo;
  wing1Box._combo = wing1Combo;
  wing2Box._combo = wing2Combo;
  chairBox.addEventListener('combo-change', calculatePreviewAndPaint);
  wing1Box.addEventListener('combo-change', calculatePreviewAndPaint);
  wing2Box.addEventListener('combo-change', calculatePreviewAndPaint);

  // --- Cards OG/OO/CG/CO ---
  positions.forEach(p => {
    const card = document.createElement('div');
    card.className = 'rounded-xl border border-slate-200 p-4 space-y-2';
    card.dataset.position = p.position;     // OG/OO/CG/CO
    card.dataset.teamShort = p.team_short || '';

    card.innerHTML = `
      <div class="flex items-center justify-between">
        <div class="text-sky-900 font-semibold">${p.position} • ${p.team_short || ''}</div>
        <div class="rank-badge inline-flex items-center justify-center text-xs font-bold text-slate-700"></div>
      </div>
      <div class="grid md:grid-cols-2 gap-3">
        <div>
          <label class="block text-sm text-slate-700 mb-1">Orador 1</label>
          <div class="deb-s1 cb"></div>
        </div>
        <div>
          <label class="block text-sm text-slate-700 mb-1">Nota 1 (50–100)</label>
          <input type="text" class="deb-s1-score w-full rounded-lg border-slate-300" />
        </div>
        <div>
          <label class="block text-sm text-slate-700 mb-1">Orador 2</label>
          <div class="deb-s2 cb"></div>
        </div>
        <div>
          <label class="block text-sm text-slate-700 mb-1">Nota 2 (50–100)</label>
          <input type="text" class="deb-s2-score w-full rounded-lg border-slate-300" />
        </div>
      </div>
    `;

    const s1Box = card.querySelector('.deb-s1');
    const s2Box = card.querySelector('.deb-s2');
    const teamShort = (p.team_short || '').trim();

    const debItems = (debaters || [])
      .filter(d => (d.soc || '').trim() === teamShort)
      .map(d => ({ id: d.edition_member_id, label: d.name }));

    const s1Combo = createCombo(s1Box, debItems, 'filtrar nomes…');
    const s2Combo = createCombo(s2Box, debItems, 'filtrar nomes…');
    s1Box._combo = s1Combo;
    s2Box._combo = s2Combo;
    s1Box.addEventListener('combo-change', calculatePreviewAndPaint);
    s2Box.addEventListener('combo-change', calculatePreviewAndPaint);

    const sc1 = card.querySelector('.deb-s1-score');
    const sc2 = card.querySelector('.deb-s2-score');
    attachScoreInput(sc1);
    attachScoreInput(sc2);
    sc1.addEventListener('input', calculatePreviewAndPaint);
    sc2.addEventListener('input', calculatePreviewAndPaint);

    positionsEl.appendChild(card);
  });

  calculatePreviewAndPaint();
}

async function saveResults() {
// evita clique duplo
if (saveBtn.disabled) return;
setSaving(true);
try {
  const did = Number(debateSel.value);
  const payload = { debate_id: did, speeches: [], judges: {} };

  // Chair/Wings
  const chair = chairBox._combo ? Number(chairBox._combo.getId()) : null;
  const w1 = wing1Box._combo ? Number(wing1Box._combo.getId()) : null;
  const w2 = wing2Box._combo ? Number(wing2Box._combo.getId()) : null;
  payload.judges = { chair, wings: [w1, w2].filter(Boolean) };

  // OG/OO/CG/CO
  const cards = positionsEl.querySelectorAll('[data-position]');
  for (const card of cards) {
    const pos = card.getAttribute('data-position');
    const s1Box = card.querySelector('.deb-s1');
    const s2Box = card.querySelector('.deb-s2');
    const sc1Inp = card.querySelector('.deb-s1-score');
    const sc2Inp = card.querySelector('.deb-s2-score');

    const s1 = s1Box._combo ? Number(s1Box._combo.getId()) : NaN;
    const s2 = s2Box._combo ? Number(s2Box._combo.getId()) : NaN;
    const sc1 = sc1Inp.value, sc2 = sc2Inp.value;

    if (!s1 || !s2 || !validScore(sc1) || !validScore(sc2)) {
      showAlert(`Preencha corretamente ${pos} (oradores e notas 50–100).`);
      return;
    }

    payload.speeches.push({
      position: pos,
      s1_id: s1, s1_score: Number(sc1),
      s2_id: s2, s2_score: Number(sc2)
    });
  }

  // Revalida: sem empates
  const sums = Array.from(cards).map(card => {
    const sc1 = Number((card.querySelector('.deb-s1-score') || {}).value || '');
    const sc2 = Number((card.querySelector('.deb-s2-score') || {}).value || '');
    return Number(sc1) + Number(sc2);
  });
  if (new Set(sums).size !== sums.length) {
    showAlert('Não podem haver empates de pontuação entre equipes.');
    return;
  }

  const res = await fetch('/api/results', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const json = await res.json();
  if (!res.ok || json.error) {
    showAlert(json.error || 'Erro ao salvar.');
    setSaving(false);
    return;
  }
  window.location.href = '/';
} catch (e) {
  showAlert('Erro ao salvar.');
  setSaving(false);
}
}


roundSel.addEventListener('change', loadDebates);
debateSel.addEventListener('change', loadDebateDetail);
document.getElementById('save').addEventListener('click', saveResults);

// inicial
loadDebateDetail();

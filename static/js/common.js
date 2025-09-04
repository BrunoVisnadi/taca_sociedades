export function showAlert(el, msg) {
  if (!el) return;
  el.textContent = msg;
  el.classList.remove('hidden');
}
export function hideAlert(el) {
  if (!el) return;
  el.textContent = '';
  el.classList.add('hidden');
}

export function setBtnDisabled(btn, disabled) {
  if (!btn) return;
  btn.disabled = !!disabled;
  btn.classList.toggle('pointer-events-none', !!disabled);
}

export function setSaving(btn, isSaving) {
  if (!btn) return;
  if (isSaving) {
    btn.dataset.prevText = btn.textContent;
    btn.innerHTML = `
      <span class="inline-flex items-center gap-2">
        <svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor"
            d="M4 12a8 8 0 018-8v4A4 4 0 004 12z"></path>
        </svg>
        Salvando…
      </span>`;
    btn.disabled = true;
  } else {
    btn.innerHTML = btn.dataset.prevText || 'Salvar resultados';
  }
}

export function colorForRank(rank) {
  // gradiente 1º → 4º (azul → mais claro)
  const shades = [
    'bg-sky-200 ring-2 ring-sky-500',  // 1º
    'bg-sky-100 ring-2 ring-sky-400',  // 2º
    'bg-sky-50  ring-2 ring-sky-300',  // 3º
    'bg-slate-50 ring-2 ring-sky-200'  // 4º
  ];
  return shades[rank] || 'bg-slate-50';
}

export function validScore(v) {
  if (v === '' || v === null || v === undefined) return false;
  const n = Number(v);
  return Number.isInteger(n) && n >= 50 && n <= 100;
}

// Input numérico 50–100 com feedback visual; chama onChange a cada alteração
export function attachScoreInput(inputEl, onChange) {
  if (!inputEl) return;
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
    onChange && onChange();
  });
  inputEl.addEventListener('blur', () => {
    let n = Number(inputEl.value || NaN);
    if (!Number.isInteger(n)) return;
    if (n < 50) n = 50;
    if (n > 100) n = 100;
    inputEl.value = String(n);
    sanitize();
    onChange && onChange();
  });
}

// Combobox pesquisável (input + lista). Dispara 'combo-change' no container.
export function createCombo(containerEl, items, placeholder='— selecione —') {
  if (!containerEl) return null;
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
      li.addEventListener('mousedown', (e) => {
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

  inp.addEventListener('input', filter);
  inp.addEventListener('focus', () => { filter(); open(); });
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { active = Math.min(active + 1, Math.max(view.length - 1, 0)); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active - 1, -1); render(); e.preventDefault(); }
    else if (e.key === 'Enter') { if (active >= 0 && view[active]) selectItem(view[active]); e.preventDefault(); }
    else if (e.key === 'Escape') { close(); }
  });
  inp.addEventListener('blur', () => setTimeout(close, 120));

  return {
    getId: () => hid.value ? String(hid.value) : '',
    setItems: (newItems) => { items = [...newItems]; filter(); },
    setPlaceholder: (ph) => { inp.placeholder = ph; },
  };
}

(() => {
  const board = document.getElementById('pilot-board');
  const cards = [...document.querySelectorAll('.pilot-card')];
  const search = document.getElementById('pilot-search');
  const type = document.getElementById('pilot-type');
  const status = document.getElementById('pilot-status');
  const owner = document.getElementById('pilot-owner');
  const result = document.getElementById('pilot-result-count');
  const empty = document.getElementById('pilot-empty');
  const detail = document.getElementById('pilot-detail');
  const detailBody = document.getElementById('pilot-detail-body');
  const boardButton = document.getElementById('view-board');
  const listButton = document.getElementById('view-list');

  function update() {
    const query = search.value.trim().toLocaleLowerCase('fr');
    let visible = 0;
    cards.forEach((card) => {
      const matches = (!query || card.textContent.toLocaleLowerCase('fr').includes(query))
        && (type.value === 'all' || card.dataset.type === type.value)
        && (status.value === 'all' || card.dataset.status === status.value)
        && (owner.value === 'all' || card.dataset.owner === owner.value);
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    document.querySelectorAll('.pilot-column').forEach((column) => {
      const count = column.querySelectorAll('.pilot-card:not([hidden])').length;
      column.querySelector('.pilot-count').textContent = String(count);
      column.hidden = count === 0;
    });
    result.textContent = `${visible} activité${visible > 1 ? 's' : ''} affichée${visible > 1 ? 's' : ''}`;
    empty.hidden = visible !== 0;
    const selected = document.querySelector('.pilot-card[aria-pressed="true"]');
    if (selected?.hidden) {
      selected.setAttribute('aria-pressed', 'false');
      detailBody.innerHTML = '<p>La sélection est masquée par les filtres. Choisissez une carte visible.</p>';
    }
  }

  function clearFilters() {
    search.value = '';
    type.value = 'all';
    status.value = 'all';
    owner.value = 'all';
    update();
    search.focus();
  }

  for (const control of [search, type, status, owner]) {
    control.addEventListener(control === search ? 'input' : 'change', update);
  }
  document.getElementById('clear-filters').addEventListener('click', clearFilters);
  document.getElementById('clear-empty').addEventListener('click', clearFilters);
  document.querySelectorAll('[data-quick-status]').forEach((button) => {
    button.addEventListener('click', () => {
      status.value = button.dataset.quickStatus;
      update();
      board.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  });

  cards.forEach((card) => {
    card.setAttribute('aria-pressed', 'false');
    card.addEventListener('click', () => {
      const template = document.getElementById(card.dataset.detail);
      if (!template) return;
      cards.forEach((other) => other.setAttribute('aria-pressed', 'false'));
      card.setAttribute('aria-pressed', 'true');
      detailBody.replaceChildren(template.content.cloneNode(true));
      detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      detail.focus({ preventScroll: true });
    });
  });

  function setLayout(mode) {
    board.classList.toggle('list-mode', mode === 'list');
    boardButton.setAttribute('aria-pressed', String(mode === 'board'));
    listButton.setAttribute('aria-pressed', String(mode === 'list'));
  }
  boardButton.addEventListener('click', () => setLayout('board'));
  listButton.addEventListener('click', () => setLayout('list'));
  update();
})();

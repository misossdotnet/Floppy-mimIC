(() => {
  const cards = [...document.querySelectorAll('.agent-card')];
  const search = document.getElementById('agent-search');
  const owner = document.getElementById('agent-owner');
  const board = document.getElementById('agent-board-grid');
  const count = document.getElementById('agent-count');
  const empty = document.getElementById('agent-empty');
  const detail = document.getElementById('agent-detail');
  const detailBody = document.getElementById('agent-detail-body');
  const boardButton = document.getElementById('agent-view-board');
  const listButton = document.getElementById('agent-view-list');

  function update() {
    const term = search.value.trim().toLocaleLowerCase('fr');
    let visible = 0;
    cards.forEach((card) => {
      const matches = (!term || card.textContent.toLocaleLowerCase('fr').includes(term))
        && (owner.value === 'all' || card.dataset.owner === owner.value);
      card.hidden = !matches;
      if (matches) visible++;
    });
    document.querySelectorAll('.agent-board .pilot-column').forEach((column) => {
      const number = column.querySelectorAll('.agent-card:not([hidden])').length;
      column.querySelector('.pilot-count').textContent = String(number);
      column.hidden = number === 0;
    });
    count.textContent = `${visible} tâche${visible > 1 ? 's' : ''} affichée${visible > 1 ? 's' : ''}`;
    empty.hidden = visible !== 0;
  }

  function openCard(card) {
    const template = document.getElementById(card.dataset.detail);
    if (!template) return;
    cards.forEach((item) => item.setAttribute('aria-pressed', 'false'));
    card.setAttribute('aria-pressed', 'true');
    detailBody.replaceChildren(template.content.cloneNode(true));
    detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    detail.focus({ preventScroll: true });
  }

  cards.forEach((card) => {
    card.setAttribute('aria-pressed', 'false');
    card.addEventListener('click', () => openCard(card));
  });
  search.addEventListener('input', update);
  owner.addEventListener('change', update);
  document.getElementById('agent-clear').addEventListener('click', () => {
    search.value = '';
    owner.value = 'all';
    update();
    search.focus();
  });
  document.querySelectorAll('[data-open-task]').forEach((link) => {
    link.addEventListener('click', (event) => {
      const card = cards.find((item) => item.dataset.detail === link.dataset.openTask);
      if (!card) return;
      event.preventDefault();
      search.value = '';
      owner.value = 'all';
      update();
      openCard(card);
    });
  });

  function setLayout(list) {
    board.classList.toggle('list-mode', list);
    boardButton.setAttribute('aria-pressed', String(!list));
    listButton.setAttribute('aria-pressed', String(list));
  }
  boardButton.addEventListener('click', () => setLayout(false));
  listButton.addEventListener('click', () => setLayout(true));

  const diffButton = document.getElementById('show-plan-diff');
  const diff = document.getElementById('plan-diff');
  diffButton.addEventListener('click', () => {
    diff.hidden = !diff.hidden;
    diffButton.setAttribute('aria-expanded', String(!diff.hidden));
    diffButton.textContent = diff.hidden ? 'Voir le diff' : 'Masquer le diff';
  });
  update();
})();

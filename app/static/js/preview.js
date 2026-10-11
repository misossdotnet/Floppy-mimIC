document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form[method="post"]').forEach((form) => {
    form.addEventListener('submit', (event) => event.preventDefault());
    form.querySelectorAll('button[type="submit"]').forEach((button) => {
      button.disabled = true;
      button.title = 'Action indisponible dans l’aperçu des templates';
    });
  });
});

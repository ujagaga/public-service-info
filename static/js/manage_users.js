document.addEventListener('DOMContentLoaded', () => {
  const dialog = document.getElementById('addressModal');
  document.querySelectorAll('.edit-address-link').forEach(button => {
    button.addEventListener('click', () => {
      document.getElementById('modal-email').value = button.dataset.email;
      document.getElementById('modal-address').value = button.dataset.address;
      document.getElementById('modal-address').dispatchEvent(new Event('input', { bubbles: true }));
      document.getElementById('editing-user').textContent = button.dataset.email;
      dialog.showModal();
      document.getElementById('modal-address').focus();
    });
  });
  dialog.querySelectorAll('.close-dialog').forEach(button => {
    button.addEventListener('click', () => dialog.close());
  });
  dialog.addEventListener('close', () => {
    document.getElementById('modal-address').dispatchEvent(new Event('input', { bubbles: true }));
  });
  dialog.addEventListener('click', event => {
    const bounds = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close();
  });
});

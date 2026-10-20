document.addEventListener('DOMContentLoaded', () => {
  const dialog = document.getElementById('mapDialog');
  const frame = dialog.querySelector('iframe');
  document.querySelectorAll('.map-button').forEach(button => {
    button.addEventListener('click', () => {
      const address = window.addressMapQuery(button.dataset.mapAddress, dialog.dataset.city);
      const params = new URLSearchParams({ q: address, output: 'embed', hl: 'sr' });
      frame.src = `https://maps.google.com/maps?${params}`;
      frame.title = `Google mapa: ${address}`;
      document.getElementById('map-dialog-title').textContent = address;
      dialog.showModal();
    });
  });
  document.querySelectorAll('.address-card').forEach(form => {
    const emails = form.querySelector('textarea');
    const save = form.querySelector('[data-save]');
    const update = () => { save.disabled = emails.value === emails.defaultValue; };
    emails.addEventListener('input', update);
    update();
  });
  const logo = document.querySelector('.logo-input');
  logo.addEventListener('change', async () => {
    const file = logo.files[0];
    if (!file) return;
    try {
      // Fit within 400x400 before upload; never upscale. PNG stays PNG to keep transparency.
      const image = await createImageBitmap(file);
      const scale = Math.min(1, 400 / image.width, 400 / image.height);
      if (scale < 1) {
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(image.width * scale);
        canvas.height = Math.round(image.height * scale);
        canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
        const type = file.type === 'image/png' ? 'image/png' : 'image/jpeg';
        const blob = await new Promise(resolve => canvas.toBlob(resolve, type, 0.9));
        const files = new DataTransfer();
        files.items.add(new File([blob], file.name, { type }));
        logo.files = files.files;
      }
    } catch {
      // Not decodable here; the server rejects anything that is not PNG/JPEG.
    }
    logo.form.submit();
  });
  dialog.querySelector('.close-dialog').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => frame.removeAttribute('src'));
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });
});

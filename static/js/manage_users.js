document.addEventListener('DOMContentLoaded', function () {

  // ---------- Image Modal ----------
  const imgModal = document.getElementById('imgModal');
  const modalImage = document.getElementById('modalImage');
  const imgClose = imgModal.querySelector('.close');

  document.querySelectorAll('.user_picture').forEach(img => {
    img.addEventListener('click', function() {
      modalImage.src = this.src;
      imgModal.style.display = 'flex';
    });
  });

  imgClose.addEventListener('click', () => {
    imgModal.style.display = 'none';
  });

  window.addEventListener('click', e => {
    if (e.target === imgModal) {
      imgModal.style.display = 'none';
    }
  });

  // ---------- Address Modal ----------
  const addressModal = document.getElementById('addressModal');
  const modalEmailInput = document.getElementById('modal-email');
  const modalAddressInput = document.getElementById('modal-address');

  document.querySelectorAll('.edit-address-link').forEach(link => {
    link.addEventListener('click', function(e) {
      e.preventDefault();
      const email = this.dataset.email;
      const address = this.dataset.address;
      modalEmailInput.value = email;
      modalAddressInput.value = address;
      addressModal.style.display = 'flex';
    });
  });

  window.addEventListener('click', e => {
    if (e.target === addressModal) {
      addressModal.style.display = 'none';
    }
  });

});

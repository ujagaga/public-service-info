(() => {
  'use strict';

  function initPicker(root) {
    const input = root.querySelector('[data-address-input]');
    const city = root.querySelector('[data-address-city]').textContent.trim();
    const search = root.querySelector('[data-map-search]');
    const status = root.querySelector('[data-map-status]');
    const results = root.querySelector('[data-map-results]');
    const frame = root.querySelector('[data-map-frame]');
    const caption = root.querySelector('[data-map-address]');

    function mapQuery() {
      const address = input.value.trim();
      if (!address) return city;
      // Previously saved addresses can already include the city in either script.
      const normalize = value => value.toLocaleLowerCase('sr').replace(/\s+/g, ' ').trim();
      const cityNames = [normalize(city)];
      if (cityNames[0] === 'novi sad') cityNames.push('нови сад');
      if (cityNames[0] === 'нови сад') cityNames.push('novi sad');
      const hasCity = address.split(',').some(part =>
        cityNames.includes(normalize(part).replace(/^\d{5}\s+/, '')));
      return hasCity ? address : `${address}, ${city}`;
    }

    function reset() {
      // Also runs when the admin editor switches between users or closes.
      results.hidden = true;
      frame.removeAttribute('src');
      caption.textContent = '';
      status.textContent = '';
      delete status.dataset.state;
    }
    input.addEventListener('input', reset);

    search.addEventListener('click', () => {
      if (!input.value.trim()) {
        input.focus();
        status.textContent = 'Najpre unesite ulicu i kućni broj.';
        status.dataset.state = 'error';
        return;
      }
      const address = mapQuery();
      const params = new URLSearchParams({ q: address, output: 'embed', hl: 'sr' });
      frame.src = `https://maps.google.com/maps?${params}`;
      frame.title = `Google mapa: ${address}`;
      caption.textContent = address;
      results.hidden = false;
      delete status.dataset.state;
      // Cross-origin map content cannot confirm an address or report match quality.
      status.textContent = 'Proverite prikazanu lokaciju. Adresu možete sačuvati i ako se mapa ne prikaže.';
    });

    search.hidden = false;
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-address-picker]').forEach(initPicker);
  });
})();

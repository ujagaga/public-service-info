/* Offline browser tests: intercept iframe navigation instead of contacting Google. */
(() => {
  const requests = [];
  const src = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'src');
  Object.defineProperty(HTMLIFrameElement.prototype, 'src', {
    ...src,
    set(value) {
      requests.push({ frame: this, url: new URL(value) });
      src.set.call(this, 'about:blank');
    },
  });

  function assert(condition, description) {
    if (!condition) throw new Error(description);
  }

  window.addEventListener('load', () => {
    const output = document.getElementById('test-output');
    try {
      const [root, other] = document.querySelectorAll('[data-address-picker]');
      const get = attribute => root.querySelector(`[${attribute}]`);
      const input = get('data-address-input');
      const button = get('data-map-search');
      const city = get('data-address-city');
      assert(city.options.length === 1 && city.value === 'Novi Sad', 'City dropdown must contain only Novi Sad');
      const frame = get('data-map-frame');
      const status = get('data-map-status');
      let submissions = 0;
      root.closest('form').addEventListener('submit', event => {
        submissions += 1;
        event.preventDefault();
      });
      const edit = value => {
        input.value = value;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      };

      assert(requests.length === 0, 'No Google navigation on page view');
      assert(!frame.hasAttribute('src'), 'Initial iframe must not load an address');
      assert(!button.hidden, 'Preview button must be available without a key');
      assert(!root.querySelector('[data-map-link]'), 'External Google Maps link must be removed');
      edit('   ');
      button.click();
      assert(requests.length === 0, 'Blank input must not navigate');
      assert(status.textContent.includes('Najpre'), 'Blank input needs an explanation');

      const address = '  Futoška 12 & 14 #2 + "A" <test>  ';
      edit(address);
      button.click();
      assert(requests.length === 1, 'Click should navigate the iframe once');
      let url = requests[0].url;
      assert(url.origin === 'https://maps.google.com' && url.pathname === '/maps', 'Use the keyless Google endpoint');
      assert(url.searchParams.get('q') === `${address.trim()}, Novi Sad`, 'Encode punctuation and Serbian letters exactly');
      assert(url.searchParams.get('output') === 'embed', 'Request an embedded map');
      assert(!url.searchParams.has('key'), 'No API key should be sent');
      assert(!get('data-map-results').hidden, 'Preview should be visible');
      assert(input.value === address, 'Preview must not overwrite submitted address');
      assert(get('data-map-address').textContent === `${address.trim()}, Novi Sad`, 'Caption should show query as text');
      assert(!get('data-map-address').querySelector('test'), 'Address must never be inserted as HTML');
      assert(frame.title.includes(address.trim()), 'Iframe needs an accessible descriptive title');
      assert(submissions === 0, 'Preview must not submit the form');

      edit('Булевар ослобођења 24, Нови Сад');
      assert(get('data-map-results').hidden, 'Editing must hide the stale preview');
      assert(!frame.hasAttribute('src'), 'Editing must clear the stale iframe URL');
      button.click();
      assert(requests.length === 2 && requests[1].url.searchParams.get('q') === input.value, 'Second preview must use the new address');

      edit('Test 1&output=bad&key=injected');
      button.click();
      url = requests[2].url;
      assert(url.searchParams.get('q') === `${input.value}, Novi Sad`, 'Query must preserve reserved characters');
      assert(url.searchParams.get('output') === 'embed' && !url.searchParams.has('key'), 'Address cannot inject URL parameters');

      const otherInput = other.querySelector('[data-address-input]');
      const otherFrame = other.querySelector('[data-map-frame]');
      otherInput.value = 'Another user 42';
      otherInput.dispatchEvent(new Event('input', { bubbles: true }));
      other.querySelector('[data-map-search]').click();
      assert(requests.at(-1).url.searchParams.get('q') === 'Another user 42, Novi Sad', 'Admin-style address must include selected city');
      assert(requests.at(-1).frame === otherFrame, 'Independent forms must update their own iframe');
      assert(get('data-map-address').textContent === `${input.value}, Novi Sad`, 'Second form must not alter the first preview');
      otherInput.value = 'Next user 56';
      otherInput.dispatchEvent(new Event('input', { bubbles: true }));
      assert(other.querySelector('[data-map-results]').hidden && !otherFrame.hasAttribute('src'), 'Switching admin users must clear the prior map');
      assert(!document.querySelector('script[src*="maps.googleapis.com"]'), 'Google SDK must not be loaded');

      output.textContent = 'PASS: Novi Sad dropdown, city appended to map query, keyless iframe, encoded queries, updates, empty input, stale-map clearing, independent forms, and manual address preservation';
      document.body.dataset.testResult = 'pass';
    } catch (error) {
      output.textContent = `FAIL: ${error.stack || error}`;
      document.body.dataset.testResult = 'fail';
    }
  });
})();

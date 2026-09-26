"""Create an offline HTML harness to open in a browser; no real Google calls."""
from pathlib import Path
import sys

import test_regressions  # Isolated test settings and database, not local credentials.
from flask import render_template_string

root = Path(__file__).resolve().parents[1]
with test_regressions.index.application.test_request_context('/'):
    picker = render_template_string('''
      {% from "address_picker.html" import address_picker with context %}
      {{ address_picker(field_id='test-address') }}
    ''')
    fallback = render_template_string('''
      {% from "address_picker.html" import address_picker with context %}
      {{ address_picker(field_id='fallback-address') }}
    ''')
html = f'''<!doctype html><html lang="sr-Latn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="{(root / 'static/css/base.css').as_uri()}">
<link rel="stylesheet" href="{(root / 'static/css/address_picker.css').as_uri()}">
</head><body><main class="container"><h1>Address picker browser tests</h1>
<pre id="test-output">RUNNING</pre><form class="panel">{picker}</form>
<form class="panel">{fallback}</form></main>
<script src="{(root / 'tests/address_picker_browser.js').as_uri()}"></script>
<script src="{(root / 'static/js/address_picker.js').as_uri()}"></script>
</body></html>'''
path = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/psi-address-picker-test.html')
path.write_text(html)
print(path)

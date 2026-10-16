"""Rebuild the portable, single-file sports prototype from shared sources."""
from pathlib import Path

root = Path(__file__).resolve().parent
html = (root / 'index.html').read_text()
html = html.replace('data-page="overview"', 'data-page="overview" data-standalone="true"')
html = html.replace('<link rel="stylesheet" href="styles.css">', '<style>\n' + (root / 'styles.css').read_text() + '\n</style>')
html = html.replace('<script src="app.js" defer></script>', '<script>\n' + (root / 'app.js').read_text() + '\n</script>')
(root / 'preview.html').write_text(html)

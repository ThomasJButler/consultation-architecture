#!/usr/bin/env bash
# Builds the submission: two SVGs from docs/diagrams/, one PDF from
# submission/index.html. Run it from anywhere; paths resolve from this file.
#
# The PDF is printed by the Chromium that mermaid-cli's puppeteer already
# downloads, so rendering the diagrams and printing the page share one browser
# and nothing extra gets installed. plans/PR-01 §7 names md-to-pdf as the
# fallback for the day that path breaks; it's the last step here.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$root/submission"
pdf="$out/Thomas_Butler_Consultation_Architecture.pdf"

# ---------- 1. Diagrams ----------
# Every .mmd renders to an SVG of the same name next to index.html. SVG, because
# the PDF has to keep its diagram text at 8 pt or more at A4 (plans/PR-01 §3)
# and a bitmap loses that the moment it's scaled. White background, because the
# SVG sits inside an <img> on a printed page and a transparent one would show
# whatever the viewer paints behind it.
for src in "$root"/docs/diagrams/*.mmd; do
  name="$(basename "${src%.mmd}")"
  echo "render: $name"
  npx -y @mermaid-js/mermaid-cli -i "$src" -o "$out/$name.svg" -b white
done

# ---------- 2. Find the browser mermaid-cli just used ----------
# puppeteer caches Chrome for Testing under
# ~/.cache/puppeteer/chrome/<platform>-<version>/. The platform tag comes from
# node, not uname: an x64 node under Rosetta downloads and runs the x64 build,
# and the cache on a Mac is often mixed. Prefer that platform's newest version;
# fall back to any build present.
tag="$(node -p "process.platform === 'darwin' ? (process.arch === 'arm64' ? 'mac_arm' : 'mac') : 'linux'")"
cache="${PUPPETEER_CACHE_DIR:-$HOME/.cache/puppeteer}/chrome"

find_chrome() {
  # $1 is a path fragment to insist on. `|| true` because an empty match is a
  # normal outcome here, not a failure, and pipefail would otherwise abort.
  find "$cache" -type f \( -name 'Google Chrome for Testing' -o -name chrome \) 2>/dev/null \
    | grep -F "$1" | sort -V | tail -n 1 || true
}
chrome="$(find_chrome "/chrome/$tag-")"
[ -n "$chrome" ] || chrome="$(find_chrome "/chrome/")"

# ---------- 3. Find puppeteer-core ----------
# mermaid-cli depends on puppeteer, which npx unpacks into its cache together
# with puppeteer-core. NODE_PATH lets the inline script require it from there,
# which keeps a package.json out of a repo that has no JavaScript of its own.
node_modules="$(find "$HOME/.npm/_npx" -maxdepth 3 -type d -name '@mermaid-js' 2>/dev/null | head -n 1 || true)"
node_modules="${node_modules%/@mermaid-js}"
if [ ! -d "$node_modules/puppeteer-core" ]; then
  node_modules="$(find "$HOME/.npm/_npx" -maxdepth 3 -type d -name puppeteer-core 2>/dev/null | head -n 1 || true)"
  node_modules="${node_modules%/puppeteer-core}"
fi

# ---------- 4. Print ----------
# preferCSSPageSize: index.html sets the 18 mm margins and marks the diagram
# sheets landscape with named @page rules; without this flag Chromium prints
# every sheet as plain portrait A4 and ignores both.
print_pdf() {
  CHROME="$chrome" HTML="$out/index.html" PDF="$pdf" NODE_PATH="$node_modules" node - <<'JS'
const puppeteer = require('puppeteer-core');
const { pathToFileURL } = require('node:url');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME,
    headless: true,
  });
  try {
    const page = await browser.newPage();
    // networkidle0 so both SVGs have loaded before the print, not just the HTML.
    await page.goto(pathToFileURL(process.env.HTML).href, { waitUntil: 'networkidle0' });
    await page.pdf({
      path: process.env.PDF,
      format: 'A4',
      printBackground: true,
      preferCSSPageSize: true,
    });
  } finally {
    await browser.close();
  }
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
JS
}

if [ -z "$chrome" ] || [ ! -d "$node_modules/puppeteer-core" ]; then
  # Fallback (plans/PR-01 §7), and only when the browser or puppeteer-core is
  # genuinely missing. A print that fails for any other reason stops the build
  # (set -e), because the fallback prints a different source document and
  # nobody should get that by accident. md-to-pdf resolves SUBMISSION.md's
  # image paths against the working directory, hence the cd.
  echo "print: chromium or puppeteer-core not found, falling back to md-to-pdf" >&2
  (cd "$root" && npx -y md-to-pdf SUBMISSION.md)
  mv "$root/SUBMISSION.pdf" "$pdf"
else
  print_pdf
  echo "print: chromium ($chrome)"
fi

# ---------- 5. Metadata ----------
# Chromium writes no Author and stamps the file with its own user agent, host
# OS included. The submission should carry my name and the title and nothing
# about the machine it was printed on. pypdf lives in a gitignored venv so the
# repo stays free of a Python project until the proof-of-concept brings one.
venv="$root/.build-venv"
[ -x "$venv/bin/python" ] || python3 -m venv "$venv"
"$venv/bin/python" -c 'import pypdf' 2>/dev/null || "$venv/bin/pip" -q install 'pypdf>=5,<7'
PDF="$pdf" "$venv/bin/python" - <<'PY'
import os
from pypdf import PdfReader, PdfWriter
path = os.environ["PDF"]
reader = PdfReader(path)
writer = PdfWriter()
writer.append(reader)
writer.add_metadata({
    "/Title": "Consultation analysis: a production architecture",
    "/Author": "Thomas Butler",
    "/Creator": "",
    "/Producer": "",
})
with open(path, "wb") as f:
    writer.write(f)
PY

echo "wrote: $pdf ($(du -h "$pdf" | cut -f1))"

"""Sammelt alle übersetzbaren Texte (deutsch) aus Templates und Python-Code.

Aufruf: python tools/i18n_extract.py [--update en]
Ohne Argument: listet fehlende Übersetzungen. Mit --update ergänzt fehlende Schlüssel (leer) und entfernt unbenutzte.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "stempeln"
STRING = r"""(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
CALLS = re.compile(r"(?<![\w.])(?:_|N_|i18n\.N_|gettext|i18n\.gettext|t|self\.t)\(\s*" + STRING)


def unescape(text):
    return text.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")


def extract():
    keys = set()
    for path in list(ROOT.rglob("*.py")) + list((ROOT / "templates").glob("*.html")):
        source = path.read_text(encoding="utf-8")
        for match in CALLS.finditer(source):
            text = unescape(match.group(1) if match.group(1) is not None else match.group(2))
            if text.strip():
                keys.add(text)
    return keys


def main():
    keys = extract()
    language = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--update" else "en"
    path = ROOT / "translations" / f"{language}.json"
    catalog = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    missing = sorted(key for key in keys if not catalog.get(key))
    unused = sorted(key for key in catalog if key not in keys)
    if len(sys.argv) > 1 and sys.argv[1] == "--update":
        updated = {key: catalog.get(key, "") for key in sorted(keys, key=str.casefold)}
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(keys)} Texte, {len(missing)} ohne Übersetzung, {len(unused)} unbenutzt")
    for key in missing:
        print("  fehlt:", key)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())

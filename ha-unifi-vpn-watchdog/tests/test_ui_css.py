"""The UI is one file; a class name reused by two screens silently restyles the other one."""
import re
from collections import defaultdict
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "src" / "vpn_watchdog" / "ui" / "index.html"


def _css() -> str:
    css = re.search(r"<style>(.*?)</style>", HTML.read_text(), re.S).group(1)
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, depth = [], 0                      # drop @media blocks: overrides there are intentional
    for i, ch in enumerate(css):
        if ch == "{" and re.search(r"@[\w-]+[^{}]*$", css[max(0, i - 200):i]) and depth == 0:
            depth = 1
            continue
        if depth:
            depth += ch == "{"
            depth -= ch == "}"
            continue
        out.append(ch)
    return "".join(out)


def test_no_class_is_defined_twice():
    seen = defaultdict(int)
    for m in re.finditer(r"([^{}]+)\{", _css()):
        for sel in m.group(1).split(","):
            bare = re.fullmatch(r"\s*\.([\w-]+)\s*", sel)
            if bare:
                seen[bare.group(1)] += 1
    dupes = sorted(k for k, n in seen.items() if n > 1)
    assert not dupes, f"CSS classes defined more than once (one screen overrides the other): {dupes}"

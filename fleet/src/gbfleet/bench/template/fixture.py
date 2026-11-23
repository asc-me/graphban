"""Builds the template suite's fixture repo, deterministically, at run time.

The repo is generated rather than bundled as a binary: the same code always yields the same
commit shas (fixed author, dates and messages), so no run ever needs the user's code.

Layout: one `root` commit holds six stub build modules (`pkg/m1.py`..`pkg/m6.py`, each raising
NotImplementedError) and five finished review modules with passing tests (`pkg/r1.py`..`r5.py`).
Refs (all tags): `mN-ref` implements stub N and adds its test; `rN-defect` plants a bug in
review module N; `rN-clean` makes a behaviour-preserving edit to it. `root` is every base.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT_REF = "root"
BUILD_COUNT = 6
REVIEW_COUNT = 5

_STUB = '"""{doc}"""\n\n\ndef {sig}:\n    raise NotImplementedError\n'

#: name, signature, doc, implementation body, test body
BUILD = {
    1: ("slugify", "slugify(text)", "Lowercase `text`, turning each run of non-alphanumerics into one '-'.",
        "import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n",
        "self.assertEqual(slugify('Hello, World!'), 'hello-world')\n"
        "        self.assertEqual(slugify('  a  b  '), 'a-b')\n        self.assertEqual(slugify('!!'), '')\n"),
    2: ("clamp", "clamp(x, lo, hi)", "Limit `x` to the closed range [lo, hi].",
        "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n",
        "self.assertEqual(clamp(5, 0, 3), 3)\n        self.assertEqual(clamp(-1, 0, 3), 0)\n"
        "        self.assertEqual(clamp(2, 0, 3), 2)\n"),
    3: ("is_palindrome", "is_palindrome(s)", "True when `s` reads the same reversed, ignoring case and punctuation.",
        "def is_palindrome(s):\n    t = [c.lower() for c in s if c.isalnum()]\n    return t == t[::-1]\n",
        "self.assertTrue(is_palindrome('A man, a plan, a canal: Panama'))\n"
        "        self.assertFalse(is_palindrome('abc'))\n        self.assertTrue(is_palindrome(''))\n"),
    4: ("chunk", "chunk(xs, n)", "Split `xs` into lists of at most `n`; ValueError when n < 1.",
        "def chunk(xs, n):\n    if n < 1:\n        raise ValueError('n must be >= 1')\n"
        "    return [list(xs[i:i + n]) for i in range(0, len(xs), n)]\n",
        "self.assertEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])\n"
        "        self.assertEqual(chunk([], 3), [])\n        with self.assertRaises(ValueError):\n"
        "            chunk([1], 0)\n"),
    5: ("word_count", "word_count(s)", "Map each lowercased whitespace-separated word of `s` to its count.",
        "def word_count(s):\n    out = {}\n    for w in s.lower().split():\n        out[w] = out.get(w, 0) + 1\n"
        "    return out\n",
        "self.assertEqual(word_count('a B a'), {'a': 2, 'b': 1})\n        self.assertEqual(word_count(''), {})\n"),
    6: ("roman", "roman(n)", "Roman numeral for 1..3999; ValueError outside that range.",
        "def roman(n):\n    if not 1 <= n <= 3999:\n        raise ValueError('out of range')\n"
        "    out = ''\n    for v, s in ((1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'), (90, 'XC'),\n"
        "                 (50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')):\n"
        "        while n >= v:\n            out += s\n            n -= v\n    return out\n",
        "self.assertEqual(roman(4), 'IV')\n        self.assertEqual(roman(1994), 'MCMXCIV')\n"
        "        with self.assertRaises(ValueError):\n            roman(0)\n"),
}

#: name, good source, defective source, clean (behaviour-preserving) source, test body
REVIEW = {
    1: ("take_last", "def take_last(xs, n):\n    return xs[-n:] if n > 0 else []\n",
        "def take_last(xs, n):\n    return xs[-n + 1:] if n > 0 else []\n",
        "def take_last(xs, n):\n    \"\"\"The last `n` items of `xs`.\"\"\"\n    return xs[-n:] if n > 0 else []\n",
        "self.assertEqual(take_last([1, 2, 3], 2), [2, 3])\n        self.assertEqual(take_last([1, 2, 3], 1), [3])\n"
        "        self.assertEqual(take_last([1, 2], 0), [])\n"),
    2: ("average", "def average(xs):\n    return sum(xs) / len(xs) if xs else 0.0\n",
        "def average(xs):\n    return sum(xs) // len(xs) if xs else 0.0\n",
        "def average(xs):\n    if not xs:\n        return 0.0\n    return sum(xs) / len(xs)\n",
        "self.assertEqual(average([1, 2]), 1.5)\n        self.assertEqual(average([]), 0.0)\n"),
    3: ("is_leap", "def is_leap(y):\n    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)\n",
        "def is_leap(y):\n    return y % 4 == 0 and y % 100 != 0\n",
        "def is_leap(y):\n    if y % 400 == 0:\n        return True\n    return y % 4 == 0 and y % 100 != 0\n",
        "self.assertTrue(is_leap(2000))\n        self.assertFalse(is_leap(1900))\n"
        "        self.assertTrue(is_leap(2024))\n        self.assertFalse(is_leap(2023))\n"),
    4: ("dedupe", "def dedupe(xs):\n    seen = set()\n    return [x for x in xs if not (x in seen or seen.add(x))]\n",
        "def dedupe(xs):\n    return list(dict.fromkeys(reversed(xs)))[::-1]\n",
        "def dedupe(xs):\n    seen = set()\n    out = []\n    for x in xs:\n        if x not in seen:\n"
        "            seen.add(x)\n            out.append(x)\n    return out\n",
        "self.assertEqual(dedupe(['b', 'a', 'b', 'c', 'a']), ['b', 'a', 'c'])\n"
        "        self.assertEqual(dedupe([]), [])\n"),
    5: ("title_case", "def title_case(s):\n    return ' '.join(w.capitalize() for w in s.split(' '))\n",
        "def title_case(s):\n    return s.title()\n",
        "def title_case(s):\n    words = s.split(' ')\n    return ' '.join(w.capitalize() for w in words)\n",
        "self.assertEqual(title_case(\"don't stop\"), \"Don't Stop\")\n        self.assertEqual(title_case('a b'), 'A B')\n"),
}


def _test_file(mod: str, fn: str, body: str) -> str:
    return (f"import unittest\n\nfrom pkg.{mod} import {fn}\n\n\nclass Test(unittest.TestCase):\n"
            f"    def test_{fn}(self):\n        {body}\n\nif __name__ == '__main__':\n    unittest.main()\n")


_ENV = {
    "GIT_AUTHOR_NAME": "gbfleet bench", "GIT_AUTHOR_EMAIL": "bench@example.invalid",
    "GIT_COMMITTER_NAME": "gbfleet bench", "GIT_COMMITTER_EMAIL": "bench@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(repo: Path, *args: str) -> None:
    done = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True,
                          env={**os.environ, **_ENV})
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()}")


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str, tag: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    _git(repo, "tag", tag)


def build(dest: str | Path) -> Path:
    """Create the fixture repo at `dest` (which must not already hold one) and return it."""
    repo = Path(dest)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "tests/__init__.py", "")
    for n, (name, sig, doc, _impl, _test) in BUILD.items():
        _write(repo, f"pkg/m{n}.py", _STUB.format(doc=doc, sig=sig))
    for n, (name, good, _bad, _clean, test) in REVIEW.items():
        _write(repo, f"pkg/r{n}.py", good)
        _write(repo, f"tests/test_r{n}.py", _test_file(f"r{n}", name, test))
    _commit(repo, "fixture root", ROOT_REF)
    for n, (name, _sig, _doc, impl, test) in BUILD.items():
        _git(repo, "checkout", "-q", "--detach", ROOT_REF)
        _write(repo, f"pkg/m{n}.py", impl)
        _write(repo, f"tests/test_m{n}.py", _test_file(f"m{n}", name, test))
        _commit(repo, f"implement {name}", f"m{n}-ref")
    for n, (_name, _good, bad, clean, _test) in REVIEW.items():
        for kind, src in (("defect", bad), ("clean", clean)):
            _git(repo, "checkout", "-q", "--detach", ROOT_REF)
            _write(repo, f"pkg/r{n}.py", src)
            _commit(repo, f"edit r{n} ({kind})", f"r{n}-{kind}")
    _git(repo, "checkout", "-q", "main")
    return repo

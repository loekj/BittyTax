# -*- coding: utf-8 -*-
"""Ex-ante global-path regression net for the venue-scoped matching work.

`match_header(..., venue=None)` MUST behave exactly as it did before venue scoping was added.
This builds a deterministic corpus from the declared header of every registered parser and freezes
the matcher decision (which parser variant it selects) into ``global_match_golden.json``. After any
change to the matching code, the decisions must be byte-for-byte identical.

Regenerate the golden (only when an intentional, reviewed change to global matching is made):

    python tests/parsers/test_global_regression.py --write
"""

import json
import os
from typing import List, Optional, Tuple

from bittytax.conv import parsers as _parsers  # noqa: F401  (registers every parser)
from bittytax.conv.dataparser import DataParser

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "global_match_golden.json")


def build_corpus() -> List[List[str]]:
    """One representative header per registered parser: keep literal column names, blank out
    callables/None. Deterministic given import order, so the corpus is stable across runs."""
    corpus = []
    for parser in DataParser.parsers:
        corpus.append([col if isinstance(col, str) else "" for col in parser.header])
    return corpus


def _decide(header: List[str]) -> Tuple[Optional[str], int]:
    """Return (matched parser name, its identity index in the registry) for venue=None, or
    (None, -1) if unrecognised. Identity index pins the exact *variant*, not just the name."""
    try:
        matched = DataParser.match_header(list(header), 0)
    except KeyError:
        return None, -1
    idx = next((i for i, p in enumerate(DataParser.parsers) if p is matched), -1)
    return matched.name, idx


def _current_decisions() -> List[dict]:
    return [
        {"header": header, "name": name, "idx": idx}
        for header in build_corpus()
        for name, idx in [_decide(header)]
    ]


def test_global_matching_unchanged() -> None:
    assert os.path.exists(
        GOLDEN_PATH
    ), "run `python tests/parsers/test_global_regression.py --write` first"
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)

    current = _current_decisions()
    assert len(current) == len(golden), (
        f"corpus size changed: {len(golden)} -> {len(current)} "
        "(parsers added/removed is fine — regenerate the golden after reviewing)"
    )

    mismatches = [
        {"header": g["header"], "was": (g["name"], g["idx"]), "now": (c["name"], c["idx"])}
        for g, c in zip(golden, current)
        if (g["name"], g["idx"]) != (c["name"], c["idx"])
    ]
    assert (
        not mismatches
    ), f"global match decision changed for {len(mismatches)} header(s): {mismatches[:5]}"


def _write_golden() -> None:
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(_current_decisions(), f, indent=1, ensure_ascii=False)
    print(f"wrote {GOLDEN_PATH} ({len(DataParser.parsers)} parsers)")


if __name__ == "__main__":
    import sys

    if "--write" in sys.argv:
        _write_golden()
    else:
        print("pass --write to (re)generate the golden")

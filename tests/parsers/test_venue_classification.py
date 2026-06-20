# -*- coding: utf-8 -*-
"""Guard: every parser that reads cells BY POSITION must be classified as a positional venue.

Venue-scoped matching only does name-based scoring for parsers that read columns by name. A parser
that reads ``data_row.row[N]`` / ``parser.in_header[N]`` would be silently misread under column
drift if it slipped into the name-based path. This test fails if such a parser is not classified in
``venue_mode.POSITIONAL_VENUES`` — so adding a new positional parser forces a conscious update.
"""

import inspect
import re

import bittytax.conv.parsers as _parsers  # noqa: F401  (registers every parser)
from bittytax.conv import venue_mode
from bittytax.conv.dataparser import DataParser

# .row[<int>] (incl. slices like row[0 : n]) and .in_header[<int>] are positional reads.
_POSITIONAL_READ = re.compile(r"\.row\[\s*\d|\.in_header\[\s*\d")


def _reads_positionally(parser: DataParser) -> bool:
    # True if the parser handler — or a same-module helper it calls by name (one level, e.g. the
    # Hotbit _parse_hotbit_orders_row or Coinbase parse_coinbase_transfers helpers) — reads a cell
    # by position. Per-parser (not per-module) so a name-based parser is not flagged just because a
    # POSITIONAL sibling lives in the same file.
    handler = parser.row_handler or parser.all_handler
    if handler is None:
        return False
    try:
        source = inspect.getsource(handler)
    except OSError:
        return False
    if _POSITIONAL_READ.search(source):
        return True
    module = inspect.getmodule(handler)
    if module is None:
        return False
    for name, func in inspect.getmembers(module, inspect.isfunction):
        if func is not handler and name in source:
            try:
                if _POSITIONAL_READ.search(inspect.getsource(func)):
                    return True
            except OSError:
                pass
    return False


def test_positional_parsers_are_classified_positional() -> None:
    # Any parser that actually reads positionally MUST be is_positional (so it never enters the
    # name-based scoped path). This also guards venue_mode.NAME_BASED_PARSERS: exempting a parser
    # that in fact reads positionally fails here.
    offenders = sorted(
        {
            p.name
            for p in DataParser.parsers
            if _reads_positionally(p) and not venue_mode.is_positional(p)
        }
    )
    assert not offenders, (
        "these parsers read cells positionally (in their handler or a helper it calls) but "
        f"venue_mode.is_positional() returns False: {offenders}"
    )


def test_projectable_positional_reads_in_bounds() -> None:
    # E2 projection rebuilds each row to the declared column count, so a handler that reads
    # a positional index >= ITS declared length would IndexError on the projected row. Assert every
    # projectable parser keeps the positional reads in its own handler within its own header. (This
    # is exactly why HitBTC is NOT projectable: parse_hitbtc_deposits_withdrawals_v1 reads row[6]
    # against a 6-column header.) Checks direct handler reads; helper reads in the current parsers
    # are all index 0, comfortably in bounds.
    pos_index = re.compile(r"\.row\[\s*(\d+)\s*\]|\.in_header\[\s*(\d+)\s*\]")
    offenders = []
    for parser in DataParser.parsers:
        if not venue_mode.is_projectable(parser):
            continue
        handler = parser.row_handler or parser.all_handler
        if handler is None:
            continue
        try:
            source = inspect.getsource(handler)
        except OSError:
            continue
        indices = [int(first or second) for first, second in pos_index.findall(source)]
        if indices and max(indices) >= len(parser.header):
            offenders.append((parser.name, max(indices), len(parser.header)))

    assert not offenders, (
        "projectable parsers whose positional reads exceed their declared header "
        f"(projection would IndexError): {offenders}"
    )

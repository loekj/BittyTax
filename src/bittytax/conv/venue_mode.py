# -*- coding: utf-8 -*-
"""Venue-scoped header matching (fork-only, opt-in).

When the user states which exchange/wallet a file came from (``--venue`` / ``config.venue``),
header matching is scoped to that venue's parsers and made tolerant of added or changed columns.
This is a purely ADDITIVE path: with no venue set nothing here runs and matching behaves exactly as
before. See plan "Venue-scoped lenient header matching".
"""

import inspect
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable, FrozenSet, List, Optional, cast

if TYPE_CHECKING:
    from .dataparser import DataParser

# Venues whose parsers read cells by POSITION (row[N] / in_header[N]) rather than by name. Added
# columns shift those positions, so they cannot use the name-based scored path safely. They are
# skipped by scoped matching here (they fall back to today's exact matching) and brought in later
# via column projection (E2) where their columns are uniquely named.
# MAINTENANCE: any new parser that reads cells positionally MUST have its venue key added here —
# enforced by tests/parsers/test_venue_classification.py.
POSITIONAL_VENUES = {
    "binance",
    "coinbase",
    "cointracking",
    "cointracker",
    "tradesatoshi",
    "hitbtc",
    "hotbit",
    "generic",
    "staketax",
}

# Positional venues whose columns are uniquely named AND whose positional reads stay within the
# declared header, so a drifted file can be PROJECTED back onto the declared column order by name
# (E2). Excluded and kept on exact matching: Coinbase (reads its asset from a column's header
# text), CoinTracking (duplicate "Cur." columns), generic/staketax (slice by position), and HitBTC
# (its deposits/withdrawals handler reads row[6] with a 6-column header — out of bounds, so
# projection would truncate and IndexError). The set is gated by
# test_venue_classification.test_projectable_positional_reads_in_bounds.
PROJECTABLE_VENUES = {
    "binance",
    "tradesatoshi",
    "hotbit",
}

# Parsers that share a positional venue's name-prefix but are themselves NAME-BASED (read only
# row_dict), verified by reading their handlers. They get scoped leniency rather than being excluded
# with their venue's positional siblings — e.g. within Coinbase only "Coinbase Transfers"
# (in_header[2]) and "Coinbase Transactions" (row[21]) read positionally; the rest read by name.
# Only names that are EXCLUSIVELY name-based may be listed (a name with a positional variant, e.g.
# "HitBTC Deposits/Withdrawals", must NOT appear). Guarded by
# test_positional_parsers_are_classified_positional, which fails CI if any listed parser reads
# positionally.
NAME_BASED_PARSERS = {
    "Coinbase",
    "Coinbase Pro Account",
    "Coinbase Pro Fills",
    "Coinbase Prime Orders",
    "Coinbase Prime Transactions",
}

# tytle venue label (normalised) -> bittytax parser-name key (normalised), for the few venues whose
# label does not prefix-match the parser name.
VENUE_ALIASES = {
    "bitcoincore": "qtwallet",
}


def normalize_venue(name: str) -> str:
    # Fold to a comparable key: lowercase, alphanumerics only (e.g. "KuCoin" -> "kucoin").
    return re.sub(r"[^a-z0-9]", "", name.lower())


def venue_key(venue: str) -> str:
    key = normalize_venue(venue)
    return VENUE_ALIASES.get(key, key)


def is_positional(parser: "DataParser") -> bool:
    if parser.name in NAME_BASED_PARSERS:
        return False
    key = normalize_venue(parser.name)
    return any(key.startswith(v) for v in POSITIONAL_VENUES)


def is_projectable(parser: "DataParser") -> bool:
    key = normalize_venue(parser.name)
    return any(key.startswith(v) for v in PROJECTABLE_VENUES)


def build_projection(result: "ScanResult") -> Optional[List[int]]:
    # File-column indices in declared-header order, for realigning a positional parser onto a
    # drifted file. None when any declared column is unbound (then projection is unsafe and the
    # caller falls back to exact matching). Projectable venues have no None/wildcard columns, so a
    # fully-matched scan yields one concrete index per declared column.
    if result.matched_count != result.declared_count:
        return None
    if any(b is None for b in result.binding):
        return None
    return [b for b in result.binding if b is not None]


def scope_candidates(parsers: List["DataParser"], venue: str) -> List["DataParser"]:
    # A parser belongs to the venue when its normalised name begins with the venue key, so
    # "KuCoin Trades"/"KuCoin Deposits" both scope to venue "kucoin". Order is preserved
    # (registration order), which is the stable tiebreak used by the caller.
    key = venue_key(venue)
    if not key:
        return []
    return [p for p in parsers if normalize_venue(p.name).startswith(key)]


@dataclass
class ScanResult:
    matched_count: int
    declared_count: int
    args: List[Any]
    binding: List[Optional[int]]
    ambiguous: bool


def scan(parser: "DataParser", file_header: List[str]) -> ScanResult:
    """Match a single declared header against a file header, order-agnostic and tolerant of
    extra columns. Returns per-declared-column ``binding`` (file index or None), the callable
    ``args`` in declared order (exactly as the existing matchers produce them), and an
    ``ambiguous`` flag: a string/callable column matching MORE THAN ONE file column, which would
    risk binding the wrong cell — the caller refuses such a match rather than guess."""
    binding: List[Optional[int]] = []
    args: List[Any] = []
    matched = 0
    declared = 0
    ambiguous = False

    for col in parser.header:
        if col is None or col == "":
            # None = wildcard; "" = an unnamed/blank column that carries no identifying info (and
            # real exports often have several trailing blanks). Neither counts toward matching, and
            # neither triggers the duplicate-column refusal below.
            binding.append(None)
            continue
        declared += 1
        if callable(col):
            matches = [(i, m) for i, cell in enumerate(file_header) for m in (col(cell),) if m]
            if len(matches) > 1:
                ambiguous = True
            if matches:
                binding.append(matches[0][0])
                args.append(matches[0][1])
                matched += 1
            else:
                binding.append(None)
        else:
            positions = [i for i, cell in enumerate(file_header) if cell == col]
            if len(positions) > 1:
                ambiguous = True
            if positions:
                binding.append(positions[0])
                matched += 1
            else:
                binding.append(None)

    return ScanResult(
        matched_count=matched,
        declared_count=declared,
        args=args,
        binding=binding,
        ambiguous=ambiguous,
    )


# --- E1: required read-set (tolerate declared-but-unread columns being absent) -----------------

# A handler that reads row_dict by a NON-literal key (e.g. f"Fee Value ({ccy})") or consumes
# positional matcher args (parser.args[...]) cannot be analysed safely -> fall back to requiring
# all declared columns. The remaining regexes pull literal reads / writes / guards / .get().
_UNANALYSABLE = re.compile(r"""row_dict\[\s*(?!["'])|\.args\[""")
_READ_RE = re.compile(r"""row_dict\[\s*["']([^"']+)["']\s*\]""")
_ASSIGN_RE = re.compile(r"""row_dict\[\s*["']([^"']+)["']\s*\]\s*=(?!=)""")
_GUARD_RE = re.compile(r"""["']([^"']+)["']\s+in\s+row_dict""")
_GET_RE = re.compile(r"""row_dict\.get\(\s*["']([^"']+)["']""")


@lru_cache(maxsize=None)
def required_columns(handler: Callable) -> Optional[FrozenSet[str]]:
    """Columns a handler MUST read from the row (so the file must contain them), derived from its
    source. Returns None when the handler can't be analysed safely (dynamic keys / positional
    args) — the caller then requires all declared columns. Excludes columns that are written
    (``row_dict["x"] =``), probed (``"x" in row_dict``), or read with a default (``.get``), since
    those tolerate absence. An incomplete result is still safe: a genuinely-needed but omitted
    column surfaces as a MissingColumnError (recorded row failure), never a wrong number."""
    try:
        src = inspect.getsource(handler)
    except (OSError, TypeError):
        return None
    if _UNANALYSABLE.search(src):
        return None
    reads = set(_READ_RE.findall(src))
    if not reads:
        return None
    optional = (
        set(_ASSIGN_RE.findall(src)) | set(_GUARD_RE.findall(src)) | set(_GET_RE.findall(src))
    )
    return frozenset(reads - optional)


def is_eligible(parser: "DataParser", result: ScanResult, header_set: FrozenSet[str]) -> bool:
    """Whether a scoped candidate matches the file.

    Baseline: every declared column is present (always eligible). E1 then RELAXES this for simple
    name-based handlers (no callable header columns, no positional args): they also match when just
    the columns they actually READ are present and at least one declared column matched. E1 only
    ADDS candidates — which rank below a full match by matched_count — so it can never displace the
    correct fully-matched parser; it only rescues a drifted file that nothing matches in full.
    (Replacing the baseline, rather than relaxing it, could make a fully-matching parser ineligible
    on an extraction quirk and hand the file to a sibling format — a real bug.)"""
    if result.declared_count > 0 and result.matched_count == result.declared_count:
        return True
    if result.matched_count > 0 and not any(callable(col) for col in parser.header):
        handler = parser.row_handler or parser.all_handler
        if handler is not None:
            req = required_columns(cast(Callable, handler))
            if req:
                return req.issubset(header_set)
    return False

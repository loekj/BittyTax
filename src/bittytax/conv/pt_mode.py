# -*- coding: utf-8 -*-
# (c) Nano Nano Ltd 2024

"""Portugal ("PT") destination-country mode for the data conversion tool.

Selected with ``--country PT`` (or ``country: 'PT'`` in the config file). The output CSV format is
unchanged; only the *classification* of forced token conversions differs.

A forced conversion (token delisting, migration or redenomination, e.g. MATIC->POL, USDT->USDC) is,
under Portuguese (CIRS) law, a non-taxable crypto-to-crypto swap whose cost basis and acquisition
date carry over to the new asset. BittyTax's default (UK) handling books each leg separately - the
received leg as an ``Airdrop`` (zero cost) and the sent leg as a ``Spend`` (a disposal) - which is
wrong for PT and cannot be reconstructed downstream.

To keep the change minimal and safe, parsers do not restructure their logic: in PT mode they simply
set ``pt_conversion`` on the ``TransactionOutRecord`` of each forced-conversion leg they emit.
This module then runs once, after all files are parsed and merged, and combines each flagged
sent/received pair into a single crypto-to-crypto ``Trade``. Only flagged legs are ever touched;
anything that cannot be paired is left exactly as it was (today's behaviour) with a warning.
"""

import sys
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional, Tuple

from colorama import Fore

from ..bt_types import TrType
from ..config import config
from ..constants import COUNTRY_PT, WARNING
from .out_record import TransactionOutRecord

if TYPE_CHECKING:
    from .datafile import DataFile
    from .datarow import DataRow

# A sent and a received leg are only paired if they fall within this window of each other.
CONVERSION_WINDOW = timedelta(days=30)

# A pairing looser than either of these is still made, but flagged to stderr for manual review.
LOW_CONFIDENCE_WINDOW = timedelta(days=7)
LOW_CONFIDENCE_QUANTITY_DISTANCE = Decimal("0.25")


def is_pt() -> bool:
    return bool(config.country == COUNTRY_PT)


def flag_conversion(data_row: "DataRow") -> None:
    """Mark a forced-conversion leg so ``merge_conversions`` can pair it into a single Trade.

    A no-op unless PT mode is active, so parsers can call it unconditionally from the branch that
    already identifies the conversion (e.g. a Kraken ``delistingconversion`` subtype).
    """
    if is_pt() and data_row.t_record is not None:
        data_row.t_record.pt_conversion = True


def merge_conversions(data_files: List["DataFile"]) -> None:
    """Merge flagged forced-conversion legs into single crypto-to-crypto Trades.

    Safe by construction: only records explicitly flagged with ``pt_conversion`` are considered, and
    an unpaired flagged leg is left untouched (as the original Airdrop/Spend) with a warning.
    """
    legs = [
        data_row
        for data_file in data_files
        for data_row in data_file.data_rows
        if data_row.t_record is not None and data_row.t_record.pt_conversion
    ]
    if not legs:
        return

    sent = [dr for dr in legs if dr.t_record is not None and dr.t_record.sell_quantity]
    received = [dr for dr in legs if dr.t_record is not None and dr.t_record.buy_quantity]

    pairs, leftovers = pair_conversion_legs(sent, received)

    low_confidence = 0
    for sent_row, recv_row in pairs:
        if _emit_pairing_diagnostics(sent_row, recv_row):
            low_confidence += 1
        _make_conversion_trade(sent_row, recv_row)

    for data_row in leftovers:
        # No counterparty leg found: keep the original Airdrop/Spend (default behaviour) but clear
        # the now-meaningless flag and make the gap visible.
        if data_row.t_record is not None:
            data_row.t_record.pt_conversion = False
        sys.stderr.write(
            f"{WARNING} PT mode: could not pair forced-conversion leg, left unchanged: "
            f"{data_row.t_record}\n"
        )

    summary = f"{Fore.CYAN}PT mode: merged {len(pairs)} forced conversion(s) into Trades"
    if low_confidence:
        summary += f" ({low_confidence} low-confidence)"
    if leftovers:
        summary += f"; {len(leftovers)} leg(s) left unchanged"
    sys.stderr.write(f"{summary}\n")


def pair_conversion_legs(
    sent: List["DataRow"], received: List["DataRow"]
) -> Tuple[List[Tuple["DataRow", "DataRow"]], List["DataRow"]]:
    """Greedily pair each sent leg with the nearest-in-time received leg of a different asset in the
    same wallet, within ``CONVERSION_WINDOW``. Returns ``(pairs, unpaired_legs)``."""
    pairs: List[Tuple["DataRow", "DataRow"]] = []
    used = set()  # id() of received rows already consumed
    received_sorted = sorted(received, key=lambda dr: dr.timestamp)

    for sent_row in sorted(sent, key=lambda dr: dr.timestamp):
        sent_rec = sent_row.t_record
        if sent_rec is None:
            continue

        best_row = None
        best_key = None
        for recv_row in received_sorted:
            if id(recv_row) in used:
                continue
            recv_rec = recv_row.t_record
            if recv_rec is None:
                continue
            if recv_rec.buy_asset == sent_rec.sell_asset:
                continue  # a conversion is to a *different* asset
            if recv_rec.wallet != sent_rec.wallet:
                continue  # never pair across wallets/exchanges
            delta = abs(recv_row.timestamp - sent_row.timestamp)
            if delta > CONVERSION_WINDOW:
                continue
            # Pick the nearest leg in time; break ties (e.g. simultaneous conversions) by the
            # closest quantity, which favours the matching 1:1 redenomination over an unrelated leg.
            key = (delta, _quantity_distance(sent_rec.sell_quantity, recv_rec.buy_quantity))
            if best_key is None or key < best_key:
                best_row = recv_row
                best_key = key

        if best_row is not None:
            used.add(id(best_row))
            pairs.append((sent_row, best_row))

    paired = {id(sent_row) for sent_row, _ in pairs} | used
    leftovers = [dr for dr in sent + received if id(dr) not in paired]
    return pairs, leftovers


def _emit_pairing_diagnostics(sent_row: "DataRow", recv_row: "DataRow") -> bool:
    """Warn on a loose (low-confidence) pairing and return True if it was; else log under debug.

    Must be called before ``_make_conversion_trade`` (which clears the received leg).
    """
    sent_rec = sent_row.t_record
    recv_rec = recv_row.t_record
    if sent_rec is None or recv_rec is None:
        return False

    delta = abs(recv_row.timestamp - sent_row.timestamp)
    qty_dist = _quantity_distance(sent_rec.sell_quantity, recv_rec.buy_quantity)
    desc = f"{sent_rec.sell_asset} -> {recv_rec.buy_asset}"

    if qty_dist > LOW_CONFIDENCE_QUANTITY_DISTANCE or delta > LOW_CONFIDENCE_WINDOW:
        sys.stderr.write(
            f"{WARNING} PT mode: low-confidence conversion pairing "
            f"({delta.days} day(s) apart, {qty_dist:.0%} quantity difference), review: {desc}\n"
        )
        return True

    if config.debug:
        sys.stderr.write(f"{Fore.CYAN}pt: merged conversion {desc}\n")
    return False


def _quantity_distance(sent_qty: Optional[Decimal], recv_qty: Optional[Decimal]) -> Decimal:
    """Relative difference between two quantities (0 = identical, ~1 = very different)."""
    if not sent_qty or not recv_qty:
        return Decimal(1)
    largest = max(abs(sent_qty), abs(recv_qty))
    if largest == 0:
        return Decimal(0)
    return abs(abs(sent_qty) - abs(recv_qty)) / largest


def _make_conversion_trade(sent_row: "DataRow", recv_row: "DataRow") -> None:
    sent_rec = sent_row.t_record
    recv_rec = recv_row.t_record
    if sent_rec is None or recv_rec is None:
        return

    # Carry a (non-zero) fee from whichever leg has one (forced conversions are usually fee-less),
    # preferring the disposal leg.
    fee_quantity: Optional[Decimal] = None
    fee_asset = ""
    fee_value: Optional[Decimal] = None
    if sent_rec.fee_quantity:
        fee_quantity, fee_asset, fee_value = (
            sent_rec.fee_quantity,
            sent_rec.fee_asset,
            sent_rec.fee_value,
        )
    elif recv_rec.fee_quantity:
        fee_quantity, fee_asset, fee_value = (
            recv_rec.fee_quantity,
            recv_rec.fee_asset,
            recv_rec.fee_value,
        )

    sent_row.t_record = TransactionOutRecord(
        TrType.TRADE,
        sent_row.timestamp,
        buy_quantity=recv_rec.buy_quantity,
        buy_asset=recv_rec.buy_asset,
        sell_quantity=sent_rec.sell_quantity,
        sell_asset=sent_rec.sell_asset,
        fee_quantity=fee_quantity,
        fee_asset=fee_asset,
        fee_value=fee_value,
        wallet=sent_rec.wallet,
        note="Delisting conversion",
    )
    recv_row.t_record = None

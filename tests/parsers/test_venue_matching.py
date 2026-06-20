# -*- coding: utf-8 -*-
"""Venue-scoped header matching (--venue / config.venue): correctness and safety.

Covers: drift tolerance, format disambiguation, the refuse-on-ambiguity guards, golden args under
drift, E1 (read-set eligibility), E2 (positional projection), and that scoping never changes an
unchanged decision (venue-on == venue-off). The global-path "never worse" net lives in
test_global_regression.py.
"""

from decimal import Decimal

import pytest

import bittytax.conv.parsers as _parsers  # noqa: F401  (registers every parser)
from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.conv.datafile import DataFile
from bittytax.conv.dataparser import DataParser
from bittytax.conv.datarow import DataRow
from bittytax.conv.exceptions import MissingColumnError
from bittytax.conv.parsers.kucoin import parse_kucoin_trades_v5

# A real KuCoin "Spot Orders Filled" v5 export header (19 columns, incl. trailing "Account Mode").
KUCOIN_V5 = [
    "UID",
    "Account Type",
    "Order ID",
    "Order Time(UTC+01:00)",
    "Symbol",
    "Side",
    "Order Type",
    "Order Price",
    "Order Amount",
    "Avg. Filled Price",
    "Filled Amount",
    "Filled Volume",
    "Filled Volume (USDT)",
    "Filled Time(UTC+01:00)",
    "Fee",
    "Fee Currency",
    "Tax",
    "Status",
    "Account Mode",
]
KUCOIN_V5_ROW = [
    "28896117",
    "mainAccount",
    "65ba",
    "2024-01-31 14:54:26",
    "DAG-USDT",
    "SELL",
    "LIMIT",
    "0.051",
    "1045079.6954",
    "0.051",
    "174196.5908",
    "8889.08",
    "8889.08",
    "2024-01-31 14:55:54",
    "8.889",
    "USDT",
    "",
    "part_deal",
    "CLASSIC",
]

# A real Koinly tax-report header (the older export read by parse_koinly via parser.args[2]).
KOINLY_REPORT = [
    "Date",
    "Type",
    "Tag",
    "Sending Wallet",
    "Sent Amount",
    "Sent Currency",
    "Sent Cost Basis",
    "Receiving Wallet",
    "Received Amount",
    "Received Currency",
    "Received Cost Basis",
    "Fee Amount",
    "Fee Currency",
    "Gain (GBP)",
    "Net Value (GBP)",
    "Fee Value (GBP)",
    "TxSrc",
    "TxDest",
    "TxHash",
    "Description",
]


# --- C2: drift tolerance + the headline fix ---------------------------------------------------


def test_added_column_matches_under_venue_but_not_without() -> None:
    drifted = KUCOIN_V5 + ["Brand New Column"]
    with pytest.raises(KeyError):  # today's behaviour: exact-count fails
        DataParser.match_header(list(drifted), 0)
    parser = DataParser.match_header(list(drifted), 0, "KuCoin")
    assert parser.name == "KuCoin Trades"
    assert parser.row_handler is parse_kucoin_trades_v5


def test_added_column_still_parses_correctly() -> None:
    drifted = KUCOIN_V5 + ["Brand New Column"]
    parser = DataParser.match_header(list(drifted), 0, "KuCoin")
    data_row = DataRow(1, KUCOIN_V5_ROW + ["ignored"], parser.in_header, "KuCoin T")
    parse_kucoin_trades_v5(data_row, parser)
    assert data_row.t_record is not None
    assert data_row.t_record.sell_quantity == Decimal("174196.5908")
    assert data_row.t_record.sell_asset == "DAG"
    assert data_row.t_record.buy_asset == "USDT"
    assert data_row.t_record.fee_quantity == Decimal("8.889")


# --- C3: refuse-on-ambiguity guards (never guess) ---------------------------------------------


def test_below_eligibility_falls_back_to_unrecognised() -> None:
    # A clearly-wrong header under a real venue must NOT be force-matched; it falls back and raises.
    with pytest.raises(KeyError):
        DataParser.match_header(["totally", "unrelated", "columns"], 0, "KuCoin")


def test_duplicate_declared_column_refused() -> None:
    # "Fee" appears twice, so a declared name binds ambiguously -> refuse (fall back), rather than
    # silently bind the wrong cell.
    dup = list(KUCOIN_V5)
    dup[16] = "Fee"  # was "Tax"; now two "Fee" columns
    with pytest.raises(KeyError):
        DataParser.match_header(dup, 0, "KuCoin")


def test_blank_columns_are_not_treated_as_duplicates() -> None:
    # Real exports carry unnamed/trailing blank columns; these are wildcards, NOT duplicate columns,
    # so they must not trigger the refusal above. FTX Deposits declares two blank columns.
    header = ["", "Time", "Coin", "Amount", "Status", "Additional info", "Transaction ID", ""]
    assert DataParser.match_header(list(header), 0, "FTX").name == "FTX Deposits"


def test_greedy_callable_decoy_refused() -> None:
    # A decoy "Net Value (USD)" beside "Net Value (GBP)" makes the Net Value callable match two
    # columns -> refuse rather than risk binding the wrong currency.
    decoy = KOINLY_REPORT[:15] + ["Net Value (USD)"] + KOINLY_REPORT[15:]
    with pytest.raises(KeyError):
        DataParser.match_header(decoy, 0, "Koinly")


# --- C4: golden args under benign drift -------------------------------------------------------


def test_koinly_currency_args_correct_under_added_column() -> None:
    # parse_koinly extracts the report currency from parser.args[2].group(1). A benign added column
    # (matching no callable) must not shift that index.
    drifted = KOINLY_REPORT + ["Imported From"]
    parser = DataParser.match_header(list(drifted), 0, "Koinly")
    assert parser.name == "Koinly"
    assert parser.args[2].group(1) == "GBP"  # the Net Value (GBP) match, not Gain or Fee


def test_kucoin_futures_args1_is_closing_time_under_drift() -> None:
    # _parse_kucoin_futures_row uses parser.args[1] (the SECOND callable = Closing Time) for the
    # timestamp. Scoped scan must keep the two ordered callables (Opening, then Closing) in declared
    # order even with an added column, else the timestamp would use the OPENING time.
    header = [
        "UID",
        "Account Type",
        "Symbol",
        "Close Type",
        "Realized PNL",
        "Total Realized PNL",
        "Total Funding Fees",
        "Total Trading Fees",
        "Position Opening Time(UTC+01:00)",
        "Position Closing Time(UTC+01:00)",
    ]
    parser = DataParser.match_header(header + ["Brand New Column"], 0, "KuCoin")
    assert parser.name == "KuCoin Bundle Futures Orders Realized PNL"
    assert parser.args[1].group(1).startswith("Position Closing Time")  # args[1] = 2nd callable

    row = [
        "uid",
        "acct",
        "XBTUSDTM",
        "CLOSE_LONG",
        "10",
        "10",
        "1",
        "0.5",
        "2021-01-01 00:00:00",
        "2021-06-15 12:00:00",
        "junk",
    ]
    data_row = DataRow(1, row, parser.in_header, "Kucoin F")
    DataRow.parse_all([data_row], parser)
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.MARGIN_GAIN
    assert data_row.t_record.timestamp.date().isoformat() == "2021-06-15"  # CLOSING, not opening


def test_swissborg_args0_is_first_currency_callable_under_drift() -> None:
    # SwissBorg declares THREE currency callables (Gross amount, Fee, Net amount); the handler uses
    # parser.args[0] (the FIRST = Gross amount) as the currency. Scoped scan must order callables by
    # declared position so args[0] stays Gross amount even with an added column.
    header = [
        "Local time",
        "Time in UTC",
        "Type",
        "Currency",
        "Gross amount",
        "Gross amount (GBP)",
        "Fee",
        "Fee (GBP)",
        "Net amount",
        "Net amount (GBP)",
        "Note",
    ]
    parser = DataParser.match_header(header + ["Extra"], 0, "SwissBorg")
    assert parser.name == "SwissBorg"
    assert parser.args[0].group(0).startswith("Gross amount")  # first callable, not Fee/Net
    assert parser.args[0].group(1) == "GBP"


# --- C5: E1 read-set eligibility + Component C safety net --------------------------------------


def test_missing_required_column_is_recorded_not_crash() -> None:
    # Component C: reading a column absent from the header raises a DataRowError (recorded as a row
    # failure), never an uncaught KeyError that aborts the whole run.
    parser = DataParser.match_header(list(KUCOIN_V5), 0, "KuCoin")
    short_header = [c for c in parser.in_header if c != "Filled Amount"]
    data_row = DataRow(1, KUCOIN_V5_ROW[: len(short_header)], short_header, "KuCoin T")
    data_row.parse(parser)
    assert isinstance(data_row.failure, MissingColumnError)
    assert "Filled Amount" in str(data_row.failure)


# --- C6: E2 projection vs strict positional venues --------------------------------------------


def test_coinbase_strict_drift_is_unrecognised_not_wrong() -> None:
    # Coinbase (asset-by-position / None columns) is kept strict: a drifted file falls back to exact
    # matching and is reported unrecognised rather than risk a silently-wrong projection.
    coinbase = [
        "Timestamp",
        "Balance",
        "Amount",
        "Currency",
        "To",
        "Notes",
        "Instantly Exchanged",
        "Transfer Total",
        "Transfer Total Currency",
        "Transfer Fee",
        "Transfer Fee Currency",
        "Transfer Payment Method",
        "Transfer ID",
        "Order Price",
        "Order Currency",
        "",
        "Order Tracking Code",
        "Order Custom Parameter",
        "Order Paid Out",
        "Recurring Payment ID",
        "",
        "",
    ]
    assert DataParser.match_header(list(coinbase), 0).name == "Coinbase Transactions"  # clean match
    with pytest.raises(KeyError):  # drifted + strict venue -> fall back -> unrecognised
        DataParser.match_header(coinbase + ["Extra"], 0, "Coinbase")


# --- E2: projection realigns positional reads under drift -------------------------------------


def test_binance_projection_realigns_string_positional_read() -> None:
    # parse_binance_deposits_withdrawals_crypto_v1 reads the timestamp at row[0]. With a column
    # inserted BEFORE it, projection must realign row[0] back onto the Date column.
    header = [
        "Date(UTC)",
        "Coin",
        "Network",
        "Amount",
        "TransactionFee",
        "Address",
        "TXID",
        "SourceAddress",
        "PaymentID",
        "Status",
    ]
    drifted = ["Inserted Col"] + header  # everything shifts right by one
    parser = DataParser.match_header(list(drifted), 0, "Binance")
    assert parser.name == "Binance Deposits/Withdrawals"
    assert parser.projection is not None and parser.projection[0] == 1  # row[0] -> the Date column

    values = {
        "Date(UTC)": "2023-05-01 10:00:00",
        "Coin": "BTC",
        "Network": "BTC",
        "Amount": "0.5",
        "TransactionFee": "0",
        "Address": "a",
        "TXID": "h",
        "SourceAddress": "",
        "PaymentID": "",
        "Status": "Completed",
    }
    drifted_row = ["junk"] + [values[c] for c in header]
    projected = [drifted_row[i] for i in parser.projection]
    data_row = DataRow(1, projected, parser.in_header, "Binance")
    parser.row_handler(data_row, parser, filename="binance_deposit.csv")
    assert data_row.t_record is not None
    assert data_row.t_record.timestamp.date().isoformat() == "2023-05-01"  # row[0] landed on Date
    assert data_row.t_record.buy_quantity == Decimal("0.5")
    assert data_row.t_record.buy_asset == "BTC"


def test_tradesatoshi_projection_realigns_callable_positional_reads() -> None:
    # parse_tradesatoshi_trades reads direction (row[2]) and timestamp (row[6]) positionally, and
    # BOTH those declared columns are callables. Projection must bind the callables and realign.
    header = ["Id", "TradePair", "TradeType", "Amount", "Rate", "Fee", "Timestamp", "IsApi"]
    drifted = ["Inserted Col"] + header
    parser = DataParser.match_header(list(drifted), 0, "TradeSatoshi")
    assert parser.name == "TradeSatoshi Trades"
    assert parser.projection is not None

    drifted_row = [
        "junk",
        "1",
        "LTC/BTC",
        "Sell",
        "10",
        "0.01",
        "0.001",
        "2020-06-15 10:00:00",
        "x",
    ]
    projected = [drifted_row[i] for i in parser.projection]
    data_row = DataRow(1, projected, parser.in_header, "TradeSatoshi")
    parser.row_handler(data_row, parser)
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.TRADE
    assert data_row.t_record.sell_quantity == Decimal("10")  # row[2]=="Sell" branch taken
    assert data_row.t_record.sell_asset == "LTC"
    assert data_row.t_record.timestamp.date().isoformat() == "2020-06-15"  # row[6] landed on date


# --- C1: scoping does not change a clean-file match (venue-on == venue-off) --------------------


def test_venue_scoping_picks_same_parser_for_unique_clean_headers() -> None:
    # On a clean, unambiguous header, turning the venue ON must select the SAME parser as the
    # global matcher (scoping only adds drift tolerance). Restricted to string-only headers (a
    # faithful real header; callable/None columns can't be reconstructed here) that are unique (a
    # header shared by sibling formats is ambiguous by columns alone — global picks by registration
    # order, scoped by rank, and either is valid).
    counts: dict = {}
    string_only = []
    for parser in DataParser.parsers:
        if any(callable(col) or col is None for col in parser.header):
            continue
        key = tuple(parser.header)
        counts[key] = counts.get(key, 0) + 1
        string_only.append(parser)

    mismatches = []
    for parser in string_only:
        if counts[tuple(parser.header)] > 1:
            continue  # header shared by sibling variants -> inherently ambiguous, skip
        header = list(parser.header)
        glob = DataParser.match_header(list(header), 0)
        try:
            scoped = DataParser.match_header(list(header), 0, glob.name.split()[0])
        except KeyError:
            scoped = None
        if scoped is None or scoped.name != glob.name:
            mismatches.append((header[:3], glob.name, None if scoped is None else scoped.name))
    assert not mismatches, f"scoping changed/lost a unique clean-header match: {mismatches[:5]}"


# --- Classification: name-based parsers under a positional venue still get leniency ------------


def test_coinbase_name_based_variant_gets_scoped_leniency() -> None:
    # Coinbase v1-v4 / Pro / Prime read only row_dict (only "Coinbase Transfers"/"Coinbase
    # Transactions" read positionally), so they must NOT be lumped strict with the positional ones —
    # a drifted Coinbase file should scope-match instead of being unrecognised.
    header = [
        "ID",
        "Timestamp",
        "Transaction Type",
        "Asset",
        "Quantity Transacted",
        "Price Currency",
        "Price at Transaction",
        "Subtotal",
        "Total (inclusive of fees and/or spread)",
        "Fees and/or Spread",
        "Notes",
    ]
    with pytest.raises(KeyError):  # drifted: today's exact match fails
        DataParser.match_header(header + ["New Col"], 0)
    assert DataParser.match_header(header + ["New Col"], 0, "Coinbase").name == "Coinbase"


# --- Infra: get_parser threads config.venue and scans past a preamble (covers CSV + xlsx) ------


def test_get_parser_uses_venue_and_scans_past_preamble() -> None:
    # get_parser is the single entry both the CSV and xlsx readers use; it reads config.venue and
    # scans up to 14 rows. A preamble line before a drifted header must still scope-match.
    config.config["venue"] = "KuCoin"
    try:
        reader = iter([["preamble, not a header"], KUCOIN_V5 + ["Extra"], KUCOIN_V5_ROW + ["x"]])
        parser = DataFile.get_parser(reader)
        assert parser is not None and parser.name == "KuCoin Trades"
    finally:
        config.config["venue"] = None


# --- Pin name-based callable args-consumers: scoped picks same parser + same args as global ----


@pytest.mark.parametrize(
    "venue, expected, header",
    [
        (
            "PayPal",
            "PayPal",
            [
                "DateTime",
                "Transaction Type",
                "Asset In (Quantity)",
                "Asset In (Currency)",
                "Asset Out (Quantity)",
                "Asset Out (Currency)",
                "Transaction Fee (Quantity)",
                "Transaction Fee (Currency)",
                "Market Value (GBP)",
            ],
        ),
        (
            "Trezor",
            "Trezor Suite",
            [
                "Timestamp",
                "Date",
                "Time",
                "Type",
                "Transaction ID",
                "Fee",
                "Fee unit",
                "Address",
                "Label",
                "Amount",
                "Amount unit",
                "Fiat (GBP)",
                "Other",
            ],
        ),
        (
            "Qt",
            "Qt Wallet (i.e. Bitcoin Core, etc)",
            ["Confirmed", "Date", "Type", "Label", "Address", "Amount (BTC)", "ID"],
        ),
        (
            "MEXC",
            "MEXC Futures",
            [
                "UID",
                "Time of Update(UTC)",
                "Futures Trading Pair",
                "Direction",
                "Leverage",
                "Order Type",
                "Order Qty (Cont.)",
                "Filled Qty (Cont.)",
                "Order Qty (Crypto)",
                "Filled Qty (Crypto)",
                "Order Qty (Amount)",
                "Filled Qty (Amount)",
                "Order Price",
                "Average Filled Price",
                "Closing PNL",
                "Trading Fee",
                "Fee-payment Crypto",
                "Status",
            ],
        ),
    ],
)
def test_callable_parser_scoped_matches_global_with_stable_args(venue, expected, header) -> None:
    # Under an added column, scoped matching must pick the SAME parser AND extract the SAME callable
    # args (currency/timestamp) as the global matcher on the clean header — i.e. the callable binds
    # to the same column despite the drift.
    glob = DataParser.match_header(list(header), 0)
    assert glob.name == expected
    global_args = [a.group(0) for a in glob.args]
    scoped = DataParser.match_header(header + ["Drift Col"], 0, venue)
    assert scoped.name == expected
    assert [a.group(0) for a in scoped.args] == global_args

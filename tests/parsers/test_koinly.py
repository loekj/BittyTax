from decimal import Decimal
from typing import List, Optional

from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.conv.dataparser import DataParser
from bittytax.conv.datarow import DataRow
from bittytax.conv.parsers.koinly import parse_koinly_universal

# Use the configured local currency for value cells so DataParser.convert_currency is a
# pass-through (no currency conversion / network) and value assertions stay deterministic
# regardless of the local_currency. Suffixed with ";<id>" to also exercise the ";<koinly_id>"
# stripping Koinly applies to currencies.
_CCY = config.ccy + ";1"

# The header of the Koinly "Bulk edit in Excel" transactions export (Transactions page -> the 3-dot
# menu -> "Bulk edit in Excel" -> Export). This is the From/To column layout, distinct from the
# older Sent/Received "Koinly" tax-report export. Currency and wallet cells carry a ";<koinly_id>"
# suffix (e.g. "WFLR;9546698"), and the transaction value lives in "Net Value (read-only)" (or the
# user-set "Net Worth Amount") rather than in the header as in the older export.
UNIVERSAL_HEADER = [
    "ID (read-only)",
    "Parent ID (read-only)",
    "Date (UTC)",
    "Type",
    "Tag",
    "From Wallet (read-only)",
    "From Wallet ID",
    "From Amount",
    "From Currency",
    "To Wallet (read-only)",
    "To Wallet ID",
    "To Amount",
    "To Currency",
    "Fee Amount",
    "Fee Currency",
    "Net Worth Amount",
    "Net Worth Currency",
    "Fee Worth Amount",
    "Fee Worth Currency",
    "Net Value (read-only)",
    "Fee Value (read-only)",
    "Value Currency (read-only)",
    "Deleted",
    "From Source (read-only)",
    "To Source (read-only)",
    "Negative Balances (read-only)",
    "Missing Rates (read-only)",
    "Missing Cost Basis (read-only)",
    "Synced To Accounting At (UTC read-only)",
    "TxSrc",
    "TxDest",
    "TxHash",
    "Description",
]


def _row(**kwargs: str) -> List[str]:
    # Build a row aligned to UNIVERSAL_HEADER; only the named columns are set, the rest are empty
    # (which is how Koinly leaves the unused side of a one-sided deposit/withdrawal).
    values = {h: "" for h in UNIVERSAL_HEADER}
    values.update(kwargs)
    return [values[h] for h in UNIVERSAL_HEADER]


def _parse(row: List[str], header: Optional[List[str]] = None) -> DataRow:
    parser = DataParser.match_header(header if header is not None else UNIVERSAL_HEADER, 0)
    assert parser.name == "Koinly"
    assert parser.row_handler is parse_koinly_universal

    data_row = DataRow(1, row, parser.in_header, "Koinly")
    parse_koinly_universal(data_row, parser)
    return data_row


def test_reward_deposit_is_staking_reward() -> None:
    # Real-world case: a Flare delegation reward - a one-sided "To" deposit tagged "reward".
    # Asserts the ";<id>" stripping on both currency and wallet, and the value from Net Value.
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-01-03 22:19:40",
                "Type": "deposit",
                "Tag": "reward",
                "To Wallet (read-only)": "Flare (FLR);flare",
                "To Amount": "8121.9422723224",
                "To Currency": "WFLR;9546698",
                "Net Value (read-only)": "224.7162634542",
                "Value Currency (read-only)": _CCY,
            }
        )
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.STAKING_REWARD
    assert data_row.t_record.buy_quantity == Decimal("8121.9422723224")
    assert data_row.t_record.buy_asset == "WFLR"
    assert data_row.t_record.buy_value == Decimal("224.7162634542")
    assert data_row.t_record.wallet == "Flare (FLR)"


def test_tag_match_is_case_insensitive() -> None:
    # Koinly varies label casing across exports; "REWARD" must map the same as "reward".
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-02-03 10:00:00",
                "Type": "deposit",
                "Tag": "REWARD",
                "To Amount": "10",
                "To Currency": "SOL;3",
            }
        )
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.STAKING_REWARD


def test_value_falls_back_to_net_worth() -> None:
    # When the Koinly-computed "Net Value (read-only)" is absent (e.g. manually-entered rows), the
    # value must fall back to the user-set "Net Worth Amount" rather than being silently lost.
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-02-04 10:00:00",
                "Type": "deposit",
                "Tag": "reward",
                "To Amount": "5",
                "To Currency": "DOT;4",
                "Net Worth Amount": "200",
                "Net Worth Currency": _CCY,
            }
        )
    )
    assert data_row.t_record is not None
    assert data_row.t_record.buy_value == Decimal("200")


def test_trade_uses_from_and_to_sides() -> None:
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-02-01 10:00:00",
                "Type": "trade",
                "From Amount": "1",
                "From Currency": "BTC;1",
                "To Amount": "15",
                "To Currency": "ETH;2",
                "Net Value (read-only)": "50000",
                "Value Currency (read-only)": _CCY,
            }
        )
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.TRADE
    assert data_row.t_record.buy_quantity == Decimal("15")
    assert data_row.t_record.buy_asset == "ETH"
    assert data_row.t_record.sell_quantity == Decimal("1")
    assert data_row.t_record.sell_asset == "BTC"


def test_withdrawal_tagged_cost_is_spend() -> None:
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-02-02 10:00:00",
                "Type": "withdrawal",
                "Tag": "Cost",
                "From Amount": "0.5",
                "From Currency": "ETH;2",
            }
        )
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.SPEND
    assert data_row.t_record.sell_quantity == Decimal("0.5")
    assert data_row.t_record.sell_asset == "ETH"


def test_untagged_deposit_is_transfer_not_gift() -> None:
    # An untagged deposit in this export is a plain transfer-IN, so it maps to DEPOSIT - NOT
    # the older tax-report export's GIFT_RECEIVED default (which would over-tax every transfer
    # as a gift).
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-02-05 10:00:00",
                "Type": "deposit",
                "Tag": "",
                "To Amount": "2",
                "To Currency": "ADA;5",
            }
        )
    )
    assert data_row.t_record is not None
    assert data_row.t_record.t_type == TrType.DEPOSIT


def test_unknown_tag_is_unmapped() -> None:
    # An unrecognised tag is surfaced as an UnmappedType for review, not booked as another type.
    data_row = _parse(
        _row(
            **{
                "Date (UTC)": "2025-02-07 10:00:00",
                "Type": "deposit",
                "Tag": "SomeWeirdTag",
                "To Amount": "1",
                "To Currency": "XYZ;9",
            }
        )
    )
    assert data_row.t_record is not None
    # UnmappedType is a NewType(str): an unrecognised tag becomes the string "_<tag>", surfaced for
    # review (BittyTax flags unmapped types) rather than being silently booked as something else.
    assert data_row.t_record.t_type == "_SomeWeirdTag"


def test_matches_with_extra_columns() -> None:
    # header_fixed=False: the export may gain columns in a future Koinly version (as KuCoin's Spot
    # export gained a trailing "Account Mode" column). An inserted and a trailing extra column must
    # still match this parser, rather than reverting to "unrecognised".
    header = (
        UNIVERSAL_HEADER[:5]
        + ["Internal Note (read-only)"]
        + UNIVERSAL_HEADER[5:]
        + ["Future Trailing Col"]
    )
    parser = DataParser.match_header(header, 0)
    assert parser.name == "Koinly"
    assert parser.row_handler is parse_koinly_universal

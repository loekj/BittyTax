from types import SimpleNamespace
from typing import Any, List

from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.conv import pt_mode
from bittytax.conv.dataparser import DataParser
from bittytax.conv.datarow import DataRow
from bittytax.conv.parsers.gateio import parse_gateio

GATEIO_HEADER = [
    "no",
    "time",
    "action_desc",
    "action_data",
    "type",
    "change_amount",
    "amount",
    "total",
]


def _row(action_desc: str, action_data: str, asset: str, change: str, no: str = "1") -> List[str]:
    return [no, "2024-01-01 12:00:00", action_desc, action_data, asset, change, change, "0"]


def _parse(rows: List[List[str]], country: str) -> List[DataRow]:
    parser = DataParser.match_header(GATEIO_HEADER, 0)
    assert parser.name == "Gate.io"
    data_rows = [DataRow(i + 1, row, parser.in_header, "Gate.io") for i, row in enumerate(rows)]
    prev = config.config.get("country")
    config.config["country"] = country
    try:
        parse_gateio(data_rows, parser)
        if pt_mode.is_pt():
            data_files: List[Any] = [SimpleNamespace(data_rows=data_rows)]
            pt_mode.merge_conversions(data_files)
    finally:
        config.config["country"] = prev
    return data_rows


def test_pt_grouped_airdrop_conversion_becomes_trade() -> None:
    # Two "Airdrop" legs sharing an action_data group, opposite signs, different assets => a forced
    # conversion, which PT mode merges into a single Trade.
    data_rows = _parse(
        [
            _row("Airdrop", "G1", "USDT", "-100.0"),
            _row("Airdrop", "G1", "USDC", "99.9"),
        ],
        country="PT",
    )

    records = [dr.t_record for dr in data_rows if dr.t_record is not None]
    assert len(records) == 1
    assert records[0].t_type == TrType.TRADE
    assert records[0].sell_asset == "USDT"
    assert records[0].buy_asset == "USDC"


def test_pt_lone_airdrop_not_merged() -> None:
    # A genuine standalone airdrop (no opposite-sign counterpart) must stay an Airdrop.
    data_rows = _parse([_row("Airdrop", "G2", "OP", "50.0")], country="PT")
    record = data_rows[0].t_record
    assert record is not None
    assert record.t_type == TrType.AIRDROP


def test_uk_grouped_airdrop_not_merged() -> None:
    # Regression: in UK mode the grouped legs stay as separate Spend/Airdrop.
    data_rows = _parse(
        [
            _row("Airdrop", "G1", "USDT", "-100.0"),
            _row("Airdrop", "G1", "USDC", "99.9"),
        ],
        country="UK",
    )

    assert data_rows[0].t_record is not None and data_rows[0].t_record.t_type == TrType.SPEND
    assert data_rows[1].t_record is not None and data_rows[1].t_record.t_type == TrType.AIRDROP


def test_pt_airdrop_with_non_airdrop_sibling_not_merged() -> None:
    # An Airdrop sharing an action_data group with a non-Airdrop leg (e.g. a withdrawal) is not a
    # grouped conversion and must stay an Airdrop.
    data_rows = _parse(
        [
            _row("Airdrop", "G3", "OP", "50.0"),
            _row("Withdrawals", "G3", "USDT", "-50.0"),
        ],
        country="PT",
    )

    airdrop = data_rows[0].t_record
    assert airdrop is not None
    assert airdrop.t_type == TrType.AIRDROP
    assert data_rows[1].t_record is not None
    assert data_rows[1].t_record.t_type == TrType.WITHDRAWAL

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, List

import pytest

from bittytax.bt_types import TrType
from bittytax.config import config
from bittytax.conv import pt_mode
from bittytax.conv.datarow import DataRow
from bittytax.conv.out_record import TransactionOutRecord


def _dt(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2025, month, day, hour, minute, 0)


def _spend(
    asset: str, quantity: Decimal, ts: datetime, wallet: str = "Kraken", flag: bool = True
) -> DataRow:
    data_row = DataRow(1, [], [], "test")
    data_row.timestamp = ts
    data_row.t_record = TransactionOutRecord(
        TrType.SPEND, ts, sell_quantity=quantity, sell_asset=asset, wallet=wallet
    )
    data_row.t_record.pt_conversion = flag
    return data_row


def _airdrop(
    asset: str, quantity: Decimal, ts: datetime, wallet: str = "Kraken", flag: bool = True
) -> DataRow:
    data_row = DataRow(1, [], [], "test")
    data_row.timestamp = ts
    data_row.t_record = TransactionOutRecord(
        TrType.AIRDROP, ts, buy_quantity=quantity, buy_asset=asset, wallet=wallet
    )
    data_row.t_record.pt_conversion = flag
    return data_row


def test_is_pt_reflects_country() -> None:
    prev = config.config.get("country")
    try:
        config.config["country"] = "PT"
        assert pt_mode.is_pt() is True
        config.config["country"] = "UK"
        assert pt_mode.is_pt() is False
    finally:
        config.config["country"] = prev


def test_flag_conversion_is_noop_in_uk() -> None:
    prev = config.config.get("country")
    try:
        data_row = _spend("USDT", Decimal("100"), _dt(4, 1), flag=False)
        assert data_row.t_record is not None

        config.config["country"] = "UK"
        pt_mode.flag_conversion(data_row)
        assert data_row.t_record.pt_conversion is False

        config.config["country"] = "PT"
        pt_mode.flag_conversion(data_row)
        assert data_row.t_record.pt_conversion is True
    finally:
        config.config["country"] = prev


def test_pair_basic_different_asset() -> None:
    sent = [_spend("USDT", Decimal("110.39"), _dt(4, 1, 19, 44))]
    received = [_airdrop("USDC", Decimal("110.36"), _dt(4, 2, 8, 33))]
    pairs, leftovers = pt_mode.pair_conversion_legs(sent, received)
    assert len(pairs) == 1
    assert not leftovers


def test_pair_same_asset_not_paired() -> None:
    sent = [_spend("USDT", Decimal("100"), _dt(4, 1))]
    received = [_airdrop("USDT", Decimal("100"), _dt(4, 1))]  # same asset is not a conversion
    pairs, leftovers = pt_mode.pair_conversion_legs(sent, received)
    assert not pairs
    assert len(leftovers) == 2


def test_pair_outside_window_not_paired() -> None:
    sent = [_spend("USDT", Decimal("100"), _dt(1, 1))]
    received = [_airdrop("USDC", Decimal("100"), _dt(6, 1))]  # ~5 months apart
    pairs, leftovers = pt_mode.pair_conversion_legs(sent, received)
    assert not pairs
    assert len(leftovers) == 2


def test_pair_different_wallet_not_paired() -> None:
    sent = [_spend("USDT", Decimal("100"), _dt(4, 1), wallet="Kraken")]
    received = [_airdrop("USDC", Decimal("100"), _dt(4, 1), wallet="Binance")]
    pairs, _leftovers = pt_mode.pair_conversion_legs(sent, received)
    assert not pairs


def test_pair_quantity_tiebreak_for_simultaneous() -> None:
    # Two received candidates at the same instant; the quantity tiebreak must pick the 1:1 match.
    ts = _dt(4, 1, 12, 0)
    sent = [_spend("AAA", Decimal("100"), ts)]
    received = [
        _airdrop("BBB", Decimal("5"), ts),  # very different quantity
        _airdrop("CCC", Decimal("100"), ts),  # 1:1 match
    ]
    pairs, _leftovers = pt_mode.pair_conversion_legs(sent, received)
    assert len(pairs) == 1
    matched = pairs[0][1].t_record
    assert matched is not None
    assert matched.buy_asset == "CCC"


def test_merge_conversions_builds_trade() -> None:
    sent = _spend("USDT", Decimal("110.39461455"), _dt(4, 1, 19, 44))
    received = _airdrop("USDC", Decimal("110.36149621"), _dt(4, 2, 8, 33))
    data_files: List[Any] = [SimpleNamespace(data_rows=[sent, received])]
    pt_mode.merge_conversions(data_files)

    trade = sent.t_record
    assert trade is not None
    assert trade.t_type == TrType.TRADE
    assert trade.sell_asset == "USDT"
    assert trade.sell_quantity == Decimal("110.39461455")
    assert trade.buy_asset == "USDC"
    assert trade.buy_quantity == Decimal("110.36149621")
    assert trade.note == "Delisting conversion"
    assert received.t_record is None  # received leg folded into the Trade


def test_merge_conversions_unpaired_falls_back() -> None:
    sent = _spend("USDT", Decimal("100"), _dt(4, 1))
    data_files: List[Any] = [SimpleNamespace(data_rows=[sent])]
    pt_mode.merge_conversions(data_files)

    assert sent.t_record is not None
    assert sent.t_record.t_type == TrType.SPEND  # original record kept
    assert sent.t_record.pt_conversion is False  # flag cleared


def test_merge_conversions_ignores_unflagged_legs() -> None:
    # A genuine airdrop and an unrelated spend that are NOT flagged must never be merged.
    sent = _spend("USDT", Decimal("100"), _dt(4, 1), flag=False)
    received = _airdrop("USDC", Decimal("100"), _dt(4, 1), flag=False)
    data_files: List[Any] = [SimpleNamespace(data_rows=[sent, received])]
    pt_mode.merge_conversions(data_files)

    assert sent.t_record is not None and sent.t_record.t_type == TrType.SPEND
    assert received.t_record is not None and received.t_record.t_type == TrType.AIRDROP


def test_merge_conversions_emits_summary(capsys: pytest.CaptureFixture[str]) -> None:
    sent = _spend("USDT", Decimal("100"), _dt(4, 1))
    received = _airdrop("USDC", Decimal("100"), _dt(4, 1))
    data_files: List[Any] = [SimpleNamespace(data_rows=[sent, received])]
    pt_mode.merge_conversions(data_files)
    assert "merged 1 forced conversion" in capsys.readouterr().err


def test_low_confidence_pairing_by_time_warns_but_merges(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 28 days apart: inside the 30-day merge window but beyond the 7-day confidence window.
    sent = _spend("USDT", Decimal("100"), _dt(4, 1))
    received = _airdrop("USDC", Decimal("100"), _dt(4, 29))
    data_files: List[Any] = [SimpleNamespace(data_rows=[sent, received])]
    pt_mode.merge_conversions(data_files)

    assert "low-confidence" in capsys.readouterr().err
    assert sent.t_record is not None and sent.t_record.t_type == TrType.TRADE  # still merged


def test_low_confidence_pairing_by_quantity_warns_but_merges(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent = _spend("AAA", Decimal("100"), _dt(4, 1))
    received = _airdrop("BBB", Decimal("40"), _dt(4, 1))  # 60% quantity difference
    data_files: List[Any] = [SimpleNamespace(data_rows=[sent, received])]
    pt_mode.merge_conversions(data_files)

    assert "low-confidence" in capsys.readouterr().err
    assert sent.t_record is not None and sent.t_record.t_type == TrType.TRADE


def test_is_pt_is_case_insensitive() -> None:
    prev = config.config.get("country")
    try:
        for value in ("pt", "Pt", "PT"):
            config.config["country"] = value
            assert pt_mode.is_pt() is True
        config.config["country"] = "uk"
        assert pt_mode.is_pt() is False
    finally:
        config.config["country"] = prev


def test_both_sided_record_is_not_self_paired() -> None:
    # Defensive: a flagged record carrying both buy and sell must never pair with (and null) itself.
    data_row = DataRow(1, [], [], "test")
    data_row.timestamp = _dt(4, 1)
    data_row.t_record = TransactionOutRecord(
        TrType.TRADE,
        _dt(4, 1),
        buy_quantity=Decimal("1"),
        buy_asset="BTC",
        sell_quantity=Decimal("2"),
        sell_asset="ETH",
        wallet="W",
    )
    data_row.t_record.pt_conversion = True
    data_files: List[Any] = [SimpleNamespace(data_rows=[data_row])]
    pt_mode.merge_conversions(data_files)

    assert data_row.t_record is not None  # not nulled by a spurious self-merge
    assert data_row.t_record.t_type == TrType.TRADE

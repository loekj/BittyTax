# -*- coding: utf-8 -*-
# (c) Nano Nano Ltd 2022

import copy
import re
import sys
from decimal import Decimal
from typing import TYPE_CHECKING, Dict, List, Optional, Union

from colorama import Fore
from typing_extensions import Unpack

from ...bt_types import TrType, UnmappedType
from ...config import config
from ..dataparser import DataParser, ParserArgs, ParserType
from ..datarow import TxRawPos
from ..exceptions import DataRowError, UnexpectedTypeError
from ..out_record import TransactionOutRecord

if TYPE_CHECKING:
    from ..datarow import DataRow

KOINLY_D_MAPPING = {
    "": TrType.GIFT_RECEIVED,
    "Airdrop": TrType.AIRDROP,
    "airdrop": TrType.AIRDROP,
    "Fork": TrType.FORK,
    "fork": TrType.FORK,
    "Mining": TrType.MINING,
    "mining": TrType.MINING,
    "Reward": TrType.STAKING_REWARD,
    "reward": TrType.STAKING_REWARD,
    "Income": TrType.INCOME,
    "income": TrType.INCOME,
    "Other income": TrType.INCOME,
    "other_income": TrType.INCOME,
    "Lending interest": TrType.INTEREST,
    "lending_interest": TrType.INTEREST,
    "Cashback": TrType.CASHBACK,
    "cashback": TrType.CASHBACK,
    "Salary": TrType.INCOME,
    "salary": TrType.INCOME,
    "Fee refund": TrType.FEE_REBATE,
    "fee_refund": TrType.FEE_REBATE,
    "Loan": TrType.LOAN,
    "loan": TrType.LOAN,
    "Margin loan": TrType.LOAN,
    "margin_loan": TrType.LOAN,
    "Realized gain": TrType.MARGIN_GAIN,
    "realized_gain": TrType.MARGIN_GAIN,
}

KOINLY_W_MAPPING = {
    "": TrType.GIFT_SENT,
    "Gift": TrType.GIFT_SENT,
    "gift": TrType.GIFT_SENT,
    "Lost": TrType.LOST,
    "lost": TrType.LOST,
    "Donation": TrType.CHARITY_SENT,
    "donation": TrType.CHARITY_SENT,
    "Cost": TrType.SPEND,
    "cost": TrType.SPEND,
    "Loan fee": TrType.LOAN_INTEREST,
    "loan_fee": TrType.LOAN_INTEREST,
    "Margin fee": TrType.MARGIN_FEE,
    "margin_fee": TrType.MARGIN_FEE,
    "Loan repayment": TrType.LOAN_REPAYMENT,
    "loan_repayment": TrType.LOAN_REPAYMENT,
    "Margin repayment": TrType.LOAN_REPAYMENT,
    "margin_repayment": TrType.LOAN_REPAYMENT,
    "Realized gain": TrType.MARGIN_LOSS,
    "realized_gain": TrType.MARGIN_LOSS,
}


def parse_koinly(
    data_rows: List["DataRow"], parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    currency = parser.args[2].group(1)

    for row_index, data_row in enumerate(data_rows):
        if config.debug:
            if parser.in_header_row_num is None:
                raise RuntimeError("Missing in_header_row_num")

            sys.stderr.write(
                f"{Fore.YELLOW}conv: "
                f"row[{parser.in_header_row_num + data_row.line_num}] {data_row}\n"
            )

        if data_row.parsed:
            continue

        try:
            _parse_koinly_row(data_rows, parser, data_row, row_index, currency)
        except DataRowError as e:
            data_row.failure = e
        except (ValueError, ArithmeticError) as e:
            if config.debug:
                raise

            data_row.failure = e


def _parse_koinly_row(
    data_rows: List["DataRow"],
    parser: DataParser,
    data_row: "DataRow",
    row_index: int,
    currency: str,
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date"])
    data_row.tx_raw = TxRawPos(
        parser.in_header.index("TxHash"),
        parser.in_header.index("TxSrc"),
        parser.in_header.index("TxDest"),
    )
    data_row.parsed = True

    if "Label" in row_dict:
        row_dict["Tag"] = row_dict["Label"]

    if row_dict["Fee Amount"]:
        fee_quantity = Decimal(row_dict["Fee Amount"])
    else:
        fee_quantity = None

    if row_dict[f"Fee Value ({currency})"]:
        fee_value = DataParser.convert_currency(
            row_dict[f"Fee Value ({currency})"], currency, data_row.timestamp
        )
    else:
        fee_value = None

    net_value = DataParser.convert_currency(
        row_dict[f"Net Value ({currency})"], currency, data_row.timestamp
    )

    if row_dict["Type"] in ("buy", "sell", "exchange"):
        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Received Amount"]),
            buy_asset=row_dict["Received Currency"],
            buy_value=net_value,
            sell_quantity=Decimal(row_dict["Sent Amount"]),
            sell_asset=row_dict["Sent Currency"],
            sell_value=net_value,
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Sending Wallet"],
            note=row_dict["Description"],
        )
    elif row_dict["Type"] == "transfer":
        data_row.t_record = TransactionOutRecord(
            TrType.WITHDRAWAL,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict["Sent Amount"]),
            sell_asset=row_dict["Sent Currency"],
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Sending Wallet"],
            note=row_dict["Description"],
        )
        dup_data_row = copy.copy(data_row)
        dup_data_row.row = []
        dup_data_row.t_record = TransactionOutRecord(
            TrType.DEPOSIT,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Received Amount"]),
            buy_asset=row_dict["Received Currency"],
            wallet=row_dict["Receiving Wallet"],
            note=row_dict["Description"],
        )
        data_rows.insert(row_index + 1, dup_data_row)
    elif row_dict["Type"] in ("fiat_deposit", "crypto_deposit"):
        if row_dict["Tag"] in KOINLY_D_MAPPING:
            t_type: Union[TrType, UnmappedType] = KOINLY_D_MAPPING[row_dict["Tag"]]
        else:
            t_type = UnmappedType(f'_{row_dict["Tag"]}')

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            buy_quantity=Decimal(row_dict["Received Amount"]),
            buy_asset=row_dict["Received Currency"],
            buy_value=net_value,
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Receiving Wallet"],
            note=row_dict["Description"],
        )
    elif row_dict["Type"] in ("fiat_withdrawal", "crypto_withdrawal"):
        if row_dict["Tag"] in KOINLY_W_MAPPING:
            t_type = KOINLY_W_MAPPING[row_dict["Tag"]]
        else:
            t_type = UnmappedType(f'_{row_dict["Tag"]}')

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            sell_quantity=Decimal(row_dict["Sent Amount"]),
            sell_asset=row_dict["Sent Currency"],
            sell_value=net_value,
            fee_quantity=fee_quantity,
            fee_asset=row_dict["Fee Currency"],
            fee_value=fee_value,
            wallet=row_dict["Sending Wallet"],
            note=row_dict["Description"],
        )
    else:
        raise UnexpectedTypeError(parser.in_header.index("Type"), "Type", row_dict["Type"])


DataParser(
    ParserType.ACCOUNTING,
    "Koinly",
    [
        "Date",
        "Type",
        lambda h: h in ("Label", "Tag"),
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
        lambda h: re.match(r"Gain \((\w{3})\)", h),
        lambda h: re.match(r"Net Value \((\w{3})\)", h),
        lambda h: re.match(r"Fee Value \((\w{3})\)", h),
        "TxSrc",
        "TxDest",
        "TxHash",
        "Description",
    ],
    worksheet_name="Koinly",
    all_handler=parse_koinly,
)


# ──────────────────────────────────────────────────────────────────────────────
# Koinly "Bulk edit in Excel" transactions export (From/To column layout).
#
# Produced by Transactions page → ⋮ → "Bulk edit in Excel" → Export (Koinly help
# article 9490043). It exports ALL (filtered) transactions with its own read-only
# columns: Date (UTC), Type, Tag, From Amount/Currency/Wallet, To Amount/Currency/Wallet,
# Net Value (read-only) in Value Currency (read-only), etc. It is DISTINCT from the older
# "Koinly" tax-report export above (Sending/Receiving Wallet, Sent/Received Amount + Cost
# Basis + Gain) — BittyTax (fork AND upstream) only had that one, so this export was
# unrecognised. Direction is determined by which side is populated: To only = deposit,
# From only = withdrawal, both = exchange/trade. The Tag column carries Koinly labels, so
# KOINLY_D_MAPPING / KOINLY_W_MAPPING are reused (matched case-insensitively). Currency and
# wallet cells carry a ";<koinly_id>" suffix (e.g. "WFLR;9546698", "EUR;11") which is
# stripped — Koinly docs confirm the cell shows the symbol and the internal ID. Value
# falls back Net Value (read-only) → Net Worth Amount so a value is never silently lost.
# Untagged deposits/withdrawals are treated as transfers (DEPOSIT/WITHDRAWAL), NOT the old
# export's GIFT default (Koinly tags real income explicitly).
# ──────────────────────────────────────────────────────────────────────────────


def _koinly_strip_id(value: str) -> str:
    # Koinly suffixes exported currency/wallet cells with ";<internal id>"; keep the symbol/name.
    return value.split(";", 1)[0].strip() if value else value


def _koinly_tag_type(tag: str, mapping: Dict[str, TrType]) -> Optional[TrType]:
    # Map a Koinly Tag/Label to a TrType, CASE-INSENSITIVELY (Koinly varies label casing across
    # export versions, e.g. "Reward" vs "reward"). Returns None when the tag is unrecognised so the
    # caller can surface it as an UnmappedType for review rather than booking it as the wrong type.
    if tag in mapping:
        return mapping[tag]
    tag_lower = tag.lower()
    for key, value in mapping.items():
        if key.lower() == tag_lower:
            return value
    return None


def parse_koinly_universal(
    data_row: "DataRow", parser: DataParser, **_kwargs: Unpack[ParserArgs]
) -> None:
    row_dict = data_row.row_dict
    data_row.timestamp = DataParser.parse_timestamp(row_dict["Date (UTC)"])
    data_row.tx_raw = TxRawPos(
        parser.in_header.index("TxHash"),
        parser.in_header.index("TxSrc"),
        parser.in_header.index("TxDest"),
    )

    tag = row_dict["Tag"]

    from_amount = row_dict["From Amount"]
    from_currency = _koinly_strip_id(row_dict["From Currency"])
    to_amount = row_dict["To Amount"]
    to_currency = _koinly_strip_id(row_dict["To Currency"])

    # Fiat value of the transaction in the local currency. Prefer the Koinly-computed
    # "Net Value (read-only)" (present on API-synced data); fall back to the user-set
    # "Net Worth Amount" (manually-entered rows) so the value is never silently lost.
    def _val(amount: str, currency_raw: str) -> Optional[Decimal]:
        currency = _koinly_strip_id(currency_raw)
        if amount and currency and Decimal(amount) != 0:
            return DataParser.convert_currency(amount, currency, data_row.timestamp)
        return None

    net_value = _val(
        row_dict["Net Value (read-only)"], row_dict["Value Currency (read-only)"]
    ) or _val(row_dict["Net Worth Amount"], row_dict["Net Worth Currency"])
    fee_value = _val(
        row_dict["Fee Value (read-only)"], row_dict["Value Currency (read-only)"]
    ) or _val(row_dict["Fee Worth Amount"], row_dict["Fee Worth Currency"])

    if row_dict["Fee Amount"] and Decimal(row_dict["Fee Amount"]) != 0:
        fee_quantity = Decimal(row_dict["Fee Amount"])
        fee_asset = _koinly_strip_id(row_dict["Fee Currency"])
    else:
        fee_quantity = None
        fee_asset = ""

    has_from = bool(from_currency) and bool(from_amount) and Decimal(from_amount) != 0
    has_to = bool(to_currency) and bool(to_amount) and Decimal(to_amount) != 0

    if has_from and has_to:
        # Both sides populated = exchange/trade (sold From, bought To).
        data_row.t_record = TransactionOutRecord(
            TrType.TRADE,
            data_row.timestamp,
            buy_quantity=Decimal(to_amount),
            buy_asset=to_currency,
            buy_value=net_value,
            sell_quantity=Decimal(from_amount),
            sell_asset=from_currency,
            sell_value=net_value,
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            fee_value=fee_value,
            wallet=_koinly_strip_id(row_dict["To Wallet (read-only)"]),
            note=row_dict["Description"],
        )
    elif has_to:
        # Received only = deposit / reward / airdrop / income (Tag drives the TrType).
        # Untagged deposit = a plain transfer-IN in the universal export (Koinly tags real income
        # like reward/airdrop/mining explicitly), so default to DEPOSIT — NOT the old tax-report
        # export's GIFT_RECEIVED default, which would over-tax every untagged transfer-in as a gift.
        # A recognised tag maps case-insensitively; an unknown one is surfaced as UnmappedType.
        if tag == "":
            t_type: Union[TrType, UnmappedType] = TrType.DEPOSIT
        else:
            mapped_d = _koinly_tag_type(tag, KOINLY_D_MAPPING)
            t_type = mapped_d if mapped_d is not None else UnmappedType(f"_{tag}")

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            buy_quantity=Decimal(to_amount),
            buy_asset=to_currency,
            buy_value=net_value,
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            fee_value=fee_value,
            wallet=_koinly_strip_id(row_dict["To Wallet (read-only)"]),
            note=row_dict["Description"],
        )
    elif has_from:
        # Sent only = withdrawal / gift / cost / lost (Tag drives the TrType).
        # Untagged withdrawal = a plain transfer-OUT (see the deposit note above); default to
        # WITHDRAWAL, NOT the old export's GIFT_SENT. Recognised tags map case-insensitively;
        # an unknown one is surfaced as UnmappedType for review.
        if tag == "":
            t_type = TrType.WITHDRAWAL
        else:
            mapped_w = _koinly_tag_type(tag, KOINLY_W_MAPPING)
            t_type = mapped_w if mapped_w is not None else UnmappedType(f"_{tag}")

        data_row.t_record = TransactionOutRecord(
            t_type,
            data_row.timestamp,
            sell_quantity=Decimal(from_amount),
            sell_asset=from_currency,
            sell_value=net_value,
            fee_quantity=fee_quantity,
            fee_asset=fee_asset,
            fee_value=fee_value,
            wallet=_koinly_strip_id(row_dict["From Wallet (read-only)"]),
            note=row_dict["Description"],
        )
    else:
        raise UnexpectedTypeError(parser.in_header.index("Type"), "Type", row_dict["Type"])


DataParser(
    ParserType.ACCOUNTING,
    "Koinly",
    [
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
    ],
    # header_fixed=False (dynamic match): all of the above columns must be present IN ORDER,
    # but the file MAY carry extra columns. This parser was built from a single real export, so
    # tolerate Koinly appending/inserting columns in a future version (as happened to KuCoin's
    # "Account Mode") rather than silently reverting to "unrecognised". The 33 declared columns
    # are highly Koinly-specific (the "(read-only)" internal columns, "Synced To Accounting At",
    # Net Worth/Value pairs), so requiring them all keeps this from matching any other
    # exchange's CSV.
    header_fixed=False,
    worksheet_name="Koinly",
    row_handler=parse_koinly_universal,
)

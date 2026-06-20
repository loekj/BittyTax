# -*- coding: utf-8 -*-
# (c) Nano Nano Ltd 2019

import sys
from datetime import datetime, tzinfo
from decimal import Decimal
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

import dateutil.parser
import dateutil.tz
from colorama import Fore, Style
from typing_extensions import NotRequired, Protocol, TypedDict, Unpack

from ..bt_types import AssetSymbol, Timestamp
from ..config import config
from ..constants import TZ_UTC
from ..price.pricedata import PriceData
from . import venue_mode
from .exceptions import CurrencyConversionError

if TYPE_CHECKING:
    from parsers.defitaxes import DtConfig

    from ..datarow import DataRow

TERM_WIDTH = 69


class ParserType(Enum):
    WALLET = "Wallets"
    EXCHANGE = "Exchanges"
    SAVINGS = "Savings, Loans & Investments"
    EXPLORER = "Explorers"
    ACCOUNTING = "Accounting"
    SHARES = "Stocks & Shares"
    GENERIC = "Generic"


class ConsolidateType(Enum):
    NEVER = auto()
    HEADER_MATCH = auto()
    PARSER_MATCH = auto()  # Default


class RowHandler(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(
        self, data_row: "DataRow", parser: "DataParser", **kwargs: Unpack["ParserArgs"]
    ) -> None: ...


class RowHandler2(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(
        self, data_row: "DataRow", _parser: "DataParser", **kwargs: Unpack["ParserArgs"]
    ) -> None: ...


class AllHandler(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(
        self,
        data_rows: List["DataRow"],
        parser: "DataParser",
        **kwargs: Unpack[Union["ParserArgs"]],
    ) -> None: ...


class AllHandler2(Protocol):  # pylint: disable=too-few-public-methods
    def __call__(
        self,
        data_rows: List["DataRow"],
        _parser: "DataParser",
        **kwargs: Unpack[Union["ParserArgs"]],
    ) -> None: ...


class ParserArgs(TypedDict):  # pylint: disable=too-few-public-methods, too-many-ancestors
    filename: NotRequired[str]
    worksheet: NotRequired[str]
    unconfirmed: NotRequired[bool]
    cryptoasset: NotRequired[str]
    dt_config: NotRequired["DtConfig"]


class DataParser:  # pylint: disable=too-many-instance-attributes
    LIST_ORDER = (
        ParserType.WALLET,
        ParserType.EXCHANGE,
        ParserType.SAVINGS,
        ParserType.EXPLORER,
        ParserType.ACCOUNTING,
        ParserType.SHARES,
    )

    price_data = PriceData(config.data_source_fiat)
    parsers: List["DataParser"] = []

    def __init__(
        self,
        p_type: ParserType,
        name: str,
        header: List[Optional[Union[str, Callable]]],
        header_fixed: bool = True,
        delimiter: str = ",",
        worksheet_name: Optional[str] = None,
        deprecated: Optional["DataParser"] = None,
        row_handler: Optional[Union[RowHandler, RowHandler2]] = None,
        all_handler: Optional[Union[AllHandler, AllHandler2]] = None,
        consolidate_type: ConsolidateType = ConsolidateType.PARSER_MATCH,
        newest_first: bool = False,
    ):
        self.p_type = p_type
        self.name = name
        self.header = header
        self.header_fixed = header_fixed
        self.worksheet_name = worksheet_name if worksheet_name else name
        self.deprecated = deprecated
        self.delimiter = delimiter
        self.row_handler = row_handler
        self.all_handler = all_handler
        self.consolidate_type = consolidate_type
        self.args: List[Any] = []
        self.in_header = [col if col and not callable(col) else "" for col in self.header]
        self.in_header_row_num = 1
        self.newest_first = newest_first
        # Set only by venue-scoped projection (E2): file-column indices in declared order, used to
        # realign a drifted positional export. None for every normal (global / name-based) match.
        self.projection: Optional[List[int]] = None

        self.parsers.append(self)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DataParser):
            return NotImplemented
        return self.name.lower() == other.name.lower()

    def __ne__(self, other: object) -> bool:
        return not self == other

    def __lt__(self, other: "DataParser") -> bool:
        return self.name.lower() < other.name.lower()

    def format_header(self) -> str:
        header = []
        for col in self.header:
            if callable(col) or col is None:
                header.append("_")
            else:
                header.append(col)

        header_str = f"'{self.delimiter.join(header)}'"

        return f"{header_str[:TERM_WIDTH]}..." if len(header_str) > TERM_WIDTH else header_str

    @classmethod
    def parse_timestamp(
        cls,
        timestamp_str: Union[str, int, float],
        tzinfos: Optional[Dict[str, Optional[tzinfo]]] = None,
        tz: Optional[str] = None,
        dayfirst: bool = False,
        fuzzy: bool = False,
    ) -> datetime:
        if isinstance(timestamp_str, (int, float)):
            timestamp = datetime.fromtimestamp(timestamp_str, TZ_UTC)
        else:
            timestamp = dateutil.parser.parse(
                timestamp_str, tzinfos=tzinfos, dayfirst=dayfirst, fuzzy=fuzzy
            )

        if tz:
            timestamp = timestamp.replace(tzinfo=dateutil.tz.gettz(tz))
            timestamp = timestamp.astimezone(TZ_UTC)
        elif timestamp.tzinfo is None:
            # Default to UTC if no timezone is specified
            timestamp = timestamp.replace(tzinfo=TZ_UTC)
        else:
            timestamp = timestamp.astimezone(TZ_UTC)

        return timestamp

    @classmethod
    def convert_currency(
        cls, value: Optional[Union[Decimal, str]], from_currency: str, timestamp: datetime
    ) -> Optional[Decimal]:
        if from_currency not in config.fiat_list:
            return None

        if not value or value is None:
            return None

        if not Decimal(value):
            return Decimal(0)

        if config.ccy == from_currency:
            return Decimal(value)

        if timestamp.date() >= datetime.now().date():
            rate_record = cls.price_data.get_latest(AssetSymbol(from_currency), config.ccy)
            rate_ccy = rate_record.price_ccy
        else:
            rate_record = cls.price_data.get_historical(
                AssetSymbol(from_currency), config.ccy, Timestamp(timestamp)
            )
            rate_ccy = rate_record.price_ccy

        if rate_ccy is not None:
            value_in_ccy = Decimal(value) * rate_ccy

            if config.debug:
                sys.stderr.write(
                    f"{Fore.YELLOW}price: {timestamp:%Y-%m-%d}, 1 {from_currency}="
                    f"{config.sym()}{rate_ccy:0,.2f} {config.ccy}, "
                    f"{Decimal(value).normalize():0,f} {from_currency}="
                    f"{Style.BRIGHT}{config.sym()}{value_in_ccy:0,.2f} "
                    f"{config.ccy}{Style.NORMAL}\n"
                )

            return value_in_ccy
        raise CurrencyConversionError(from_currency, config.ccy, timestamp)

    @classmethod
    def match_header(
        cls, row: List[str], row_num: int, venue: Optional[str] = None
    ) -> "DataParser":
        row = [col.replace("\n", "").strip() for col in row]
        if config.debug:
            sys.stderr.write(
                f"{Fore.YELLOW}header: row[{row_num + 1}] TRY: {cls._format_row(row)}\n"
            )

        # Additive venue-scoped pass: runs ONLY when a venue is given, and on any miss falls through
        # to the unchanged global matchers below. So venue=None executes identical code to before.
        parser = cls._match_scoped(row, row_num, venue) if venue else None
        if not parser:
            parser = cls._match_fixed_header(row, row_num)
            if not parser:
                parser = cls._match_dynamic_header(row, row_num)
            if parser is not None:
                # global path never projects; clear any stale projection left on this singleton
                parser.projection = None

        if parser:
            if config.debug:
                sys.stderr.write(
                    f"{Fore.CYAN}header: row[{row_num + 1}] "
                    f"MATCHED: {cls._format_row(parser.header)} as '{parser.name}'\n"
                )
            return parser
        raise KeyError

    @classmethod
    def _match_scoped(cls, row: List[str], row_num: int, venue: str) -> Optional["DataParser"]:
        # Scoped, order-agnostic, drift-tolerant matching, used only when the venue is known.
        # Picks the most specific eligible parser within the venue; on genuine ambiguity it returns
        # None so match_header falls back to the global matchers, rather than guessing.
        candidates = venue_mode.scope_candidates(cls.parsers, venue)
        if not candidates:
            if config.debug:
                sys.stderr.write(
                    f"{Fore.BLUE}header: venue '{venue}' matched no parser group; "
                    f"falling back to global matching\n"
                )
            return None

        header_set = frozenset(row)
        # each entry: (matched_count, parser, ScanResult, projection|None) in registration order
        eligible: List[Tuple[int, "DataParser", venue_mode.ScanResult, Optional[List[int]]]] = []
        for parser in candidates:
            positional = venue_mode.is_positional(parser)
            if positional and not venue_mode.is_projectable(parser):
                continue  # strict positional (e.g. Coinbase) -> leave to exact matching
            result = venue_mode.scan(parser, row)
            if result.ambiguous or result.declared_count == 0:
                continue
            if positional:
                projection = venue_mode.build_projection(result)
                if projection is not None:
                    eligible.append((result.matched_count, parser, result, projection))
            elif venue_mode.is_eligible(parser, result, header_set):
                eligible.append((result.matched_count, parser, result, None))

        if not eligible:
            return None

        # Most declared columns matched = most specific format; stable sort keeps registration order
        # within ties. An exact top-tie is genuine ambiguity -> refuse (fall back).
        eligible.sort(key=lambda e: -e[0])
        if len(eligible) > 1 and eligible[0][0] == eligible[1][0]:
            if config.debug:
                sys.stderr.write(
                    f"{Fore.BLUE}header: row[{row_num + 1}] "
                    f"SCOPED AMBIGUOUS for venue '{venue}', falling back\n"
                )
            return None

        _, parser, result, projection = eligible[0]
        parser.args = list(result.args)
        parser.projection = projection
        parser.in_header = [row[i] for i in projection] if projection is not None else list(row)
        parser.in_header_row_num = row_num + 1
        return parser

    @classmethod
    def _match_fixed_header(cls, row: List[str], row_num: int) -> Optional["DataParser"]:
        parsers_reduced = [p for p in cls.parsers if len(p.header) == len(row) and p.header_fixed]

        for parser in parsers_reduced:
            parser.args = []
            match = False

            for i, row_field in enumerate(row):
                if callable(parser.header[i]):
                    match = parser.header[i](row_field)  # type: ignore[operator, misc]
                    parser.args.append(match)
                elif parser.header[i] is not None:
                    match = row_field == parser.header[i]

                if not match:
                    break

            if match:
                parser.in_header = row
                parser.in_header_row_num = row_num + 1
                return parser

            if config.debug:
                sys.stderr.write(
                    f"{Fore.BLUE}header: row[{row_num + 1}] "
                    f"NO MATCH: {cls._format_row(parser.header)} '{parser.name}'\n"
                )

        return None

    @classmethod
    def _match_dynamic_header(cls, row: List[str], row_num: int) -> Optional["DataParser"]:
        parsers_reduced = [
            p for p in cls.parsers if len(p.header) <= len(row) and not p.header_fixed
        ]

        for parser in parsers_reduced:
            parser.args = []
            match = False
            i = 0

            # All fields must exist in order, but don't have to be contiguous
            for header_field in parser.header:
                while i < len(row):
                    if callable(header_field):
                        match = header_field(row[i])
                        if match:
                            parser.args.append(match)
                    else:
                        match = row[i] == header_field

                    if match:
                        break
                    i += 1

                if not match:
                    break

            if match:
                parser.in_header = row
                parser.in_header_row_num = row_num + 1
                return parser

            if config.debug:
                sys.stderr.write(
                    f"{Fore.BLUE}header: row[{row_num + 1}] "
                    f"NO MATCH: {cls._format_row(parser.header)} '{parser.name}'\n"
                )

        return None

    @classmethod
    def venues(cls) -> List[str]:
        # Distinct parser names, so callers (e.g. tytle) can align their venue vocabulary with what
        # --venue scopes to: a venue string scopes to a parser when the parser name starts with it
        # (case/punctuation-insensitive), so "KuCoin" covers "KuCoin Trades", "KuCoin Deposits".
        return sorted({parser.name for parser in cls.parsers})

    @classmethod
    def format_parsers(cls) -> str:
        txt = ""
        for p_type in cls.LIST_ORDER:
            txt += f"  {p_type.value.upper()}:\n"
            prev_name = None
            for parser in sorted([parser for parser in cls.parsers if parser.p_type == p_type]):
                if parser.name != prev_name:
                    txt += f"    {parser.name}\n"
                txt += f"      {parser.format_header()}\n"

                prev_name = parser.name

        return txt

    @staticmethod
    def _format_row(row: List) -> str:
        row_out = []
        for col in row:
            if callable(col):
                row_out.append("<lambda>")
            elif col is None:
                row_out.append("*")
            else:
                row_out.append(f"'{col}'")

        return f"[{', '.join(row_out)}]"

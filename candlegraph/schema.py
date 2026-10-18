#!/usr/bin/env python3

import strawberry
from datetime import datetime
from psycopg2 import sql
from typing import List, Tuple
from candlegraph.dba import db_manager, chart_query, get_token_metadata
from candlegraph.tokens import supported_tokens
from candlegraph.helpers import format_chart_data


symbol_map = {v: k for k, v in supported_tokens.items()}

def fetch_chart_data(token_symbol: str, time_unit_in_hours: int):
    """
    Retrieves aggregated token data for a given symbol and time interval.
    Args:
        token_symbol (str): Symbol of the token.
        time_unit_in_hours (int): Time interval in hours for aggregating data.
    Returns:
        tuple: A tuple (status_code, data), where `status_code` is 0 if no data is found,
        and `data` contains the aggregated chart data.
    Description:
        This function queries aggregated historical data such as open, close, high,
        low, and average prices for a specified token over given time intervals.
    """

    interval = time_unit_in_hours * 3600
    token_id = symbol_map.get(token_symbol)
    if not token_id:
        return 0, []

    params = {"interval": interval, "token_id": token_id}
    return db_manager.execute_read_query(chart_query, params)


def fetch_token_metadata(token_symbol: str):
    """
    Retrieves token metadata associated with a give token
    Args:
        token_symbol (str): Symbol of the token.
    Returns:
        tuple: A tuple (status_code, data), where `status_code` is 0 if no data is found,
        and `data` contains the aggregated chart data.
    Description:
        This function queries aggregated historical data such as open, close, high,
        low, and average prices for a specified token over given time intervals.
    """
    token_id = symbol_map.get(token_symbol)
    if not token_id:
        return {}
    params = {"token_id": token_id}
    count, metadata = db_manager.execute_read_query(get_token_metadata, params)
    if count == 0:
        return {}

    return metadata[0]


@strawberry.type
class Candle:
    time: str
    priceType: str
    value: float


@strawberry.type
class TokenMetadata:
    id: str
    name: str
    symbol: str
    totalSupply: str
    volumeUsd: str
    decimals: str


@strawberry.type
class ChartData:
    tokenMetadata: TokenMetadata
    candles: List[List[Candle]]


@strawberry.type
class Query:
    @strawberry.field
    def get_chart_data(self, token_symbol: str, time_unit_in_hours: int) -> ChartData:
        """
        Provides token metadata and formatted chart data for a given token symbol and interval.
        Args:
            token_symbol (str): The symbol of the token.
            time_unit_in_hours (int): The time interval in hours for which data is aggregated.
        Returns:
            Tuple[TokenMetadata, List[List[Candle]]]: A tuple containing token metadata and a nested list of Candle objects formatted for charting purposes.
        Description:
            Retrieves and formats data for visual representation in charts, including token metadata, aggregating data into specified time intervals.
        """
        _, data = fetch_chart_data(token_symbol, time_unit_in_hours)
        if not data:
            formatted_data = [[["", ptype, 0.0] for ptype in ['open', 'close', 'high', 'low', 'priceUSD']]]
        else:
            formatted_data = format_chart_data(data)

        token_metadata = fetch_token_metadata(token_symbol)
        token_meta = TokenMetadata(
            id=token_metadata.get("id", ""),
            name=token_metadata.get("name", ""),
            symbol=token_metadata.get("symbol", ""),
            totalSupply=token_metadata.get("total_supply", ""),
            volumeUsd=token_metadata.get("volume_usd", ""),
            decimals=token_metadata.get("decimals", "")
        )

        return ChartData(
            tokenMetadata=token_meta,
            candles=[
                [Candle(time=point[0], priceType=point[1], value=float(point[2])) for point in group]
                for group in formatted_data
            ]
        )


chart_schema = strawberry.Schema(query=Query)

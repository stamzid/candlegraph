#!/usr/bin/env python3

import datetime
from typing import List


def add_symbol(data: List[dict], tokens: dict[str]):
    """
    Appends token symbols and reformats timestamps in a list of data entries.
    Args:
        data (List[dict]): List of data entries with token IDs and Unix timestamps.
        tokens (dict[str]): Mapping of token IDs to symbols.
    Description:
        For each entry, adds symbol and converts Unix timestamp to readable format.
    """
    for entry in data:
        entry_token_id = entry["id"].split("-")[0]
        entry["id"] = entry_token_id
        entry["symbol"] = tokens[entry_token_id]
        entry["timestamp"] = datetime.datetime.utcfromtimestamp(entry["periodStartUnix"]).strftime("%Y-%m-%dT%H:%M:%S")


def format_chart_data(data: List[dict]):
    """
    Transforms raw database query results into a structured format for charting.
    Args:
        data (List[dict]): Raw data from database queries containing pricing information.
    Returns:
        List[List[tuple]]: Formatted data grouped by price type, each entry as (time, price type, value).
    Description:
        Converts database results into a nested list where each sublist corresponds
        to price types such as open, close, high, low, and average USD price, formatted for chart display.
    """
    opens = []
    closes = []
    highs = []
    lows = []
    price_usd = []

    for record in data:
        time_str = record['interval_start'].strftime('%Y-%m-%dT%H:%M:%S')
        opens.append([time_str, "open", record['opens'][0] if record['opens'] else 0])
        closes.append([time_str, "close", record['closes'][0] if record['closes'] else 0])
        highs.append([time_str, "high", record.get('max_high', 0)])
        lows.append([time_str, "low", record.get('min_low', 0)])
        price_usd.append([time_str, "priceUSD", record.get('avg_price_usd', 0)])

    formatted_data = [opens, closes, highs, lows, price_usd]
    return formatted_data

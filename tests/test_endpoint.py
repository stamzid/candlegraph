#!/usr/bin/env python3

# tests/test_api.py

import pytest
from typing import List
from gql import Client, gql
from gql.transport.aiohttp import AIOHTTPTransport


query = gql("""
    query getChartData($tokenSymbol: String!, $timeUnitInHours: Int!) {
        getChartData(tokenSymbol: $tokenSymbol, timeUnitInHours: $timeUnitInHours) {
            tokenMetadata {
                id
                name
                symbol
                totalSupply
                volumeUsd
                decimals
            }
            candles {
                time
                priceType
                value
            }
        }
    }
""")


@pytest.mark.asyncio
async def test_get_chart_data():
    transport = AIOHTTPTransport(url="http://localhost:8000/graphql")
    client = Client(transport=transport, fetch_schema_from_transport=True)

    response = await client.execute_async(query, variable_values={"tokenSymbol": "WBTC", "timeUnitInHours": 2})

    assert 'getChartData' in response
    assert isinstance(response['getChartData'], dict)
    assert isinstance(response['getChartData']['candles'], list)


@pytest.mark.asyncio
async def test_get_chart_data_empty():
    transport = AIOHTTPTransport(url="http://localhost:8000/graphql")
    client = Client(transport=transport, fetch_schema_from_transport=True)

    variables = {
        "tokenSymbol": "EMPTYTOKEN",
        "timeUnitInHours": 1
    }

    response = await client.execute_async(query, variable_values=variables)

    assert 'getChartData' in response
    assert response['getChartData']['tokenMetadata']['id'] == ""
    assert isinstance(response['getChartData']['candles'], list)
    assert len(response['getChartData']['candles']) == 1
    assert response['getChartData']['candles'][0][0]['value'] == 0

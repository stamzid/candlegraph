#!/usr/bin/env python3

import json
import time
import backoff
import datetime

from gql import gql, Client
from gql.transport.aiohttp import AIOHTTPTransport
from candlegraph.settings import TRANSPORT_URL
from candlegraph.utils.logging_utils import service_logger
from candlegraph.dba import DatabaseManager, insert_token_hour_sql, insert_token_sql
from candlegraph.helpers import add_symbol


class SubgraphClient:
    def __init__(self, dba: DatabaseManager):
        """
        Initializes the SubgraphClient with a database manager and a GraphQL client.
        Args:
            dba (DatabaseManager): A database manager instance to handle database operations.
        """
        transport = AIOHTTPTransport(url=TRANSPORT_URL)
        self.client = Client(transport=transport, fetch_schema_from_transport=True)
        self.dba = dba

    @backoff.on_exception(backoff.expo, (KeyError, Exception), max_tries=10, max_time=10)
    def fetch_token_hour_datas(self, timestamps: dict[int], tokens: dict[str]):
        """
        Fetches and stores hourly token data for given tokens from a GraphQL API.
        Args:
            timestamps (dict[int]): A dictionary mapping token IDs to their last processed timestamps.
            tokens (dict[str]): A dictionary mapping token IDs to token symbols.
        Returns:
            dict[int]: Updated dictionary of timestamps after fetching new data.
        Description:
            This method queries a GraphQL API to retrieve hourly data for each token and updates the database.
            It handles retries and backoff in case of errors or incomplete data.
        """
        for token_id in timestamps:
            current_timestamp = timestamps[token_id]
            while True:
                query = gql(f"""
                    {{
                        tokenHourDatas(first: 100, where: {{token: "{token_id}", periodStartUnix_gt: {current_timestamp}}}) {{
                            id
                            periodStartUnix
                            open
                            close
                            high
                            low
                            priceUSD
                        }}
                    }}
                """)

                try:
                    response = self.client.execute(query)
                    data = response.get('tokenHourDatas', [])
                    if not data:
                        break

                    new_timestamp = max(data, key=lambda x: x['periodStartUnix'])['periodStartUnix']

                    # Ensure to update the timestamp only if new data was fetched
                    if new_timestamp > current_timestamp:
                        timestamps[token_id] = new_timestamp
                        current_timestamp = new_timestamp

                    add_symbol(data, tokens)
                    self.dba.execute_batch_insert(insert_token_hour_sql, data)
                except KeyError as e:
                    service_logger.error("Error processing gql response %s", e)
                except Exception as e:
                    service_logger.error(e)

        return timestamps

    @backoff.on_exception(backoff.expo, (Exception), max_tries=10, max_time=10)
    def fetch_token(self, tokens: dict):
        """
        Fetches and updates token metadata in the database.
        Args:
            tokens (dict): A dictionary of tokens with their IDs to fetch detailed data.
        Description:
            Retrieves token information such as name, symbol, total supply, etc., from the GraphQL API
            and updates the database using batch insert. The process retries on failure.
        """
        token_ids = list(tokens.keys())
        token_ids_json = json.dumps(token_ids)

        # Construct the GraphQL query
        query = gql(f"""
            {{
                tokens(where: {{id_in: {token_ids_json}}}) {{
                    id,
                    name,
                    symbol,
                    totalSupply,
                    volumeUSD,
                    decimals
                }}
            }}
        """)

        try:
            response = self.client.execute(query)
            data = response.get('tokens', [])
            self.dba.execute_batch_insert(insert_token_sql, data)
        except Exception as e:
            service_logger.error(e)

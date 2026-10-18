#!/usr/bin/env python3

import os
import psycopg2
from psycopg2 import pool, sql
from psycopg2.extras import DictCursor
import traceback
from contextlib import contextmanager
from candlegraph.utils.logging_utils import service_logger
from candlegraph.settings import MIN_CONN, MAX_CONN

import candlegraph.settings as Settings


insert_token_hour_sql = sql.SQL("""INSERT INTO candlegraph.token_hours_data
(
    token_id,
    symbol,
    open,
    high,
    low,
    close,
    price_usd,
    period_start_unix,
    timestamp
) VALUES (%(id)s,%(symbol)s,%(open)s,%(high)s,%(low)s,%(close)s,%(priceUSD)s,%(periodStartUnix)s,%(timestamp)s) RETURNING id;""")


insert_token_sql = sql.SQL("""INSERT INTO candlegraph.token
(
    id,
    name,
    symbol,
    total_supply,
    volume_usd,
    decimals
) VALUES (%(id)s,%(name)s, %(symbol)s,%(totalSupply)s,%(volumeUSD)s,%(decimals)s) ON CONFLICT (id)
DO UPDATE SET
    total_supply = EXCLUDED.total_supply,
    volume_usd = EXCLUDED.volume_usd,
    decimals = EXCLUDED.decimals
RETURNING id;""")

get_token_metadata = sql.SQL("""SELECT * from candlegraph.token WHERE id =%(token_id)s""")
get_latest_timestamp = sql.SQL("""
SELECT
    token_id,
    MAX(period_start_unix) AS latest_unix
FROM
    candlegraph.token_hours_data
GROUP BY token_id;
""")


chart_query = sql.SQL("""
    SELECT
        MIN(timestamp) AS interval_start,
        array_agg(open ORDER BY timestamp) AS opens,
        array_agg(close ORDER BY timestamp DESC) AS closes,
        MAX(high) AS max_high,
        MIN(low) AS min_low,
        AVG(price_usd) AS avg_price_usd
    FROM
        candlegraph.token_hours_data
    WHERE
        token_id = %(token_id)s
    GROUP BY
        FLOOR(EXTRACT(EPOCH FROM timestamp) / %(interval)s)
    ORDER BY
        interval_start
""")

delete_older_data = sql.SQL("""DELETE FROM candlegraph.token_hours_data WHERE period_start_unix < %(interval_start)s""")


# Singleton pattern implemented to ensure one instance of the class used throughout
class DatabaseManager:
    """
    Manages database connections and operations. Uses a singleton pattern to ensure only one instance exists.
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, host, port, user, password, db, schema):
        """
        Sets up the database connection using provided credentials and database information.
        """
        if not hasattr(self, 'initialized'):
            self.DB_HOST = host
            self.DB_PORT = port
            self.DB_USER = user
            self.DB_PASS = password
            self.DB_NAME = db
            self.DB_SCHEMA = schema
            self.connection_pool = self.init_connection_pool()
            self.initialized = True

    def init_connection_pool(self):
        """
        Initializes a pool of database connections.
        """
        return pool.ThreadedConnectionPool(
            minconn=MIN_CONN,
            maxconn=MAX_CONN,
            user=self.DB_USER,
            password=self.DB_PASS,
            host=self.DB_HOST,
            port=self.DB_PORT,
            database=self.DB_NAME
        )

    @contextmanager
    def get_db_connection(self):
        """
        Provides a database connection from the pool. Ensures the connection is returned to the pool afterward.
        """
        connection = self.connection_pool.getconn()
        try:
            yield connection
        finally:
            self.connection_pool.putconn(connection)

    @contextmanager
    def get_db_cursor(self, commit=False):
        """
        Provides a database cursor for executing queries. Commits changes if `commit` is True.
        """
        with self.get_db_connection() as connection:
            cursor = connection.cursor(cursor_factory=DictCursor)
            try:
                yield cursor
                if commit:
                    connection.commit()
            except Exception as e:
                connection.rollback()
                raise e
            finally:
                cursor.close()

    def execute_write_query(self, query, record):
        """
        Executes a write query and returns the number of affected rows and any generated ID.
        """
        count = 0
        return_id = ""
        try:
            with self.get_db_cursor(commit=True) as cursor:
                cursor.execute(query, record)
                return_id = cursor.fetchone()[0] if cursor.description else None
                count = cursor.rowcount
        except Exception as e:
            traceback.print_exc()
            service_logger.error(e)

        return count, return_id

    def execute_read_query(self, query, record):
        """
        Executes a read query with parameters and returns the result as a list of dictionaries.
        """
        count = 0
        data = []
        try:
            with self.get_db_cursor() as cursor:
                cursor.execute(query, record)
                count = cursor.rowcount
                data = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            traceback.print_exc()
            service_logger.error(e)

        return count, data

    def execute_read_without_condition(self, query):
        """
        Executes a read query without any conditions and returns all results as a list of dictionaries.
        """
        count = 0
        data = []
        try:
            with self.get_db_cursor() as cursor:
                cursor.execute(query)
                count = cursor.rowcount
                data = [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            traceback.print_exc()
            service_logger.error(e)
        return count, data

    def execute_batch_insert(self, query, records):
        """
        Executes a batch insert query, inserting multiple records at once.
        """
        count = 0
        try:
            with self.get_db_cursor(commit=True) as cursor:
                cursor.executemany(query, records)
                count = cursor.rowcount
        except Exception as e:
            traceback.print_exc()
            service_logger.error(e)

        return count, None


db_manager = DatabaseManager(Settings.DB_HOST, Settings.DB_PORT, Settings.DB_USER, Settings.DB_PASS, Settings.DB_NAME, Settings.DB_SCHEMA)

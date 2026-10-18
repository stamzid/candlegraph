#!/usr/bin/env python3

import threading
import uvicorn
from fastapi import FastAPI
from strawberry.fastapi import GraphQLRouter
from candlegraph.schema import chart_schema
from candlegraph.subgraph_client import SubgraphClient
from candlegraph.dba import db_manager,  get_latest_timestamp, delete_older_data
from candlegraph.tokens import supported_tokens
from candlegraph.utils.logging_utils import service_logger
from candlegraph.settings import LOOKBACK_DAYS, DATA_POLL_INTERVAL
from datetime import datetime, timedelta
from candlegraph.settings import PERSISTANCE_MODE
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI()

@app.get("/status")
def status():
    return {"status": "Server is running and data is ready."}


graphql_app = GraphQLRouter(chart_schema)
app.include_router(graphql_app, prefix="/graphql")


def data_load(initial: bool = False):
    """
    Loads initial data and starts polling operations for token data updates.
    Description:
        Initializes the GraphQL client, fetches the latest timestamps for tokens, and starts
        fetching and storing token data continuously.
    """
    client = SubgraphClient(db_manager)
    _, ts_data = db_manager.execute_read_without_condition(get_latest_timestamp)

    start_time = int(datetime.timestamp(datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)))
    timestamps = {d["token_id"]: d["latest_unix"] for d in ts_data}
    for token_id in supported_tokens:
        if token_id not in timestamps:
            timestamps[token_id] = start_time

    client.fetch_token(supported_tokens)
    client.fetch_token_hour_datas(timestamps, supported_tokens)
    if PERSISTANCE_MODE is None and initial is False:
        service_logger.info("Deleting data older than %s days", LOOKBACK_DAYS)
        params = {"interval_start": start_time}
        db_manager.execute_write_query(delete_older_data, params)


def start_scheduler():
    """
    Starts the scheduler to run data_load every DATA_POLL_INTERVAL seconds.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: data_load(False), 'interval', seconds=DATA_POLL_INTERVAL)
    scheduler.start()


if __name__ == "__main__":
    """
    Main execution block to start the server and handle shutdown on KeyboardInterrupt.
    """
    try:
        service_logger.info("Populating Initial Data")
        data_load(True)
        service_logger.info("Starting scheduler...")
        start_scheduler()
        service_logger.info("Starting server...")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except KeyboardInterrupt:
        service_logger.warning("Shutting down application due to KeyboardInterrupt")

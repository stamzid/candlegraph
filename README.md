# Overview
This document provides instructions on how to run the application using Docker, and how to query the GraphQL API to fetch chart data for cryptocurrency tokens.

## Prerequisites
* Docker
* docker-compose
* poetry (optional)
* python 3.11 (optional)

Having poetry python will allow the user to run some unittest and gql test incase they don't have the gql clients

# Setup and Running
## Starting the Application with docker
Run the application using Docker Compose:

```sh
docker-compose up -d
```

This command will start all required services in detached mode. In case the user wants to run the program from command line, then start the database first and then run the shell commands in project root. For example:
```sh
docker-compose up -d postgres
poetry shell
poetry install
alembic upgrade head
python -m candlegraph.app
```

## DB Schema Management

As it can be seen, the database schema is being managed by `alembic`. It's a seamless integration with sqlalchemy and creating / dropping DB schema. More info can be found here: https://alembic.sqlalchemy.org/en/latest/tutorial.html

```sh
# To add a migration:
alembic revision -m "migration_name"

# To get to latest schema
alembic upgrade head

# To downgrade a migration
alembic downgrade -1
```

As usual this needs to be run from project root as alembic is part of `pyproject.toml`
## Supported Tokens

At the moment tokens are added to the `candlegraph/tokens.py` file in a dictionary with their `token_id` and `symbol`. If the user wants to add support more tokens, simply add the `token_id` and `symbol` to the existing dictionary and rebuild the docker image. For example:
```sh
docker build . -t candlegraph:latest
```
## Environment Variables

> **Note on credentials:** the database user/password `candlegraph`/`candlegraph` in `docker-compose.yaml`, `candlegraph/settings.py` and `alembic.ini` are throwaway defaults for the local Docker Postgres only. They are not real secrets. For anything beyond local development, override them with environment variables; see `.env.example` for the full list.

All environment variables are using default values at the moment. Most of them are self explanatory (i.e. DB variables). Following contains the significance of less obvious ones:

* MIN_CONN: Minimum number of connections in DB connection pool.
* MAX_CONN: Maximum number of connections in DB connection pool.
* LOOKBACK_DAYS: How many days of data to include in query, default is 7 days.
* DATA_POLL_INTERVAL: How often data is being polled, at this moment it's every 5 minutes.
* PERSISTANCE_MODE: Whether to keep or delete the data older than DATA_POLL_INTERVAL. Default is "DELETE", any other value will persist the data.

## Querying the GraphQL API
You can access the GraphQL API to fetch data once the application is up. Here is how you can query chart data for the "Wrapped Bitcoin (WBTC)" token over specified time intervals.

### Example GraphQL Query
Use a GraphQL client or a tool like Postman or GraphiQL to execute the following query:

```graphql
query GetChartData {
  getChartData(tokenSymbol: "WBTC", timeUnitInHours: 2) {
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
```


### Example Responses

#### When Data is Available
If there is data available for the WBTC token, the response might look like this:

```json
{
  "data": {
    "getChartData": {
      "tokenMetadata": {
        "id": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
        "name": "Wrapped BTC",
        "symbol": "WBTC",
        "totalSupply": "18700000",
        "volumeUsd": "7200000000",
        "decimals": "8"
      },
      "candles": [
        [
          {
            "time": "2024-01-01T00:00:00",
            "priceType": "open",
            "value": 50000
          },
          {
            "time": "2024-01-01T02:00:00",
            "priceType": "close",
            "value": 50500
          },
          {
            "time": "2024-01-01T02:00:00",
            "priceType": "high",
            "value": 51000
          },
          {
            "time": "2024-01-01T00:00:00",
            "priceType": "low",
            "value": 49500
          },
          {
            "time": "2024-01-01T01:00:00",
            "priceType": "priceUSD",
            "value": 50250
          }
        ]
      ]
    }
  }
}
```

#### When No Data is Available
If no data is available for the specified interval, the response would look like this:

```json
{
    "data": {
        "getChartData": {
            "tokenMetadata": {
                "id": "",
                "name": "",
                "symbol": "",
                "totalSupply": "",
                "volumeUsd": "",
                "decimals": ""
            },
            "candles": [
                [
                    {
                        "time": "",
                        "priceType": "open",
                        "value": 0
                    },
                    {
                        "time": "",
                        "priceType": "close",
                        "value": 0
                    },
                    {
                        "time": "",
                        "priceType": "high",
                        "value": 0
                    },
                    {
                        "time": "",
                        "priceType": "low",
                        "value": 0
                    },
                    {
                        "time": "",
                        "priceType": "priceUSD",
                        "value": 0
                    }
                ]
            ]
        }
    }
}
```

This documentation ensures you have a clear understanding of how to start the application and interact with the GraphQL API, including handling scenarios where no data is available.

## Testing

Testing the application involves both unit tests for isolated components and end-to-end tests to verify the overall functionality. Unittests are focusing on function without external dependencies. Due to the nature of the program, there's only a handful of such functions.

### Prerequisites for Testing
Ensure that all dependencies are installed and that you are operating within the virtual environment managed by Poetry:

```sh
# Activate the Poetry shell
poetry shell
```

### Install dependencies
```sh
poetry install
```

### Running Unit Tests
Unit tests are designed to test isolated helper functions without requiring access to the database or external APIs:

```sh
# Run unit tests
python -m unittest tests.test_helpers
```
### Running End-to-End Tests

End-to-end tests require that the application be running within the Docker environment with docker compose. Ensure both application and db container is up and running. These tests check the full functionality from the GraphQL endpoint through to the database by leveraging pytest:

```sh
# Ensure the application is running
docker-compose ps

# Run end-to-end tests
poetry run pytest tests/test_endpoint.py
```

These tests will validate the integration between various components and ensure that the system behaves as expected.


## Design Decisions
### Language and Database

* Python: one of my primary languages (the other being Scala), with mature libraries for GraphQL clients and Postgres.
* PostgreSQL: The data we're dealing with is structured and python has great ORM framework and library for Postgres. Also provides fast read access for complex queries

### Frameworks

* FastAPI: I chose this mainly for asynchronouse request handling and it runs perfectly with ASGI web server implementation. It's also highly performant compared to other frameworks I used such as Flask and Django.
* Strawberry GraphQL: Main reason is it is the recommeded graphQL framework for FastAPI with seamless integration and simplifies creating strongly-typed APIs in a Pythonic way. 

### Virtual Environment and Packaging
* Poetry: Handles dependency management and packaging. It simplifies the management of project dependencies and virtual environments.
* Docker: Ease of containerization and power of docker compose for local developments. The images are platform independent and one of the mainstays of CI/CD practices.

### Testing
Mainly leveraged popular python testing libraries for both unittest and integration testing.

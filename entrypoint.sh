#!/bin/bash

alembic upgrade head
python -m candlegraph.app

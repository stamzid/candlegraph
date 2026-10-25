"""Ingestion: pulling market data from a source and writing it to storage."""

from candlegraph.ingestion.quality import QualityReport, build_report, find_gaps, format_report
from candlegraph.ingestion.runner import IngestResult, ingest_once, run_ingest
from candlegraph.ingestion.subgraph import SubgraphClient, SubgraphError
from candlegraph.ingestion.worker import run_worker

__all__ = [
    "IngestResult",
    "QualityReport",
    "SubgraphClient",
    "SubgraphError",
    "build_report",
    "find_gaps",
    "format_report",
    "ingest_once",
    "run_ingest",
    "run_worker",
]

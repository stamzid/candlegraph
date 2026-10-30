"""The knowledge layer: entities, documents, and what cites them (stage 1).

Grows step by step with ``docs/rollout/ROLLOUT.md``. So far: the schema
(``statements.py``) and the asset sync (``assets.py``).
"""

from candlegraph.knowledge.assets import asset_rows, sync_assets
from candlegraph.knowledge.statements import KnowledgeStatements, knowledge_statements_for

__all__ = ["KnowledgeStatements", "asset_rows", "knowledge_statements_for", "sync_assets"]

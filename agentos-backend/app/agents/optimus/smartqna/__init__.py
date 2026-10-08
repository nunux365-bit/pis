"""SmartQnA — RAG-based Q&A with CRAG pipeline and adaptive retrieval."""

from app.agents.optimus.smartqna.agent import answer_question
from app.agents.optimus.smartqna.retriever import AdaptiveRetriever
from app.agents.optimus.smartqna.ingestion import ingest_document, ingest_documents_from_directory

__all__ = ["answer_question", "AdaptiveRetriever", "ingest_document", "ingest_documents_from_directory"]

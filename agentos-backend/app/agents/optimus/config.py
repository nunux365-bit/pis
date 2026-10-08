"""Local settings wrapper for Optimus agent — re-exports from app.config.settings."""

from app.config.settings import settings

# Feature flags
OPTIMUS_ENABLED = settings.optimus_enabled
SMARTQNA_ENABLED = settings.optimus_smartqna_enabled
PROSIGHT_ENABLED = settings.optimus_prosight_enabled
QUICKML_ENABLED = settings.optimus_quickml_enabled
TEXTTOWORKFLOW_ENABLED = settings.optimus_texttoworkflow_enabled
FLOCK_ENABLED = settings.optimus_flock_enabled
LLM_USAGE_LOGGING_ENABLED = settings.optimus_llm_usage_logging_enabled

# Paths
DOCUMENTS_DIR = settings.optimus_documents_dir

# LLM
OPENAI_MODEL = settings.optimus_openai_model
# Use Optimus-specific API key if set, otherwise fall back to generic key
OPENAI_API_KEY = settings.optimus_openai_api_key or settings.openai_api_key

# Embeddings (OpenAI API-based)
EMBEDDING_MODEL = settings.optimus_embedding_model
EMBEDDING_DIMENSIONS = settings.optimus_embedding_dimensions

# Reranker (LLM-based using GPT-4o Mini for CRAG scoring)
RERANKER_MODEL = settings.optimus_reranker_model

# CRAG thresholds
CRAG_SUFFICIENT_THRESHOLD = settings.optimus_crag_sufficient_threshold
CRAG_PARTIAL_THRESHOLD = settings.optimus_crag_partial_threshold
CRAG_FILTER_MIN_SCORE = settings.optimus_crag_filter_min_score
CRAG_DROP_GAP = settings.optimus_crag_drop_gap

# Retrieval
VECTOR_SEARCH_CANDIDATES = settings.optimus_vector_search_candidates

# Reranking skip thresholds (skip LLM reranking if top vector score exceeds threshold)
RERANK_SKIP_THRESHOLD_SIMPLE = settings.optimus_rerank_skip_threshold_simple
RERANK_SKIP_THRESHOLD_COMPLEX = settings.optimus_rerank_skip_threshold_complex

# API resilience settings (staging performance optimization)
EMBEDDING_TIMEOUT_SECONDS = settings.optimus_embedding_timeout_seconds
EMBEDDING_MAX_RETRIES = settings.optimus_embedding_max_retries
REQUEST_TIMEOUT_SECONDS = settings.optimus_request_timeout_seconds
FLOCK_MAX_CONCURRENT_HANDLERS = settings.optimus_flock_max_concurrent_handlers
QUERY_EXPANSION_CACHE_TTL_SECONDS = settings.optimus_query_expansion_cache_ttl_seconds

# Session / conversation
SESSION_TTL_HOURS = settings.optimus_session_ttl_hours
SESSION_MAX_TURNS = settings.optimus_session_max_turns

# Ingestion timeout (auto-mark stuck documents as FAILED)
INGESTION_TIMEOUT_MINUTES = settings.optimus_ingestion_timeout_minutes

# Qdrant
QDRANT_COLLECTION = settings.qdrant_collection_optimus

# Flock integration
FLOCK_APP_ID = settings.optimus_flock_app_id
FLOCK_APP_SECRET = settings.optimus_flock_app_secret
FLOCK_BOT_TOKEN = settings.optimus_flock_bot_token
FLOCK_VERIFICATION_TOKEN = settings.optimus_flock_verification_token
# Support/issue form URL shown in Flock "raise a request" pointers.
FLOCK_SUPPORT_FORM_URL = settings.optimus_flock_support_form_url

# Full Optimus web app URL shared in responses and Flock welcome messages.
WEB_APP_URL = settings.optimus_web_app_url

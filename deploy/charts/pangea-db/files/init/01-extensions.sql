-- ============================================================================
-- 01 - Extensions
-- Pangea Energy and Power :: PostgreSQL 15
-- License note: pgvector is PostgreSQL-Licensed (permissive). No TimescaleDB.
-- ============================================================================

-- Vector similarity search for the conversational-AI knowledge base (RAG).
-- NOTE: the pgvector extension is named "vector", not "pgvector".
CREATE EXTENSION IF NOT EXISTS vector;

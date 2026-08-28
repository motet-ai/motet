-- Enable pgvector on first boot of a Compose Postgres volume.
-- Tables are created by migrate-pgvector / PGVectorStore, not here.
CREATE EXTENSION IF NOT EXISTS vector;

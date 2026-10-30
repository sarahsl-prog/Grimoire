# ⚠️ Unsupported Prototypes

The files in this directory are **drifted, unsupported prototypes**. They are not part of the main Grimoire application and are provided only for reference.

**Do not use in production.**

- `app.py` — Streamlit demo that loads documents into a FAISS vector store.
- `rag_pipeline.py` — LangChain-based RAG pipeline using FAISS and Ollama. Uses `FAISS.load_local(..., allow_dangerous_deserialization=True)`, which bypasses security controls.

These prototypes predate the production architecture (ChromaDB, FastAPI/MCP, Postgres, structured auth) and are kept here only for historical context.

# Grimoire

A multi-document RAG (Retrieval-Augmented Generation) assistant that lets you upload PDF and TXT files, then ask natural language questions answered using only the content of your documents. Runs entirely locally with no API keys required.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Streamlit |
| LLM Orchestration | LangChain |
| Embeddings | HuggingFace (`sentence-transformers/all-MiniLM-L6-v2`) |
| Vector Store | FAISS |
| LLM | Ollama (`llama3`) |
| Document Parsing | PyPDF, LangChain TextLoader |

## How It Works

1. **Upload** -- Drop PDF or TXT files through the Streamlit UI
2. **Index** -- Documents are chunked (800 chars, 150 overlap), embedded, and stored in a FAISS vector index
3. **Query** -- Your question is embedded and matched against the index to retrieve the top 4 most relevant chunks
4. **Answer** -- The retrieved context is sent to a local llama3 model via Ollama, which generates an answer grounded only in your documents

New uploads are merged into the existing index, so you can add documents incrementally.

## Prerequisites

- **Python 3.10+**
- **Ollama** installed and running with the `llama3` model pulled:
  ```bash
  ollama pull llama3
  ```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install streamlit langchain langchain-community langchain-huggingface langchain-ollama langchain-text-splitters faiss-cpu pypdf
```

## Usage

```bash
streamlit run app.py
```

Then open the displayed URL (default `http://localhost:8501`), upload your documents, and start asking questions.

## Project Structure

```
app.py             # Streamlit UI -- file upload, query input, result display
rag_pipeline.py    # RAG logic -- document loading, chunking, embedding, retrieval, LLM call
documents/         # Uploaded files (created at runtime)
vectorstore/       # Persisted FAISS index (created at runtime)
```

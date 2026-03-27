# app.py

import streamlit as st
from rag_pipeline import load_documents, create_vectorstore, load_vectorstore, ask_question

st.title("📚 Multi‑Document RAG Assistant")

uploaded_files = st.file_uploader(
    "Upload documents",
    type=["pdf", "txt"],
    accept_multiple_files=True
)

if uploaded_files:

    paths = []

    for file in uploaded_files:
        path = f"documents/{file.name}"
        with open(path, "wb") as f:
            f.write(file.getbuffer())
        paths.append(path)

    docs = load_documents(paths)

    vectorstore = create_vectorstore(docs)

    st.success("Documents indexed successfully!")

query = st.text_input("Ask a question about your documents")

if query:

    vectorstore = load_vectorstore()

    answer, sources = ask_question(vectorstore, query)

    st.subheader("Answer")
    st.write(answer)

    st.subheader("Sources")

    for doc in sources:
        st.write(doc.metadata)
# rag_pipeline.py

import os
from typing import List

import streamlit as st
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_ollama import Ollama
from langchain.schema import Document


@st.cache_resource
def get_embeddings():
    return HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )


@st.cache_resource
def get_llm():
    return Ollama(model="llama3")


def load_documents(files: List[str]) -> List[Document]:
    docs = []

    for path in files:
        if path.endswith(".pdf"):
            loader = PyPDFLoader(path)
        else:
            loader = TextLoader(path)

        docs.extend(loader.load())

    return docs


def create_vectorstore(docs: List[Document], persist_dir="vectorstore"):

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=150
    )

    chunks = splitter.split_documents(docs)

    embeddings = get_embeddings()

    new_store = FAISS.from_documents(chunks, embeddings)

    # Merge into existing index if one exists, otherwise save fresh
    if os.path.exists(os.path.join(persist_dir, "index.faiss")):
        existing = FAISS.load_local(
            persist_dir, embeddings, allow_dangerous_deserialization=True
        )
        existing.merge_from(new_store)
        existing.save_local(persist_dir)
        return existing

    new_store.save_local(persist_dir)
    return new_store


def load_vectorstore(persist_dir="vectorstore"):

    embeddings = get_embeddings()

    return FAISS.load_local(
        persist_dir, embeddings, allow_dangerous_deserialization=True
    )


def ask_question(vectorstore, query, k=4):

    retriever = vectorstore.as_retriever(
        search_kwargs={"k": k}
    )

    docs = retriever.invoke(query)

    context = "\n\n".join(d.page_content for d in docs)

    prompt = f"""
Answer ONLY using the context below.
If the answer is not present, reply: "Not found".

Context:
{context}

Question:
{query}
"""

    llm = get_llm()

    response = llm.invoke(prompt)

    return response, docs
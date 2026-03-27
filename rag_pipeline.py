# rag_pipeline.py

import os
from typing import List
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.document_loaders import PyPDFLoader, TextLoader
from langchain_huggingface import HuggingFaceEmbeddings
from langchain.vectorstores import FAISS
from langchain_ollama import Ollama
from langchain.schema import Document


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

    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )

    vectorstore = FAISS.from_documents(chunks, embeddings)

    vectorstore.save_local(persist_dir)

    return vectorstore


def load_vectorstore(persist_dir="vectorstore"):

    embeddings = HuggingFaceEmbeddings(
        model_name="sentence-transformers/all-MiniLM-L6-v2"
    )

    return FAISS.load_local(persist_dir, embeddings)


def ask_question(vectorstore, query, k=4):

    retriever = vectorstore.as_retriever(
        search_kwargs={"k": k}
    )

    docs = retriever.get_relevant_documents(query)

    context = "\n\n".join(d.page_content for d in docs)

    prompt = f"""
Answer ONLY using the context below.
If the answer is not present, reply: "Not found".

Context:
{context}

Question:
{query}
"""

    llm = Ollama(model="llama3")

    response = llm.invoke(prompt)

    return response, docs
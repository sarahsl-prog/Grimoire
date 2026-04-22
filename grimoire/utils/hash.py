"""
Hash utility functions for Grimoire.

This module provides utility functions for generating and working with hashes
used throughout the Grimoire knowledge management system.
"""

import hashlib
from typing import Union
from pathlib import Path


def compute_file_hash(file_path: Union[str, Path]) -> str:
    """
    Compute SHA-256 hash of a file.

    Args:
        file_path: Path to the file to hash

    Returns:
        SHA-256 hash as hexadecimal string

    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    sha256_hash = hashlib.sha256()

    try:
        with open(file_path, "rb") as f:
            # Read file in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
    except IOError as e:
        raise IOError(f"Error reading file {file_path}: {e}")


def compute_string_hash(content: str) -> str:
    """
    Compute SHA-256 hash of a string.

    Args:
        content: String to hash

    Returns:
        SHA-256 hash as hexadecimal string
    """
    if not isinstance(content, str):
        raise TypeError("Content must be a string")

    sha256_hash = hashlib.sha256()
    sha256_hash.update(content.encode("utf-8"))
    return sha256_hash.hexdigest()


def compute_query_hash(query: str, filters: dict = None, top_k: int = None) -> str:
    """
    Compute hash for a query with filters and top_k parameters.

    This is used for caching query results.

    Args:
        query: Query string
        filters: Optional dictionary of filters
        top_k: Optional top_k parameter

    Returns:
        SHA-256 hash as hexadecimal string
    """
    # Create a consistent string representation of the query parameters
    filter_str = str(sorted(filters.items())) if filters else ""
    params = f"{query}|{filter_str}|{top_k}"
    return compute_string_hash(params)


def verify_file_hash(file_path: Union[str, Path], expected_hash: str) -> bool:
    """
    Verify that a file's hash matches an expected hash.

    Args:
        file_path: Path to the file to verify
        expected_hash: Expected SHA-256 hash

    Returns:
        True if hashes match, False otherwise
    """
    try:
        actual_hash = compute_file_hash(file_path)
        return actual_hash == expected_hash
    except (FileNotFoundError, IOError):
        return False

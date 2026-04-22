"""
Path utility functions for Grimoire.

This module provides utility functions for working with file paths
used throughout the Grimoire knowledge management system.
"""

import os
from pathlib import Path
from typing import Union, List, Optional
from urllib.parse import urlparse


def normalize_path(path: Union[str, Path]) -> Path:
    """
    Normalize a file path to a Path object.

    Args:
        path: Path string or Path object to normalize

    Returns:
        Normalized Path object

    Raises:
        ValueError: If path is invalid
    """
    if not path:
        raise ValueError("Path cannot be empty")

    return Path(path).resolve()


def is_uri(path: str) -> bool:
    """
    Check if a path is a URI (e.g., gdrive://, onedrive://).

    Args:
        path: Path string to check

    Returns:
        True if path is a URI, False otherwise
    """
    if not isinstance(path, str):
        return False

    try:
        result = urlparse(path)
        return bool(result.scheme and result.netloc)
    except Exception:
        return False


def get_uri_scheme(path: str) -> Optional[str]:
    """
    Extract the scheme from a URI path.

    Args:
        path: URI path string

    Returns:
        Scheme portion of URI or None if not a URI
    """
    if not is_uri(path):
        return None

    try:
        return urlparse(path).scheme.lower()
    except Exception:
        return None


def join_paths(base_path: Union[str, Path], *paths: Union[str, Path]) -> Path:
    """
    Join paths safely, handling both local and URI paths.

    Args:
        base_path: Base path
        *paths: Additional path components

    Returns:
        Joined Path object
    """
    base = normalize_path(base_path)

    # For URI paths, we need special handling
    if isinstance(base_path, str) and is_uri(base_path):
        # For URI paths, join as strings
        result = base_path
        for p in paths:
            # Simple string joining for URIs
            if not result.endswith("/"):
                result += "/"
            result += str(p).lstrip("/")
        return Path(result)
    else:
        # For local paths, use Path.joinpath
        return base.joinpath(*[str(p) for p in paths])


def expand_user_path(path: Union[str, Path]) -> Path:
    """
    Expand user home directory references in a path.

    Args:
        path: Path that may contain ~ references

    Returns:
        Path with ~ expanded to user home directory
    """
    path_obj = Path(path)
    return path_obj.expanduser().resolve()


def is_subpath(child: Union[str, Path], parent: Union[str, Path]) -> bool:
    """
    Check if child path is a subpath of parent path.

    Args:
        child: Potential child path
        parent: Potential parent path

    Returns:
        True if child is a subpath of parent, False otherwise
    """
    child_path = normalize_path(child)
    parent_path = normalize_path(parent)

    try:
        # Check if child path is relative to parent path
        child_path.relative_to(parent_path)
        return True
    except ValueError:
        return False


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing or replacing invalid characters.

    Args:
        filename: Filename to sanitize

    Returns:
        Sanitized filename
    """
    if not isinstance(filename, str):
        raise TypeError("Filename must be a string")

    # Remove invalid characters for most filesystems
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, "_")

    # Remove control characters
    filename = "".join(char for char in filename if ord(char) >= 32)

    # Strip leading/trailing whitespace and dots
    filename = filename.strip(". ")

    # Ensure filename isn't empty
    if not filename:
        filename = "unnamed"

    return filename


def get_file_extension(file_path: Union[str, Path]) -> str:
    """
    Get the file extension from a path.

    Args:
        file_path: Path to extract extension from

    Returns:
        File extension including the dot (e.g., '.txt')
    """
    path_obj = Path(file_path)
    return path_obj.suffix.lower()


def get_storage_backend_from_path(path: Union[str, Path]) -> str:
    """
    Determine storage backend from path.

    Args:
        path: Path to analyze

    Returns:
        Storage backend identifier ('local', 'gdrive', 'onedrive', etc.)
    """
    path_str = str(path)

    if is_uri(path_str):
        scheme = get_uri_scheme(path_str)
        if scheme:
            return scheme
        else:
            return "local"  # Fallback for malformed URIs
    else:
        return "local"

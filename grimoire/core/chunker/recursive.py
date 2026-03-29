"""Recursive character-based text splitting with configurable separators.

This module implements recursive text splitting that tries multiple separators
in sequence, starting from larger structural units and falling back to
smaller ones. This produces high-quality chunks while respecting natural
text boundaries.
"""

from typing import List, Optional

from pydantic import Field

from grimoire.core.chunker.base import Chunk, ChunkConfig, Chunker, ChunkingStrategy


class RecursiveChunkConfig(ChunkConfig):
    """Configuration for recursive character chunking.

    Extends base ChunkConfig with recursive-specific parameters.

    Attributes:
        separators: List of separator strings to try, in order of preference.
            Higher-level separators (paragraphs) are tried first,
            falling back to lower-level ones (sentences, words).
        keep_separator: Whether to keep the separator at chunk boundaries.
        is_separator_regex: Whether separators should be treated as regex.

    Example:
        ```python
        config = RecursiveChunkConfig(
            separators=["\n\n", "\n", ". ", " ", ""],
            chunk_size=1000,
            chunk_overlap=200,
        )
        ```
    """

    strategy: ChunkingStrategy = ChunkingStrategy.RECURSIVE

    separators: List[str] = Field(
        default_factory=lambda: [
            "\n\n",  # Paragraphs
            "\n",     # Lines
            ". ",      # Sentences
            " ",       # Words
            "",        # Characters (last resort)
        ],
        description="Separators to try, in order from largest to smallest",
    )
    keep_separator: bool = Field(
        default=True, description="Whether to keep separator at boundary"
    )
    is_separator_regex: bool = Field(
        default=False, description="Treat separators as regex patterns"
    )

    @classmethod
    def for_code(cls, language: str) -> "RecursiveChunkConfig":
        """Create config optimized for code (Python, JS, etc.).

        Args:
            language: Programming language name.

        Returns:
            Config with language-appropriate separators.
        """
        language_separators = {
            "python": ["\nclass ", "\ndef ", "\n\n", "\n", " ", ""],
            "javascript": ["\nfunction ", "\nconst ", "\n\n", "\n", " ", ""],
            "typescript": ["\nclass ", "\nfunction ", "\n\n", "\n", " ", ""],
            "java": ["\nclass ", "\nvoid ", "\n\n", "\n", " ", ""],
            "rust": ["\nfn ", "\nstruct ", "\n\n", "\n", " ", ""],
        }

        separators = language_separators.get(language.lower(), ["\n\n", "\n", " ", ""])
        return cls(
            separators=separators,
            chunk_size=1000,
            chunk_overlap=200,
        )


class RecursiveCharacterTextSplitter(Chunker):
    """Recursively split text using hierarchical separators.

    This chunker tries multiple separators in order of preference,
    starting with paragraph boundaries and falling back to sentences,
    words, and finally individual characters. This creates natural
    chunk boundaries while respecting the target size.

    The overlap mechanism ensures context continuity between chunks.

    Example:
        ```python
        config = RecursiveChunkConfig(
            separators=["\n\n", "\n", ". ", " ", ""],
        )
        chunker = RecursiveCharacterTextSplitter(config)
        chunks = await chunker.chunk(document_text, doc_id="doc-123")
        ```
    """

    def __init__(self, config: Optional[RecursiveChunkConfig] = None) -> None:
        """Initialize recursive text splitter.

        Args:
            config: Recursive chunking configuration. Uses defaults if not provided.
        """
        super().__init__(config or RecursiveChunkConfig())
        self.config: RecursiveChunkConfig  # Type hint for IDE

    def _split_text_with_separator(
        self, text: str, separator: str, keep_separator: bool = True
    ) -> List[str]:
        """Split text using a separator, optionally keeping it.

        Args:
            text: Text to split.
            separator: Separator string (or "" for characters).
            keep_separator: Whether to keep separator with preceding chunk.

        Returns:
            List of split parts.
        """
        import re

        if separator == "":
            # Character-level splitting
            return list(text)

        if self.config.is_separator_regex:
            parts = re.split(separator, text)
        else:
            parts = text.split(separator)

        if not keep_separator:
            return [p for p in parts if p]

        # Re-attach separator to preceding chunk
        result: List[str] = []
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                if self.config.is_separator_regex:
                    # For regex, we need to find the actual separator
                    match = re.search(separator, text)
                    actual_sep = match.group(0) if match else separator
                else:
                    actual_sep = separator
                result.append(part + actual_sep)
            else:
                result.append(part)

        return [r for r in result if r]

    def _merge_splits_with_overlap(
        self, splits: List[str], separator: str = ""
    ) -> List[str]:
        """Merge splits into chunks with target size and overlap.

        Args:
            splits: List of text splits.
            separator: Separator to use when joining splits.

        Returns:
            List of merged chunks.
        """
        # Estimate characters per token (roughly 4 for English)
        chars_per_token = 4
        target_chars = self.config.chunk_size * chars_per_token
        overlap_chars = self.config.chunk_overlap * chars_per_token

        if not splits:
            return []

        chunks: List[str] = []
        current_chunk: List[str] = []
        current_chars = 0

        for split in splits:
            split_len = len(split)

            # If adding this split would exceed target and we have content
            if current_chars + split_len > target_chars and current_chunk:
                # Save current chunk
                chunks.append(separator.join(current_chunk))

                # Calculate overlap: keep last overlap_chars worth of content
                overlap_splits: List[str] = []
                overlap_len = 0
                for prev_split in reversed(current_chunk):
                    if overlap_len + len(prev_split) <= overlap_chars:
                        overlap_splits.insert(0, prev_split)
                        overlap_len += len(prev_split)
                    else:
                        break

                current_chunk = overlap_splits + [split]
                current_chars = overlap_len + split_len
            else:
                current_chunk.append(split)
                current_chars += split_len

        # Don't forget the last chunk
        if current_chunk:
            chunks.append(separator.join(current_chunk))

        return [c.strip() for c in chunks if c.strip()]

    def _recursive_split(
        self, text: str, separators: List[str]
    ) -> List[str]:
        """Recursively split text using hierarchical separators.

        Args:
            text: Text to split.
            separators: List of separators to try.

        Returns:
            List of text chunks.
        """
        if not text.strip():
            return []

        if not separators:
            # No separators left - return text as-is
            return [text]

        separator = separators[0]
        remaining_separators = separators[1:]

        # Split using current separator
        splits = self._split_text_with_separator(
            text, separator, self.config.keep_separator
        )

        # Estimate chars per token
        chars_per_token = 4
        target_chars = self.config.chunk_size * chars_per_token

        # Check if splits are small enough
        good_splits = [s for s in splits if len(s) <= target_chars]
        large_splits = [s for s in splits if len(s) > target_chars]

        # Merge good splits into chunks
        if good_splits:
            merged = self._merge_splits_with_overlap(good_splits, separator="")
        else:
            merged = []

        # Recursively split large splits
        for large in large_splits:
            if remaining_separators:
                sub_splits = self._recursive_split(large, remaining_separators)
                merged.extend(
                    self._merge_splits_with_overlap(sub_splits, separator="")
                )
            else:
                # No more separators - just truncate
                merged.extend(
                    self._merge_splits_with_overlap([large], separator="")
                )

        return merged

    async def chunk(self, text: str, doc_id: Optional[str] = None) -> List[Chunk]:
        """Split text recursively using hierarchical separators.

        Args:
            text: The text content to chunk.
            doc_id: Optional document ID for chunk metadata.

        Returns:
            List of Chunk objects with continuity links.

        Raises:
            ValueError: If text is invalid.
        """
        if not text or not text.strip():
            return []

        # Perform recursive splitting
        chunk_texts = self._recursive_split(text, self.config.separators)

        # Create Chunk objects
        chunks: List[Chunk] = []
        for i, content in enumerate(chunk_texts):
            if not content.strip():
                continue

            token_count = self._count_tokens(content)

            chunk = Chunk(
                content=content,
                token_count=token_count,
                index=i,
                metadata={
                    "strategy": "recursive",
                    "separator_count": len(self.config.separators),
                },
            )
            chunks.append(chunk)

        # Set continuity links
        if chunks:
            self._set_continuity_links(chunks, doc_id or "doc")

        return chunks
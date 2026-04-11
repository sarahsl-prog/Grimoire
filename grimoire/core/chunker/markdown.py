"""Markdown-aware chunking using header hierarchy.

This module implements markdown-specific chunking that respects header
hierarchy (# ## ###), producing chunks organized by document structure.
This preserves the semantic organization of markdown documents.
"""

import re
from typing import List, Optional, Pattern, Tuple

from pydantic import Field

from grimoire.core.chunker.base import Chunk, ChunkConfig, Chunker, ChunkingStrategy


class MarkdownChunkConfig(ChunkConfig):
    """Configuration for markdown header-based chunking.

    Extends base ChunkConfig with markdown-specific parameters.

    Attributes:
        headers_to_split_on: List of header markers to split on (e.g., ["#", "##"]).
        keep_headers: Whether to prepend headers to chunk content.

    Example:
        ```python
        config = MarkdownChunkConfig(
            headers_to_split_on=["#", "##", "###"],
            keep_headers=True,
        )
        ```
    """

    strategy: ChunkingStrategy = ChunkingStrategy.MARKDOWN

    headers_to_split_on: List[str] = Field(
        default_factory=lambda: ["#", "##", "###"],
        description="Header levels to split on, e.g., ['#', '##', '###']",
    )
    keep_headers: bool = Field(
        default=True, description="Whether to prepend header text to chunk content"
    )


class MarkdownHeaderTextSplitter(Chunker):
    """Split markdown documents respecting header hierarchy.

    This chunker respects document structure by splitting on headers
    (# ## ###). Each chunk includes its header context, preserving
    the semantic organization of the document.

    The splitter maintains header hierarchy, so a chunk under "##"
    knows it's part of the "#" section above it.

    Example:
        ```python
        markdown_text = '''
        # Main Title
        Some intro text.

        ## Section 1
        Content here.

        ### Subsection 1.1
        More content.
        '''

        config = MarkdownChunkConfig()
        chunker = MarkdownHeaderTextSplitter(config)
        chunks = await chunker.chunk(markdown_text, doc_id="doc-123")

        # Results in chunks:
        # - "Main Title" (intro text)
        # - "Section 1" (with header)
        # - "Subsection 1.1" (with full header path)
        ```
    """

    def __init__(self, config: Optional[MarkdownChunkConfig] = None) -> None:
        """Initialize markdown header splitter.

        Args:
            config: Markdown chunking configuration. Uses defaults if not provided.
        """
        super().__init__(config or MarkdownChunkConfig())
        self.config: MarkdownChunkConfig  # Type hint for IDE
        self._header_pattern = self._build_header_pattern()

    def _build_header_pattern(self) -> Pattern[str]:
        """Build regex pattern for matching headers.

        Returns:
            Compiled regex pattern for headers.
        """
        # Build pattern from headers_to_split_on
        escaped = [
            re.escape(h)
            for h in sorted(self.config.headers_to_split_on, key=len, reverse=True)
        ]
        pattern = r"^(" + "|".join(escaped) + r")\s+(.+)$"
        return re.compile(pattern, re.MULTILINE)

    def _extract_header_level(self, header_marker: str) -> int:
        """Get header level from marker (e.g., "##" -> 2).

        Args:
            header_marker: The header marker string.

        Returns:
            Header level (1 for #, 2 for ##, etc.).
        """
        return len(header_marker)

    def _build_header_context(self, header_stack: List[Tuple[int, str]]) -> str:
        """Build full header path from header stack.

        Args:
            header_stack: Stack of (level, title) tuples.

        Returns:
            Full header path string.
        """
        if not header_stack:
            return ""
        return " > ".join(title for _, title in header_stack)

    def _split_text_by_headers(self, text: str) -> List[Tuple[Optional[str], str]]:
        """Split text into (header, content) sections.

        Args:
            text: Markdown text to split.

        Returns:
            List of (header_line, content) tuples.
        """
        if not text.strip():
            return []

        lines = text.split("\n")
        sections: List[Tuple[Optional[str], str]] = []
        current_content: List[str] = []
        current_header: Optional[str] = None

        for line in lines:
            stripped = line.strip()
            if not stripped:
                current_content.append(line)
                continue

            # Check if line is a header
            is_header = False
            for header_marker in sorted(self.config.headers_to_split_on, key=len, reverse=True):
                if stripped.startswith(header_marker + " "):
                    # Save previous section
                    if current_content:
                        content_str = "\n".join(current_content).strip()
                        if content_str:
                            sections.append((current_header, content_str))
                        current_content = []
                    current_header = line.strip()
                    is_header = True
                    break

            if not is_header:
                current_content.append(line)

        # Don't forget the last section
        if current_content:
            content_str = "\n".join(current_content).strip()
            if content_str:
                sections.append((current_header, content_str))

        # If no headers found, treat entire text as one section
        if not sections:
            stripped_text = text.strip()
            if stripped_text:
                sections.append((None, stripped_text))

        return sections

    def _organize_sections(
        self, sections: List[Tuple[Optional[str], str]]
    ) -> List[Tuple[str, str, str, int]]:
        """Organize sections with their header context.

        Args:
            sections: List of (header_line, content) tuples.

        Returns:
            List of (content, header_title, full_context, level) tuples.
        """
        organized: List[Tuple[str, str, str, int]] = []
        header_stack: List[Tuple[int, str]] = []

        header_pattern = self._header_pattern

        for header_line, content in sections:
            if header_line is None:
                # No header - this is content before first header
                organized.append((content, "", "", 0))
                continue

            # Parse header
            match = header_pattern.match(header_line)
            if not match:
                # Not a valid header pattern
                organized.append((content, header_line, "", 0))
                continue

            header_marker = match.group(1)
            header_title = match.group(2).strip()
            level = self._extract_header_level(header_marker)

            # Pop headers of equal or higher level
            while header_stack and header_stack[-1][0] >= level:
                header_stack.pop()

            # Push current header
            header_stack.append((level, header_title))

            # Build full context
            full_context = self._build_header_context(header_stack)

            organized.append((content, header_title, full_context, level))

        return organized

    async def chunk(self, text: str, doc_id: Optional[str] = None) -> List[Chunk]:
        """Split markdown text respecting header hierarchy.

        Args:
            text: The markdown text to chunk.
            doc_id: Optional document ID for chunk metadata.

        Returns:
            List of Chunk objects with header context in metadata.

        Raises:
            ValueError: If text is invalid.
        """
        if not text or not text.strip():
            return []

        # Split by headers
        sections = self._split_text_by_headers(text)

        # Organize with header context
        organized = self._organize_sections(sections)

        chunks: List[Chunk] = []

        for content, header_title, full_context, level in organized:
            if not content.strip():
                continue

            # Build chunk content with header if requested
            if self.config.keep_headers and header_title:
                display_context = f"# {full_context}\n\n" if full_context else ""
                chunk_content = f"{display_context}{content}"
            else:
                chunk_content = content

            token_count = self._count_tokens(chunk_content)

            chunk = Chunk(
                content=chunk_content,
                token_count=token_count,
                index=len(chunks),
                metadata={
                    "header": header_title,
                    "header_level": level,
                    "header_context": full_context,
                    "strategy": "markdown",
                },
            )
            chunks.append(chunk)

        # If chunks are too large, split them recursively
        final_chunks: List[Chunk] = []
        for chunk in chunks:
            chunk_tokens = chunk.token_count
            if chunk_tokens > self.config.chunk_size:
                # Split large chunks while preserving header context
                sub_chunks = self._split_large_chunk(chunk, len(final_chunks))
                final_chunks.extend(sub_chunks)
            else:
                chunk.index = len(final_chunks)
                final_chunks.append(chunk)

        # Reindex to ensure sequential indices after splits
        for idx, chunk in enumerate(final_chunks):
            chunk.index = idx

        # Set continuity links
        if final_chunks:
            self._set_continuity_links(final_chunks, doc_id or "doc")

        return final_chunks

    def _split_large_chunk(self, chunk: Chunk, current_index: int) -> List[Chunk]:
        """Split a chunk that's too large while preserving header context.

        Args:
            chunk: The chunk to split.
            current_index: Current index in the chunks list.

        Returns:
            List of sub-chunks.
        """
        content = chunk.content
        header_prefix = ""

        # Extract header prefix if it exists
        if chunk.content.startswith("#"):
            lines = content.split("\n", 1)
            if len(lines) > 1 and lines[0].startswith("#"):
                header_prefix = lines[0] + "\n\n"
                content = lines[1]

        # Estimate characters per token (roughly 4 for English)
        chars_per_token = 4
        target_chars = self.config.chunk_size * chars_per_token

        # Split by paragraphs first
        paragraphs = content.split("\n\n")

        sub_chunks: List[Chunk] = []
        current_content: List[str] = []
        current_chars = 0
        chunk_index = current_index

        for paragraph in paragraphs:
            para_len = len(paragraph)

            if current_chars + para_len > target_chars and current_content:
                # Create a new chunk
                chunk_text = header_prefix + "\n\n".join(current_content)
                sub_chunk = Chunk(
                    content=chunk_text,
                    token_count=self._count_tokens(chunk_text),
                    index=chunk_index,
                    metadata={**chunk.metadata, "is_split": True},
                )
                sub_chunks.append(sub_chunk)
                chunk_index += 1

                # Keep overlap for continuity
                overlap_text = (
                    "\n\n".join(current_content[-2:])
                    if len(current_content) >= 2
                    else current_content[-1]
                    if current_content
                    else ""
                )
                current_content = (
                    [overlap_text, paragraph] if overlap_text else [paragraph]
                )
                current_chars = len(overlap_text) + para_len
            else:
                current_content.append(paragraph)
                current_chars += para_len + 2  # +2 for newlines

        # Add remaining content
        if current_content:
            chunk_text = header_prefix + "\n\n".join(current_content)
            sub_chunk = Chunk(
                content=chunk_text,
                token_count=self._count_tokens(chunk_text),
                index=chunk_index,
                metadata={**chunk.metadata, "is_split": True},
            )
            sub_chunks.append(sub_chunk)

        return sub_chunks

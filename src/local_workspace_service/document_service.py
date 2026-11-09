"""Secure, policy-aware reading and literal search for workspace documents."""

from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import time
import zipfile
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree

from ._filesystem import FileSnapshot, ReparsePointError, UnsafeFileError, open_root
from .errors import RootChangedError
from .models import DEFAULT_IGNORED_FILES, ExclusionReason, IncludedFile, Workspace
from .repository import WorkspaceRepository
from .scanner import scan_workspace


MAX_DOCX_ENTRIES = 1000
MAX_DOCX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
MAX_DOCX_XML_ELEMENTS = 200_000
MAX_DOCX_XML_DEPTH = 128
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_SEGMENTS = 200_000
DEFAULT_READ_CHARS = 12_000
MAX_READ_CHARS = 30_000
MAX_SEARCH_FILES = 200
MAX_SEARCH_BYTES = 50 * 1024 * 1024
MAX_SEARCH_TEXT_CHARS = 2_000_000
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_RETURN_CHARS = 30_000
MAX_SEARCH_SECONDS = 10.0
MAX_LIST_RESULTS = 100


class DocumentError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TextSegment:
    location: str
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    relative_path: str
    version: str
    size_bytes: int
    segments: list[TextSegment]
    notes: list[str]


@dataclass(frozen=True)
class ReadResult:
    relative_path: str
    version: str
    size_bytes: int
    text: str
    locations: list[str]
    truncated: bool
    next_offset: int | None
    next_char_offset: int | None
    notes: list[str]


@dataclass(frozen=True)
class SearchMatch:
    relative_path: str
    version: str
    location: str
    text: str


@dataclass(frozen=True)
class SearchResult:
    matches: list[SearchMatch]
    searched_files: int
    searched_bytes: int
    skipped: list[dict[str, str]]
    truncated: bool
    coverage_note: str


class DocumentService:
    def __init__(self, repository: WorkspaceRepository) -> None:
        self.repository = repository

    def list_files(self, workspace_id: str, *, offset: int = 0, max_results: int = MAX_LIST_RESULTS) -> tuple[list[IncludedFile], int | None, bool]:
        if not 0 <= offset <= 100_000 or not 1 <= max_results <= MAX_LIST_RESULTS:
            raise DocumentError("invalid_limit", "文件列表位置或返回数量超出允许范围")
        workspace = self.repository.get(workspace_id)
        scan = scan_workspace(workspace, deadline=time.monotonic() + MAX_SEARCH_SECONDS)
        end = offset + max_results
        next_offset = end if end < len(scan.included) else None
        incomplete = scan.summary.limit_reached or scan.summary.traversal_limit_reached or bool(scan.errors)
        return scan.included[offset:end], next_offset, incomplete

    def metadata(self, workspace_id: str, relative_path: str) -> IncludedFile:
        normalized = self._normalize_path(relative_path)
        workspace = self.repository.get(workspace_id)
        self._validate_policy(workspace, relative_path)
        for item in scan_workspace(workspace, deadline=time.monotonic() + MAX_SEARCH_SECONDS).included:
            if item.relative_path == normalized:
                return item
        raise DocumentError("not_allowed", "文件不存在或不符合当前扫描规则")

    def read(
        self,
        workspace_id: str,
        relative_path: str,
        *,
        offset: int = 0,
        char_offset: int = 0,
        max_chars: int = DEFAULT_READ_CHARS,
    ) -> ReadResult:
        if offset < 0 or offset > 1_000_000:
            raise DocumentError("invalid_offset", "读取位置超出允许范围")
        if not 0 <= char_offset <= MAX_DOCUMENT_BYTES:
            raise DocumentError("invalid_offset", "片段内读取位置超出允许范围")
        if max_chars < 1 or max_chars > MAX_READ_CHARS:
            raise DocumentError("invalid_limit", "返回字符数超出允许范围")
        workspace = self.repository.get(workspace_id)
        path = self._validate_policy(workspace, relative_path)
        parsed = self._parse(workspace, path)
        if offset > len(parsed.segments) or (
            char_offset and (offset == len(parsed.segments) or char_offset >= len(parsed.segments[offset].text))
        ):
            raise DocumentError("invalid_offset", "读取位置超过文档末尾")
        selected: list[TextSegment] = []
        used = 0
        index = offset
        cursor = char_offset
        while index < len(parsed.segments):
            segment = parsed.segments[index]
            remaining = max_chars - used - bool(selected)
            if remaining <= 0:
                break
            text = segment.text[cursor : cursor + remaining]
            selected.append(TextSegment(segment.location, text))
            used += len(text) + (len(selected) > 1)
            cursor += len(text)
            if cursor < len(segment.text):
                break
            index += 1
            cursor = 0
        truncated = index < len(parsed.segments)
        return ReadResult(
            relative_path=parsed.relative_path,
            version=parsed.version,
            size_bytes=parsed.size_bytes,
            text="\n".join(item.text for item in selected),
            locations=[item.location for item in selected],
            truncated=truncated,
            next_offset=index if truncated else None,
            next_char_offset=cursor if truncated else None,
            notes=parsed.notes,
        )

    def search(
        self,
        workspace_id: str,
        keyword: str,
        *,
        path_prefix: str | None = None,
        max_results: int = 20,
        task_deadline: float | None = None,
    ) -> SearchResult:
        query = keyword.strip()
        if not query or len(query) > 200:
            raise DocumentError("invalid_keyword", "关键词不能为空且不能超过 200 个字符")
        if not 1 <= max_results <= MAX_SEARCH_RESULTS:
            raise DocumentError("invalid_limit", "搜索结果数量超出允许范围")
        prefix = None
        if path_prefix:
            prefix = self._normalize_path(path_prefix).rstrip("/")
        deadline = time.monotonic() + MAX_SEARCH_SECONDS
        if task_deadline is not None:
            deadline = min(deadline, task_deadline)
        workspace = self.repository.get(workspace_id)
        scan = scan_workspace(workspace, deadline=deadline)
        candidates = scan.included
        if prefix is not None:
            candidates = [
                item for item in candidates
                if item.relative_path == prefix or item.relative_path.startswith(prefix + "/")
            ]
        matches: list[SearchMatch] = []
        skipped = [
            {"relative_path": item.relative_path, "code": item.reason.value, "message": item.detail}
            for item in [*scan.errors, *scan.excluded]
            if item.reason in {ExclusionReason.ACCESS_ERROR, ExclusionReason.FILE_TOO_LARGE, ExclusionReason.FILE_LIMIT_EXCEEDED}
            and (prefix is None or item.relative_path == prefix or item.relative_path.startswith(prefix + "/"))
        ][:MAX_SEARCH_FILES]
        searched_files = searched_bytes = returned_chars = text_chars = attempted = 0
        truncated = scan.summary.traversal_limit_reached or scan.summary.limit_reached
        needle = query.casefold()
        for item in candidates:
            if (
                attempted >= MAX_SEARCH_FILES
                or searched_bytes >= MAX_SEARCH_BYTES
                or time.monotonic() >= deadline
            ):
                truncated = True
                break
            attempted += 1
            try:
                current = self.repository.get(workspace_id)
                path = self._validate_policy(current, item.relative_path)
                snapshot = self._secure_snapshot(current, path, byte_budget=MAX_SEARCH_BYTES - searched_bytes, attempts=1)
                searched_bytes += snapshot.size
                parsed = self._parse_snapshot(path, snapshot)
            except DocumentError as exc:
                skipped.append({"relative_path": item.relative_path, "code": exc.code, "message": str(exc)})
                continue
            searched_files += 1
            stopped = False
            for segment in parsed.segments:
                text_chars += len(segment.text)
                if time.monotonic() >= deadline or text_chars > MAX_SEARCH_TEXT_CHARS:
                    stopped = True
                    break
                if needle not in segment.text.casefold():
                    continue
                excerpt = _excerpt(segment.text, query)
                if returned_chars + len(excerpt) > MAX_SEARCH_RETURN_CHARS:
                    stopped = True
                    break
                matches.append(SearchMatch(parsed.relative_path, parsed.version, segment.location, excerpt))
                returned_chars += len(excerpt)
                if len(matches) >= max_results:
                    stopped = True
                    break
            if stopped:
                truncated = True
                break
        complete = not truncated and not skipped and searched_files == len(candidates)
        note = (
            "已搜索当前路径范围内全部符合规则的候选文件，规则排除项未读取。"
            if complete
            else "搜索范围不完整；存在跳过、解析失败或资源上限，未命中不代表整个工作区不存在。"
        )
        return SearchResult(matches, searched_files, searched_bytes, skipped, truncated, note)

    def _parse(self, workspace: Workspace, path: PurePosixPath) -> ParsedDocument:
        return self._parse_snapshot(path, self._secure_snapshot(workspace, path))

    def _parse_snapshot(self, path: PurePosixPath, snapshot: FileSnapshot) -> ParsedDocument:
        version = hashlib.sha256(snapshot.content).hexdigest()
        if path.suffix.lower() == ".md":
            try:
                text = snapshot.content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DocumentError("decode_failed", "Markdown 文件不是有效的 UTF-8 文本") from exc
            # Universal CR/LF handling while preserving other Unicode characters in a line.
            lines = io.StringIO(text, newline=None)
            segments = []
            for number, line in enumerate(lines, 1):
                if number > MAX_DOCUMENT_SEGMENTS:
                    raise DocumentError("markdown_limit", "Markdown 行数超过处理限制")
                segments.append(TextSegment(f"第 {number} 行", line.removesuffix("\n")))
            return ParsedDocument(path.as_posix(), version, snapshot.size, segments, [])
        return self._parse_docx(path.as_posix(), snapshot, version)

    def _secure_snapshot(
        self, workspace: Workspace, path: PurePosixPath, *,
        byte_budget: int = MAX_DOCUMENT_BYTES, attempts: int = 2,
    ) -> FileSnapshot:
        maximum = min(workspace.limits.max_file_size_mb * 1024 * 1024, MAX_DOCUMENT_BYTES, byte_budget)
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                with ExitStack() as stack:
                    root, resolved = stack.enter_context(open_root(workspace.root_path, verify_identity=True))
                    if os.path.normcase(str(resolved)) != os.path.normcase(workspace.root_path):
                        raise RootChangedError("the registered root now resolves elsewhere")
                    directory = root
                    parents = []
                    for part in path.parts[:-1]:
                        child = stack.enter_context(closing(directory.open_child(part)))
                        directory.verify_child(part, child)
                        parents.append((directory, part, child))
                        directory = child
                    snapshot = directory.read_file(path.name, max_bytes=maximum)
                    for parent, name, child in reversed(parents):
                        parent.verify_child(name, child)
                    return snapshot
            except UnsafeFileError as exc:
                last_error = exc
                if "changed while" in str(exc):
                    continue
                raise DocumentError("unsafe_file", str(exc)) from exc
            except ReparsePointError as exc:
                raise DocumentError("link_not_allowed", "符号链接、junction 和重解析点不允许读取") from exc
            except RootChangedError:
                raise
            except OSError as exc:
                raise DocumentError("access_failed", "文件无法安全打开或已发生变化") from exc
        raise DocumentError("file_changed", "文件在读取期间发生变化，请稍后重试") from last_error

    @staticmethod
    def _normalize_path(value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value or "\\" in value:
            raise DocumentError("invalid_path", "必须提供工作区内的相对路径")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in value.rstrip("/").split("/")):
            raise DocumentError("invalid_path", "路径不能是绝对路径或包含路径穿越")
        return path.as_posix()

    def _validate_policy(self, workspace: Workspace, relative_path: str) -> PurePosixPath:
        normalized = self._normalize_path(relative_path)
        path = PurePosixPath(normalized)
        ignored_dirs = {item.casefold() for item in workspace.ignored_directories}
        if any(part.casefold() in ignored_dirs for part in path.parts[:-1]):
            raise DocumentError("ignored_directory", "路径位于当前规则忽略的目录中")
        patterns = [*DEFAULT_IGNORED_FILES, *workspace.ignored_files]
        if any(fnmatch.fnmatchcase(path.name.casefold(), pattern.casefold()) for pattern in patterns):
            raise DocumentError("ignored_file", "文件名符合敏感或忽略规则")
        if path.suffix.lower() not in workspace.allowed_extensions:
            raise DocumentError("extension_not_allowed", "文件类型不在当前白名单中")
        if not workspace.permissions.read:
            raise DocumentError("read_not_allowed", "当前工作区未授权读取")
        return path

    @staticmethod
    def _parse_docx(relative_path: str, snapshot: FileSnapshot, version: str) -> ParsedDocument:
        try:
            with zipfile.ZipFile(io.BytesIO(snapshot.content)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_DOCX_ENTRIES:
                    raise DocumentError("docx_limit", "Word 文档 ZIP 条目数超过限制")
                if sum(item.file_size for item in infos) > MAX_DOCX_EXPANDED_BYTES:
                    raise DocumentError("docx_limit", "Word 文档解压后大小超过限制")
                if any(item.flag_bits & 1 for item in infos):
                    raise DocumentError("docx_unsupported", "不支持加密的 Word 文档")
                documents = [item for item in infos if item.filename == "word/document.xml"]
                if len(documents) != 1:
                    raise DocumentError("docx_invalid", "Word 文档缺少唯一的正文结构")
                info = documents[0]
                if info.file_size > MAX_DOCX_XML_BYTES:
                    raise DocumentError("docx_limit", "Word 正文 XML 超过限制")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise DocumentError("docx_unsupported", "Word 正文使用了不支持的压缩方式")
                xml = archive.read(info)
        except DocumentError:
            raise
        except (zipfile.BadZipFile, KeyError, RuntimeError, OSError) as exc:
            raise DocumentError("docx_invalid", "Word 文档已损坏或格式不受支持") from exc
        try:
            root = ElementTree.fromstring(xml, parser=ElementTree.XMLParser(target=_WordTreeBuilder()))
        except ElementTree.ParseError as exc:
            raise DocumentError("docx_invalid", "Word 正文 XML 无法解析") from exc
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        body = root.find(f"{ns}body")
        if body is None:
            raise DocumentError("docx_invalid", "Word 文档缺少正文")
        segments: list[TextSegment] = []
        paragraph_number = 0
        table_number = 0
        for child in body:
            if child.tag == f"{ns}p":
                paragraph_number += 1
                text = _word_paragraph(child, ns)
                if text:
                    segments.append(TextSegment(f"段落 {paragraph_number}", text))
            elif child.tag == f"{ns}tbl":
                table_number += 1
                for row_number, row in enumerate(child.findall(f"{ns}tr"), 1):
                    for column_number, cell in enumerate(row.findall(f"{ns}tc"), 1):
                        text = "\n".join(
                            _word_paragraph(paragraph, ns) for paragraph in cell.findall(f"{ns}p")
                        )
                        if text:
                            segments.append(TextSegment(f"表格 {table_number} / 行 {row_number} / 单元格 {column_number}", text))
        notes = ["Word 定位为段落或表格单元格，不提供页码。", "图片、批注、文本框、嵌套表格及页眉页脚等内容未读取。"]
        return ParsedDocument(relative_path, version, snapshot.size, segments, notes)


class _WordTreeBuilder(ElementTree.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = self.count = 0

    def doctype(self, name, pubid, system) -> None:
        raise DocumentError("docx_unsupported", "Word 正文 XML 不允许 DTD 或实体声明")

    def start(self, tag, attrs):
        self.depth += 1
        self.count += 1
        if self.depth > MAX_DOCX_XML_DEPTH or self.count > MAX_DOCX_XML_ELEMENTS:
            raise DocumentError("docx_limit", "Word 正文 XML 结构超过处理限制")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def _word_paragraph(node: ElementTree.Element, ns: str) -> str:
    if node.tag == f"{ns}t":
        return node.text or ""
    if node.tag in {f"{ns}br", f"{ns}cr"}:
        return "\n"
    if node.tag == f"{ns}tab":
        return "\t"
    if node.tag in {f"{ns}p", f"{ns}r", f"{ns}hyperlink", f"{ns}ins"}:
        return "".join(_word_paragraph(child, ns) for child in node)
    return ""


def _excerpt(text: str, keyword: str, radius: int = 180) -> str:
    folded = text.casefold()
    needle = keyword.casefold()
    index = folded.index(needle)
    end = index + len(needle)
    if len(folded) != len(text):
        position = 0
        original_start = 0
        for original_index, char in enumerate(text):
            position += len(char.casefold())
            if position <= index:
                original_start = original_index + 1
            if position >= end:
                index, end = original_start, original_index + 1
                break
    return text[max(0, index - radius) : end + radius]

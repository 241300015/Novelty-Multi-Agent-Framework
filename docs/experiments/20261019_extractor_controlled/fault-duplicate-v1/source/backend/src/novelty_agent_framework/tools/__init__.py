"""Project-level tool API.

Database provider and structured-retrieval implementation details deliberately
live under :mod:`novelty_agent_framework.tools.database_search`.
"""

from .browser import BrowserTool
from .browser_backend import BrowserBackend, BrowserFetchResult, PlaywrightBrowserBackend
from .browser_runtime import (
    BrowserDependencyError,
    BrowserNetworkSettings,
    ChromiumRuntimeSettings,
    resolve_browser_network,
    resolve_chromium_runtime,
)
from .database_search import DatabaseSearchTool
from .evidence_card_builder import EvidenceCardBuilder
from .reader import ReaderTool, ReferenceReaderResearcherTool, ReviewerReaderTool
from .reference_reader import ReferenceArtifactReaderTool
from .reference_search import ReferenceSearchTool
from .renderer import (
    MarkdownRenderer,
    RendererFactory,
    ReportRenderer,
    ReportRenderError,
    render_report,
)
from .researcher_registry import ResearcherTool, ResearcherToolRegistry
from .web_search import WebSearchTool
from .web_search_backend import (
    BaiduSearchBackend,
    BaiduSearchError,
    SearchBackend,
    SearchBackendResult,
    SearchHit,
)

__all__ = [
    "BaiduSearchBackend",
    "BaiduSearchError",
    "BrowserBackend",
    "BrowserDependencyError",
    "BrowserFetchResult",
    "BrowserNetworkSettings",
    "BrowserTool",
    "ChromiumRuntimeSettings",
    "DatabaseSearchTool",
    "EvidenceCardBuilder",
    "MarkdownRenderer",
    "PlaywrightBrowserBackend",
    "ReaderTool",
    "ReviewerReaderTool",
    "ReferenceArtifactReaderTool",
    "ReferenceReaderResearcherTool",
    "ReferenceSearchTool",
    "RendererFactory",
    "ReportRenderError",
    "ReportRenderer",
    "ResearcherTool",
    "ResearcherToolRegistry",
    "SearchBackend",
    "SearchBackendResult",
    "SearchHit",
    "WebSearchTool",
    "render_report",
    "resolve_browser_network",
    "resolve_chromium_runtime",
]

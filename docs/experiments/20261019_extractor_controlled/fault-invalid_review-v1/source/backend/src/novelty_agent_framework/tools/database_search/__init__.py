"""Database-search internals and the package's public researcher tool."""

from .adapter import (
    AdapterFactory,
    CompiledQuery,
    QueryAdapter,
    QueryAdapterError,
    compile_search_plan,
)
from .factory import (
    build_database_search_tool,
    build_retrieval_source,
    build_source_registry,
    build_structured_source_retrieval_tool,
)
from .legacy_tool import StructuredRetrievalResearcherTool
from .providers.null_catalog import (
    NullQueryAdapter,
    NullSearchTool,
    build_null_catalog_source,
)
from .retrieval_sources import RetrievalSource, RetrievalSourceRegistry
from .structured_retrieval import (
    StructuredRetrievalAdapter,
    StructuredSourceRetrievalTool,
)
from .tool import DatabaseSearchTool

__all__ = [
    "AdapterFactory",
    "CompiledQuery",
    "DatabaseSearchTool",
    "NullQueryAdapter",
    "NullSearchTool",
    "QueryAdapter",
    "QueryAdapterError",
    "RetrievalSource",
    "RetrievalSourceRegistry",
    "StructuredRetrievalAdapter",
    "StructuredRetrievalResearcherTool",
    "StructuredSourceRetrievalTool",
    "build_null_catalog_source",
    "build_database_search_tool",
    "build_retrieval_source",
    "build_source_registry",
    "build_structured_source_retrieval_tool",
    "compile_search_plan",
]

# The null provider is always available; concrete network providers remain lazy.
AdapterFactory.register("null_catalog", NullQueryAdapter)

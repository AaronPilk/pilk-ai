"""Local → cloud pilkd migration.

Operator exports a bundle from their laptop via
``pilk-migrate export ...``, then uploads it to the cloud deploy at
``POST /migration/upload``. The cloud backs up its current state,
overwrites with the bundle, and returns a structured report.

Only the master-admin account (matching
``settings.supabase_master_admin_email``) is allowed to import. This
is a "once per deploy" operation by design — gate it hard.
"""

from core.migration.exporter import ExportError, build_bundle
from core.migration.importer import (
    MAX_BUNDLE_SIZE_BYTES,
    BundleImportError,
    ImportReport,
    apply_bundle,
)
from core.migration.models import (
    BUNDLE_VERSION,
    FileEntry,
    Manifest,
    TableCounts,
)

__all__ = [
    "BUNDLE_VERSION",
    "MAX_BUNDLE_SIZE_BYTES",
    "BundleImportError",
    "ExportError",
    "FileEntry",
    "ImportReport",
    "Manifest",
    "TableCounts",
    "apply_bundle",
    "build_bundle",
]

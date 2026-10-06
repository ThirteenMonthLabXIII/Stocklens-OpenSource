"""The single, code-owned identity for a Stocklens V3 release.

Customer-facing documents deliberately use only the product and support names.
Build implementation details belong in developer documentation and manifests.
"""

from __future__ import annotations

APP_VERSION = "3.0.0"
DISPLAY_VERSION = "V3"
WINDOWS_FILE_VERSION = "3.0.0.0"
PRODUCT_NAME = "Stocklens V3 / A股选股仪 V3"
PRODUCT_SHORT_NAME = "Stocklens"
PUBLISHER_NAME = "Stocklens"
EXE_NAME = "Stocklens.exe"
DIST_DIRECTORY_NAME = "Stocklens_V3"
INSTALLER_NAME = "Stocklens_V3_Setup.exe"
INSTALLER_BASE_FILENAME = "Stocklens_V3_Setup"
VERSION_NOTE_FILE = "docs/版本说明_v3.0.0.txt"
RELEASE_DIRECTORY_PREFIX = "V3_商业交付_"
NUITKA_VERSION = "4.1.3"
QT_WINDOW_TITLE = f"{PRODUCT_NAME}｜Qt 工作台"
INSTALL_DIRECTORY_NAME = "Stocklens V3"
INSTALLER_FILE_DESCRIPTION = "Stocklens V3 A股选股仪"


def as_manifest_identity() -> dict[str, str]:
    return {
        "product_version": APP_VERSION,
        "display_version": DISPLAY_VERSION,
        "windows_file_version": WINDOWS_FILE_VERSION,
        "product_name": PRODUCT_NAME,
        "exe_name": EXE_NAME,
        "dist_directory_name": DIST_DIRECTORY_NAME,
        "installer_name": INSTALLER_NAME,
        "installer_base_filename": INSTALLER_BASE_FILENAME,
        "version_note_file": VERSION_NOTE_FILE,
    }

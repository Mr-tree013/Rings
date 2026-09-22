"""The whitelisted eHall pipeline: the only place this project drives a browser (ADR-0025)."""

from assistant.adapters.ehall.executor import (
    E_HALL_CERTIFICATE_ACTION_TYPE,
    EHallCertificateExecutor,
)
from assistant.adapters.ehall.nju_certificate import NjuCertificateGateway
from assistant.adapters.ehall.page import PlaywrightEHallPage
from assistant.adapters.ehall.session import (
    ALLOWED_TOP_LEVEL_ORIGINS,
    EHALL_HOME_URL,
    ChromiumRuntime,
    EHallBrowserSession,
    check_top_level_origin,
    chromium_runtime_available,
    chromium_runtime_state,
    ehall_profile_dir,
    expected_chromium_builds,
)

__all__ = [
    "ALLOWED_TOP_LEVEL_ORIGINS",
    "EHALL_HOME_URL",
    "E_HALL_CERTIFICATE_ACTION_TYPE",
    "ChromiumRuntime",
    "EHallBrowserSession",
    "EHallCertificateExecutor",
    "NjuCertificateGateway",
    "PlaywrightEHallPage",
    "check_top_level_origin",
    "chromium_runtime_available",
    "chromium_runtime_state",
    "ehall_profile_dir",
    "expected_chromium_builds",
]

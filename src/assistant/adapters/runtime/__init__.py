"""Runtime infrastructure that is neither an external integration nor a data store (ADR-0032).

`permissions.py` sets owner-only modes on the private objects this project creates;
`instance_lock.py` is the single-instance guarantee for `assistantd`. Both are adapters because
both are operating-system facts — file modes and advisory locks — rather than domain rules, and the
kernel-specific call (`fcntl`) must not leak further up the stack.
"""

from assistant.adapters.runtime.instance_lock import (
    InstanceLock,
    InstanceLockHeld,
    daemon_lock_path,
)
from assistant.adapters.runtime.permissions import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    ensure_private_file,
    file_mode,
    is_world_writable,
)

__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "InstanceLock",
    "InstanceLockHeld",
    "daemon_lock_path",
    "ensure_private_directory",
    "ensure_private_file",
    "file_mode",
    "is_world_writable",
]

# -*- coding: utf-8 -*-
"""Safe JSON session with filename sanitization for cross-platform
compatibility.

Windows filenames cannot contain: \\ / : * ? " < > |
This module wraps agentscope's JSONSession so that session_id and user_id
are sanitized before being used as filenames.

Sessions are stored per-user at users/<user_id>/sessions/<session_id>.json.
"""
import os
import re

from agentscope.session import JSONSession

from ...config.utils import get_sessions_dir_for_user


# Characters forbidden in Windows filenames
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str) -> str:
    """Replace characters that are illegal in Windows filenames with ``--``.

    >>> sanitize_filename('discord:dm:12345')
    'discord--dm--12345'
    >>> sanitize_filename('normal-name')
    'normal-name'
    """
    return _UNSAFE_FILENAME_RE.sub("--", name)


class SafeJSONSession(JSONSession):
    """JSONSession subclass that sanitizes session_id / user_id before
    building file paths.

    Sessions are stored under users/<user_id>/sessions/<session_id>.json.

    All other behaviour (save / load / state management) is inherited
    unchanged from :class:`JSONSession`.
    """

    def _get_save_path(self, session_id: str, user_id: str) -> str:
        """Return a filesystem-safe save path.

        Overrides the parent implementation to ensure the generated
        filename is valid on Windows, macOS and Linux. When user
        isolation is enabled, uses per-user sessions directory.
        """
        sessions_dir = get_sessions_dir_for_user(user_id)
        os.makedirs(str(sessions_dir), exist_ok=True)
        safe_sid = sanitize_filename(session_id)
        file_path = f"{safe_sid}.json"
        return os.path.join(str(sessions_dir), file_path)

# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,too-many-return-statements
import os
import mimetypes
import unicodedata
from urllib.parse import quote, urlparse, unquote

from agentscope.tool import ToolResponse
from agentscope.message import (
    TextBlock,
    ImageBlock,
    AudioBlock,
    VideoBlock,
)

from ...context import get_process_request_meta

from ..schema import FileBlock


def _user_facing_file_url(absolute_path: str) -> str:
    """URL placed in ImageBlock/FileBlock for the console.

    LCAgent ``/console/api/files/download`` accepts paths under ``/tmp/`` and
    ``/app/upload/``. The web UI maps those paths to
    ``.../files/download?file_path=...``.

    Using ``file://...`` here can cause an intermediate layer to synthesize a
    broken HTTP URL (e.g. ``?path=``). For those prefixes, pass the absolute
    path as a plain string instead.
    """
    normalized = absolute_path.replace("\\", "/")
    if normalized.startswith("/tmp/") or normalized.startswith("/app/upload/"):
        return normalized
    return f"file://{absolute_path}"


def _auto_as_type(mt: str) -> str:
    if mt.startswith("image/"):
        return "image"
    if mt.startswith("audio/"):
        return "audio"
    if mt.startswith("video/"):
        return "video"
    return "file"


def _guess_mime_from_name(name: str) -> str:
    mime_type, _ = mimetypes.guess_type(name)
    if mime_type is None:
        return "application/octet-stream"
    return mime_type


def _resolve_lcagent_public_origin(meta: dict) -> str:
    """Origin (scheme+host[:port]) for browser-facing LCAgent console."""
    pub = (meta.get("lcagent_console_public_base") or "").strip().rstrip("/")
    if pub:
        return pub
    api_base = (meta.get("lcagent_console_api_base") or "").strip().rstrip("/")
    if not api_base:
        return ""
    for suffix in ("/console/api", "/console"):
        if api_base.endswith(suffix):
            return api_base[: -len(suffix)].rstrip("/")
    return api_base


def _is_lcagent_server_path(posix_path: str) -> bool:
    """Paths that may exist on LCAgent API pod (same whitelist as file download)."""
    return posix_path.startswith("/tmp/") or posix_path.startswith("/app/upload/")


def _strip_url_scheme(url: str) -> str:
    """Return host[:port]/path part without http(s) prefix."""
    u = url.strip().rstrip("/")
    low = u.lower()
    if low.startswith("https://"):
        return u[8:]
    if low.startswith("http://"):
        return u[7:]
    return u


def _apply_optional_file_url_scheme(origin: str) -> str:
    """Normalize scheme for ``/console/api/files/download`` links.

    By default the origin from ``meta.lcagent_console_public_base`` is used as-is
    (production should inject ``https://`` via ``LCAGENT_CONSOLE_PUBLIC_BASE``).

    Optional override: set env ``LCAGENT_COPAW_FILE_URL_SCHEME`` to ``http`` or
    ``https`` to force that scheme (e.g. ``https`` in production when meta lacks
    a scheme or is misconfigured).
    """
    o = origin.strip().rstrip("/")
    if not o:
        return o
    scheme = (os.environ.get("LCAGENT_COPAW_FILE_URL_SCHEME") or "").strip().lower()
    if scheme in ("http", "https"):
        rest = _strip_url_scheme(o)
        return f"{scheme}://{rest}".rstrip("/")
    return o


def _lcagent_download_file_url(origin: str, posix_path: str) -> str:
    origin_norm = _apply_optional_file_url_scheme(origin)
    return (
        f"{origin_norm}/console/api/files/download"
        f"?file_path={quote(posix_path, safe='')}"
    )


_MISSING_ORIGIN_FOR_LCAGENT_PATH_MSG = (
    "错误：无法将 LCAgent 服务端路径转为下载链接："
    "缺少 meta 中的 lcagent_console_public_base（或可用的 "
    "lcagent_console_api_base）。"
    "请通过 LCAgent 首页 lcClaw访问，以便注入控制台对外地址。"
)


def _tool_response_for_media_url(
    file_url: str,
    mime_type: str,
    filename: str,
) -> ToolResponse:
    """Build Image/Audio/Video/File blocks; ``file_url`` is http(s), file://, or path token."""
    as_type = _auto_as_type(mime_type)
    source = {"type": "url", "url": file_url}
    ok = TextBlock(type="text", text="File sent successfully.")

    if as_type == "image":
        return ToolResponse(
            content=[ImageBlock(type="image", source=source), ok],
        )
    if as_type == "audio":
        return ToolResponse(
            content=[AudioBlock(type="audio", source=source), ok],
        )
    if as_type == "video":
        return ToolResponse(
            content=[VideoBlock(type="video", source=source), ok],
        )
    return ToolResponse(
        content=[
            FileBlock(
                type="file",
                source=source,
                filename=filename or "file",
            ),
            ok,
        ],
    )


async def send_file_to_user(
    file_path: str,
) -> ToolResponse:
    """Send a file to the user.

    Accepts:
    - **Local paths** (``os.path.exists`` on this CoPaw container): always emit
      URLs for the **embedded console** to resolve via ``file://`` or bare
      ``/tmp/`` / ``/app/upload/`` tokens → ``/copaw/api/files/preview/...``.
      Never use LCAgent ``/console/api/files/download`` for local files: that
      endpoint reads **LCAgent's** disk; CoPaw's ``/app/upload/`` or workspace
      paths are not the same files unless uploaded through LCAgent.
    - ``http(s)://`` URLs: passed through to the client for preview/download.
    - LCAgent server paths ``/app/upload/...`` or ``/tmp/...`` when the file is
      **not** present in this container: builds the same URL as the console
      ``/console/api/files/download?file_path=...`` using
      ``meta.lcagent_console_public_base`` (requires LCAgent CoPaw proxy).
      Production typically uses ``https`` from ``LCAGENT_CONSOLE_PUBLIC_BASE``;
      optional env ``LCAGENT_COPAW_FILE_URL_SCHEME=http|https`` forces the scheme.

    Args:
        file_path (`str`):
            Path or URL to send.

    Returns:
        `ToolResponse`:
            The tool response containing the file or an error message.
    """
    raw_in = unicodedata.normalize("NFC", (file_path or "").strip())
    if not raw_in:
        return ToolResponse(
            content=[
                TextBlock(type="text", text="Error: file_path is empty."),
            ],
        )

    # Absolute HTTP(S): console or external link — no local existence check.
    if raw_in.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw_in)
        name = os.path.basename(unquote(parsed.path)) or "file"
        mime_type = _guess_mime_from_name(name)
        try:
            return _tool_response_for_media_url(raw_in, mime_type, name)
        except Exception as e:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: Send file failed due to \n{e}",
                    ),
                ],
            )

    file_path_local = os.path.expanduser(raw_in)

    if os.path.exists(file_path_local):
        if not os.path.isfile(file_path_local):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: The path {file_path_local} is not a file.",
                    ),
                ],
            )
        mime_type, _ = mimetypes.guess_type(file_path_local)
        if mime_type is None:
            mime_type = "application/octet-stream"
        try:
            absolute_path = os.path.abspath(file_path_local)
            # Local file always lives on this CoPaw process/container. Do NOT emit
            # ``.../console/api/files/download?file_path=...`` (LCAgent pod): that
            # URL only works when the bytes exist on LCAgent (e.g. after console
            # upload). Same path prefix ``/app/upload/`` on CoPaw is a different
            # filesystem than LCAgent's ``/app/upload/``. Use console preview paths
            # (``file://`` or bare ``/tmp/``/``/app/upload/`` tokens) so the browser
            # hits ``/copaw/api/files/preview/...`` for local files.
            file_url = _user_facing_file_url(absolute_path)
            return _tool_response_for_media_url(
                file_url,
                mime_type,
                os.path.basename(file_path_local),
            )
        except Exception as e:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: Send file failed due to \n{e}",
                    ),
                ],
            )

    posix_path = raw_in.replace("\\", "/")
    if _is_lcagent_server_path(posix_path):
        meta = get_process_request_meta()
        origin = _resolve_lcagent_public_origin(meta)
        if not origin:
            return ToolResponse(
                content=[
                    TextBlock(type="text", text=_MISSING_ORIGIN_FOR_LCAGENT_PATH_MSG),
                ],
            )
        full_url = _lcagent_download_file_url(origin, posix_path)
        name = os.path.basename(posix_path) or "file"
        mime_type = _guess_mime_from_name(name)
        try:
            return _tool_response_for_media_url(full_url, mime_type, name)
        except Exception as e:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Error: Send file failed due to \n{e}",
                    ),
                ],
            )

    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=f"Error: The file {file_path_local} does not exist.",
            ),
        ],
    )

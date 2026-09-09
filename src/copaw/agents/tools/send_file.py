# -*- coding: utf-8 -*-
# flake8: noqa: E501
# pylint: disable=line-too-long,too-many-return-statements
import os
import mimetypes
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse, unquote

from agentscope.tool import ToolResponse
from agentscope.message import (
    TextBlock,
    ImageBlock,
    AudioBlock,
    VideoBlock,
)

from ...constant import USERS_DIR
from ...context import get_process_request_meta

from ..schema import FileBlock
from .lcagent_media import (
    coerce_lcagent_static_urls_to_path_tokens,
    is_lcagent_server_path,
    is_path_covered_by_invoke,
    static_upload_url_to_lcagent_path,
)


def _working_abspath_to_preview_url(absolute_path: str) -> Optional[str]:
    """Map ``USERS_DIR/<user_id>/workspaces/...`` to browser path ``/copaw/api/files/preview/...``.

    Nginx exposes lcClaw as ``/copaw/api/`` → service ``/api/``; same-origin relative
    URLs work for ``<video src>`` and markdown links without frontend rewrites.
    """
    try:
        resolved = Path(absolute_path).resolve()
        base = Path(USERS_DIR).resolve()
        rel = resolved.relative_to(base)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if not parts or ".." in parts:
        return None
    tail = "/".join(quote(p, safe="") for p in parts)
    return f"/copaw/api/files/preview/{tail}"


def _user_facing_file_url(absolute_path: str) -> str:
    """URL placed in ImageBlock/FileBlock for the console.

    LCAgent ``/console/api/files/download`` accepts paths under ``/tmp/`` and
    ``/app/upload/``. The web UI maps those paths to
    ``.../files/download?file_path=...``.

    Workspace files under ``USERS_DIR`` (e.g. ``.../users/<id>/workspaces/...``)
    are converted by :func:`_working_abspath_to_preview_url` to
    ``/copaw/api/files/preview/<user_id>/workspaces/...``. Other allowed prefixes
    use bare ``/tmp/`` / ``/app/upload/`` path tokens (not ``file://``).

    Using ``file://...`` for other paths can cause an intermediate layer to
    synthesize a broken HTTP URL (e.g. ``?path=``). For the prefixes above, pass
    the absolute path as a plain string instead.
    """
    normalized = absolute_path.replace("\\", "/")
    if (
        normalized.startswith("/tmp/")
        or normalized.startswith("/app/upload/")
    ):
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
    return is_lcagent_server_path(posix_path)


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


_LCAGENT_UPLOAD_PREFIX = "/app/upload"


def _lcagent_static_file_url(origin: str, posix_path: str) -> str:
    """Convert /app/upload/xxx → {origin}/static/upload/xxx (nginx static, no auth required).

    Falls back to the download API for /tmp/ or other non-upload paths.
    """
    origin_norm = _apply_optional_file_url_scheme(origin)
    if posix_path.startswith(_LCAGENT_UPLOAD_PREFIX):
        rel = posix_path[len(_LCAGENT_UPLOAD_PREFIX):]
        return f"{origin_norm}/static/upload{rel}"
    return _lcagent_download_file_url(origin, posix_path)


_MISSING_ORIGIN_FOR_LCAGENT_PATH_MSG = (
    "错误：无法将 LCAgent 服务端路径转为下载链接："
    "缺少 meta 中的 lcagent_console_public_base（或可用的 "
    "lcagent_console_api_base）。"
    "请通过 LCAgent 首页 lcClaw访问，以便注入控制台对外地址。"
)


def _success_text_with_link(
    file_url: str,
    mime_type: str,
    filename: str,
) -> TextBlock:
    """Text shown after tool runs; keeps a renderable link when media blocks are stripped.

    OpenAI-formatter ``promote_tool_result_images`` can drop Image/File blocks from
    persisted tool output, leaving only text. Embedding markdown preserves the URL
    for console markdown and for stream parsers that inject image content.
    """
    name = (filename or "file").replace("[", "").replace("]", "") or "file"
    as_type = _auto_as_type(mime_type)
    if as_type == "image":
        line = f"![{name}]({coerce_lcagent_static_urls_to_path_tokens(file_url)})"
    else:
        line = f"[{name}]({coerce_lcagent_static_urls_to_path_tokens(file_url)})"
    return TextBlock(
        type="text",
        text=f"File sent successfully.\n\n{line}",
    )


def _tool_response_working_preview_url(
    preview_url: str,
    mime_type: str,
    filename: str,
) -> ToolResponse:
    """Structured file + text; URL is same-origin ``/copaw/api/files/preview/...``."""
    source = {"type": "url", "url": preview_url}
    ok = _success_text_with_link(preview_url, mime_type, filename)
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


def _tool_response_for_media_url(
    file_url: str,
    mime_type: str,
    filename: str,
) -> ToolResponse:
    """Build Image/Audio/Video/File blocks; ``file_url`` is http(s), file://, or path token."""
    as_type = _auto_as_type(mime_type)
    source = {"type": "url", "url": file_url}
    ok = _success_text_with_link(file_url, mime_type, filename)

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
    - **Local paths** (``os.path.exists`` on this lcClaw container): files under
      ``USERS_DIR/<user_id>/workspaces/...`` emit same-origin
      ``/copaw/api/files/preview/<user_id>/workspaces/...``; other allowed dirs use
      ``file://`` or bare ``/tmp/`` / ``/app/upload/`` tokens.
      Never use LCAgent ``/console/api/files/download`` for local files: that
      endpoint reads **LCAgent's** disk; lcClaw's ``/app/upload/`` or workspace
      paths are not the same files unless uploaded through LCAgent.
    - ``http(s)://`` URLs: passed through to the client for preview/download.
    - LCAgent server paths ``/app/upload/...`` or ``/tmp/...`` when the file is
      **not** present in this container: emits bare ``/app/upload/...`` path
      tokens (or ``/console/api/files/download?...`` when built upstream) for
      the LCAgent web UI to resolve—**not** ``/static/upload/`` on another host.
      If the same path was already returned by ``invoke_lcagent_published_app``
      in this request, returns a short note instead of duplicating media.

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
        mapped = static_upload_url_to_lcagent_path(raw_in)
        if mapped and is_path_covered_by_invoke(mapped):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "该文件已由 invoke_lcagent_published_app 返回并应出现在"
                            "面向用户的正文中，无需重复 send_file_to_user。"
                        ),
                    ),
                ],
            )
        if mapped and _is_lcagent_server_path(mapped):
            name = os.path.basename(mapped) or "file"
            mime_type = _guess_mime_from_name(name)
            try:
                return _tool_response_for_media_url(
                    _user_facing_file_url(mapped),
                    mime_type,
                    name,
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
        parsed = urlparse(raw_in)
        name = os.path.basename(unquote(parsed.path)) or "file"
        mime_type = _guess_mime_from_name(name)
        try:
            return _tool_response_for_media_url(
                coerce_lcagent_static_urls_to_path_tokens(raw_in),
                mime_type,
                name,
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
            # Local file always lives on this lcClaw process/container. Do NOT emit
            # ``.../console/api/files/download?file_path=...`` (LCAgent pod): that
            # URL only works when the bytes exist on LCAgent (e.g. after console
            # upload). Same path prefix ``/app/upload/`` on lcClaw is a different
            # filesystem than LCAgent's ``/app/upload/``. Workspace trees under
            # ``USERS_DIR`` use ``/copaw/api/files/preview/<user>/workspaces/...``;
            # other allowed dirs use bare path tokens or ``file://``.
            preview_url = _working_abspath_to_preview_url(absolute_path)
            if preview_url:
                return _tool_response_working_preview_url(
                    preview_url,
                    mime_type,
                    os.path.basename(file_path_local),
                )
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
        if is_path_covered_by_invoke(posix_path):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "该文件已由 invoke_lcagent_published_app 返回并应出现在"
                            "面向用户的正文中，无需重复 send_file_to_user。"
                        ),
                    ),
                ],
            )
        name = os.path.basename(posix_path) or "file"
        mime_type = _guess_mime_from_name(name)
        file_url = _user_facing_file_url(posix_path)
        try:
            return _tool_response_for_media_url(file_url, mime_type, name)
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

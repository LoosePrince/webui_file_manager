#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebUI 文件管理器插件

功能：
1) 浏览/预览 MCDR 根目录下的所有文件（非超管：只读）
2) 超级管理员额外可浏览/读写所有本地磁盘（scope=drive）

安全策略：
- 前端传入的 scope/path 会被后端归一化（去掉前导 /、禁止 ..、禁止 ':' 等）
- 所有落盘/读取都要校验解析后的真实路径仍位于允许 base 之下
"""

from __future__ import annotations

import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from mcdreforged.api.types import PluginServerInterface
from starlette.responses import FileResponse, Response

PLUGIN_ID = "webui_file_manager"

_PREVIEW_LIMIT_BYTES = 2 * 1024 * 1024  # 2MiB
_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024  # 10MiB（单文件）

_PLUGIN_DIR = Path(__file__).resolve().parent
_HTML_FILE = _PLUGIN_DIR / "static" / "demo.html"
_CONFIG_HTML_PATH = Path("./config") / "webui_file_manager" / "demo.html"

# 在 on_load 里计算并缓存
_MC_ROOT: Optional[Path] = None


def _q_str(query: dict[str, Any], key: str) -> str:
    v = query.get(key)
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v) if v is not None else ""


def _sanitize_relpath(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip()
    if rel in ("", "/"):
        return ""

    # 禁止绝对路径/盘符
    rel = rel.lstrip("/")
    if ":" in rel:
        raise ValueError("invalid path")

    parts: list[str] = []
    for p in rel.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise ValueError("path traversal is denied")
        parts.append(p)

    return "/".join(parts)


def _resolve_in_base(base: Path, rel: str) -> Path:
    base_resolved = base.resolve()
    candidate = (base_resolved / rel).resolve() if rel else base_resolved

    # 确保 candidate 始终落在 base_resolved 内
    try:
        candidate.relative_to(base_resolved)
    except Exception as e:
        raise ValueError("outside of allowed scope") from e

    return candidate


def _sanitize_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("invalid name")
    n = name.strip()
    if not n or n in (".", ".."):
        raise ValueError("invalid name")
    if ":" in n or "/" in n or "\\" in n:
        raise ValueError("invalid name")
    return n


def _remove_target(target: Path, *, recursive: bool) -> None:
    if not target.exists():
        return
    if target.is_dir():
        if not recursive:
            raise ValueError("directory delete requires recursive")
        shutil.rmtree(target)
    else:
        target.unlink()


def _get_auth(params: dict[str, Any]) -> dict[str, Any]:
    auth = params.get("auth") or {}
    return auth if isinstance(auth, dict) else {}


def _is_super_admin(params: dict[str, Any]) -> bool:
    auth = _get_auth(params)
    return bool(auth.get("is_super_admin", False))


def _list_drives() -> list[str]:
    # 简化实现：列出所有存在的盘符根路径
    out: list[str] = []
    for code in range(ord("A"), ord("Z") + 1):
        letter = chr(code)
        p = Path(f"{letter}:\\")
        try:
            if p.exists():
                out.append(letter)
        except Exception:
            continue
    return out


def _guess_text_decode(data: bytes) -> Optional[tuple[str, str]]:
    if b"\0" in data:
        return None

    # 常见编码尝试（尽量避免把二进制误当文本）
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936"):
        try:
            return data.decode(enc), enc
        except Exception:
            continue
    return None


def _api_handler(url_path: str, params: dict[str, Any]) -> dict[str, Any] | Response:
    method = params.get("method", "GET")
    query = params.get("query") or {}
    body = params.get("body")

    auth = _get_auth(params)
    is_super = bool(auth.get("is_super_admin", False))

    if url_path == "fs_info" and method == "GET":
        drives = _list_drives() if is_super else []
        return {
            "ok": True,
            "is_super_admin": is_super,
            "allowed_scopes": ["mcdr"] + (["drive"] if is_super else []),
            "drives": drives,
            "limits": {
                "preview_bytes": _PREVIEW_LIMIT_BYTES,
                "upload_bytes": _UPLOAD_LIMIT_BYTES,
            },
        }

    # 以下 API 基于 scope/path
    scope = _q_str(query, "scope") or "mcdr"
    scope = scope.lower()
    try:
        rel = _sanitize_relpath(_q_str(query, "path"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    if scope == "mcdr":
        if not _MC_ROOT:
            return {"ok": False, "error": "mcdr_root not ready"}
        base = _MC_ROOT
    elif scope == "drive":
        if not is_super:
            return {"ok": False, "error": "super admin required"}
        drive = _q_str(query, "drive").upper().replace(":", "")
        if not drive or len(drive) != 1:
            return {"ok": False, "error": "invalid drive"}
        base = Path(f"{drive}:\\")
        if not base.exists():
            return {"ok": False, "error": f"drive not exists: {drive}"}
    else:
        return {"ok": False, "error": "invalid scope"}

    # list
    if url_path == "list" and method == "GET":
        try:
            target = _resolve_in_base(base, rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not target.exists() or not target.is_dir():
            return {"ok": False, "error": "not a directory"}

        items: list[dict[str, Any]] = []
        for child in sorted(target.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name.lower())):
            try:
                st = child.stat()
                items.append(
                    {
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": None if child.is_dir() else st.st_size,
                        "mtime": int(st.st_mtime),
                        "ext": (child.suffix or "").lstrip("."),
                    }
                )
            except Exception:
                items.append(
                    {
                        "name": child.name,
                        "is_dir": child.is_dir(),
                        "size": None,
                        "mtime": 0,
                        "ext": (child.suffix or "").lstrip("."),
                    }
                )
        return {"ok": True, "items": items, "path": rel, "scope": scope}

    # read / preview
    if url_path == "read" and method == "GET":
        try:
            target = _resolve_in_base(base, rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not target.exists() or not target.is_file():
            return {"ok": False, "error": "file not found"}

        try:
            size = target.stat().st_size
            if size > _PREVIEW_LIMIT_BYTES:
                return {"ok": False, "error": "file too large to preview", "size": size}
            with open(target, "rb") as f:
                data = f.read(_PREVIEW_LIMIT_BYTES + 1)
        except Exception as e:
            return {"ok": False, "error": f"read failed: {e}"}

        if len(data) > _PREVIEW_LIMIT_BYTES:
            return {"ok": False, "error": "file too large to preview"}

        decoded = _guess_text_decode(data)
        if decoded is None:
            return {"ok": False, "error": "not a supported text file"}

        text, enc = decoded
        return {
            "ok": True,
            "text": text,
            "encoding": enc,
            "size": len(data),
            "path": rel,
            "scope": scope,
        }

    # download
    if url_path == "download" and method == "GET":
        try:
            target = _resolve_in_base(base, rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not target.exists() or not target.is_file():
            return {"ok": False, "error": "file not found"}

        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return FileResponse(str(target), media_type=mime, filename=target.name)

    # copy (super admin)
    if url_path == "copy" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid json body"}

        src_path = body.get("src_path")
        dst_dir = body.get("dst_dir")
        dst_name = body.get("dst_name")
        recursive = bool(body.get("recursive", True))
        overwrite = bool(body.get("overwrite", False))

        if not isinstance(src_path, str) or not isinstance(dst_dir, str) or not isinstance(dst_name, str):
            return {"ok": False, "error": "invalid params"}

        if not src_path or src_path.strip() in ("", "/"):
            return {"ok": False, "error": "refuse to copy scope root"}

        try:
            src_rel = _sanitize_relpath(src_path)
            dst_dir_rel = _sanitize_relpath(dst_dir)
            dst_leaf = _sanitize_name(dst_name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        src_target = _resolve_in_base(base, src_rel)
        if not src_target.exists():
            return {"ok": False, "error": "source not found"}

        dst_dir_target = _resolve_in_base(base, dst_dir_rel)
        if not dst_dir_target.exists() or not dst_dir_target.is_dir():
            return {"ok": False, "error": "destination directory not found"}

        dst_target = (dst_dir_target / dst_leaf).resolve()
        try:
            dst_target.relative_to(base.resolve())
        except Exception:
            return {"ok": False, "error": "outside of allowed scope"}

        if src_target.is_dir():
            if not recursive:
                return {"ok": False, "error": "directory copy requires recursive"}
            if dst_target == src_target:
                return {"ok": False, "error": "cannot copy to itself"}
            try:
                dst_target.relative_to(src_target)
                return {"ok": False, "error": "cannot copy into source subdirectory"}
            except ValueError:
                pass

        if dst_target.exists():
            if not overwrite:
                return {"ok": False, "error": "target already exists"}
            try:
                _remove_target(dst_target, recursive=True)
            except Exception as e:
                return {"ok": False, "error": f"remove target failed: {e}"}

        try:
            if src_target.is_dir():
                shutil.copytree(src_target, dst_target)
            else:
                shutil.copy2(src_target, dst_target)
        except Exception as e:
            return {"ok": False, "error": f"copy failed: {e}"}

        dst_rel = (dst_dir_rel + "/" + dst_leaf).strip("/") if dst_dir_rel else dst_leaf
        return {"ok": True, "message": "copied", "dst_path": dst_rel, "scope": scope}

    # move (super admin)
    if url_path == "move" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid json body"}

        src_path = body.get("src_path")
        dst_dir = body.get("dst_dir")
        dst_name = body.get("dst_name")
        recursive = bool(body.get("recursive", True))
        overwrite = bool(body.get("overwrite", False))

        if not isinstance(src_path, str) or not isinstance(dst_dir, str) or not isinstance(dst_name, str):
            return {"ok": False, "error": "invalid params"}

        if not src_path or src_path.strip() in ("", "/"):
            return {"ok": False, "error": "refuse to move scope root"}

        try:
            src_rel = _sanitize_relpath(src_path)
            dst_dir_rel = _sanitize_relpath(dst_dir)
            dst_leaf = _sanitize_name(dst_name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        src_target = _resolve_in_base(base, src_rel)
        if not src_target.exists():
            return {"ok": False, "error": "source not found"}

        if src_target.is_dir() and not recursive:
            return {"ok": False, "error": "directory move requires recursive"}

        dst_dir_target = _resolve_in_base(base, dst_dir_rel)
        if not dst_dir_target.exists() or not dst_dir_target.is_dir():
            return {"ok": False, "error": "destination directory not found"}

        dst_target = (dst_dir_target / dst_leaf).resolve()
        try:
            dst_target.relative_to(base.resolve())
        except Exception:
            return {"ok": False, "error": "outside of allowed scope"}

        if src_target.is_dir():
            if dst_target == src_target:
                return {"ok": False, "error": "cannot move to itself"}
            try:
                dst_target.relative_to(src_target)
                return {"ok": False, "error": "cannot move into source subdirectory"}
            except ValueError:
                pass

        if dst_target.exists():
            if not overwrite:
                return {"ok": False, "error": "target already exists"}
            try:
                _remove_target(dst_target, recursive=True)
            except Exception as e:
                return {"ok": False, "error": f"remove target failed: {e}"}

        try:
            shutil.move(str(src_target), str(dst_target))
        except Exception as e:
            return {"ok": False, "error": f"move failed: {e}"}

        dst_rel = (dst_dir_rel + "/" + dst_leaf).strip("/") if dst_dir_rel else dst_leaf
        return {"ok": True, "message": "moved", "dst_path": dst_rel, "scope": scope}

    # save (text only, super admin)
    if url_path == "save" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid json body"}
        content = body.get("content")
        if not isinstance(content, str):
            return {"ok": False, "error": "content must be a string"}
        if len(content.encode("utf-8")) > _PREVIEW_LIMIT_BYTES:
            return {"ok": False, "error": "content too large"}

        try:
            target = _resolve_in_base(base, rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if target.exists() and target.is_dir():
            return {"ok": False, "error": "target is a directory"}

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return {"ok": False, "error": f"save failed: {e}"}

        return {"ok": True, "message": "saved", "path": rel, "scope": scope}

    # upload (super admin)
    if url_path == "upload" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid form body"}

        raw = body.get("file")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, dict) or raw.get("type") != "file":
            return {"ok": False, "error": "expected multipart field 'file'"}
        data = raw.get("data") or b""
        if not isinstance(data, (bytes, bytearray)):
            return {"ok": False, "error": "invalid file data"}
        if len(data) > _UPLOAD_LIMIT_BYTES:
            return {"ok": False, "error": "upload file too large"}

        # query.path 表示目标目录（相对 base）
        target_dir = None
        try:
            target_dir = _resolve_in_base(base, rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not target_dir.exists() or not target_dir.is_dir():
            return {"ok": False, "error": "upload target dir not found"}

        filename = raw.get("filename") or "upload.bin"
        filename = os.path.basename(str(filename))
        if not filename or ":" in filename or "/" in filename or "\\" in filename:
            return {"ok": False, "error": "invalid filename"}

        target_file = (target_dir / filename).resolve()
        try:
            target_file.relative_to(base.resolve())
        except Exception:
            return {"ok": False, "error": "outside of allowed scope"}

        try:
            with open(target_file, "wb") as f:
                f.write(data)
        except Exception as e:
            return {"ok": False, "error": f"upload failed: {e}"}

        return {"ok": True, "message": "uploaded", "filename": filename}

    # delete (super admin)
    if url_path == "delete" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid json body"}
        del_path = _q_str(body, "path") if "path" in body else _q_str(query, "path")
        recursive = bool(body.get("recursive", True))

        try:
            del_rel = _sanitize_relpath(del_path)
            target = _resolve_in_base(base, del_rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if target == base:
            return {"ok": False, "error": "refuse to delete scope root"}

        try:
            if target.is_dir():
                if not recursive:
                    return {"ok": False, "error": "directory delete requires recursive"}
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception as e:
            return {"ok": False, "error": f"delete failed: {e}"}

        return {"ok": True, "message": "deleted"}

    # rename (super admin)
    if url_path == "rename" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid json body"}
        from_path = body.get("path")
        new_name = body.get("new_name")
        if not isinstance(from_path, str) or not isinstance(new_name, str):
            return {"ok": False, "error": "invalid params"}
        if not new_name or ":" in new_name or "/" in new_name or "\\" in new_name:
            return {"ok": False, "error": "invalid new_name"}

        try:
            from_rel = _sanitize_relpath(from_path)
            from_target = _resolve_in_base(base, from_rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if not from_target.exists():
            return {"ok": False, "error": "source not found"}
        if from_target == base:
            return {"ok": False, "error": "refuse to rename scope root"}

        to_target = (from_target.parent / new_name).resolve()
        try:
            to_target.relative_to(base.resolve())
        except Exception:
            return {"ok": False, "error": "outside of allowed scope"}

        try:
            from_target.rename(to_target)
        except Exception as e:
            return {"ok": False, "error": f"rename failed: {e}"}

        return {"ok": True, "message": "renamed", "new_path": new_name}

    # mkdir (super admin)
    if url_path == "mkdir" and method == "POST":
        if not is_super:
            return {"ok": False, "error": "read only"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid json body"}
        new_dir_path = body.get("path")
        if not isinstance(new_dir_path, str):
            return {"ok": False, "error": "path must be string"}

        try:
            new_rel = _sanitize_relpath(new_dir_path)
            new_target = _resolve_in_base(base, new_rel)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        if new_target == base:
            return {"ok": False, "error": "refuse to mkdir scope root"}

        try:
            new_target.mkdir(parents=True, exist_ok=False)
        except Exception as e:
            return {"ok": False, "error": f"mkdir failed: {e}"}

        return {"ok": True, "message": "created"}

    return {"ok": False, "error": "no route", "url_path": url_path, "method": method}


def on_load(server: PluginServerInterface, old) -> None:
    global _MC_ROOT
    webui = server.get_plugin_instance("guguwebui")
    if not webui or not hasattr(webui, "register_plugin_page"):
        server.logger.warning("[%s] guguwebui 未找到 register_plugin_page，跳过注册", PLUGIN_ID)
        return
    try:
        data_folder = Path(server.get_data_folder()).resolve()
        # 与工程内其它逻辑保持一致：mcdr_root = dirname(dirname(data_folder))
        _MC_ROOT = data_folder.parent.parent
    except Exception as e:
        server.logger.error("[%s] 计算 MCDR 根目录失败: %s", PLUGIN_ID, e)
        _MC_ROOT = None

    def _extract_bundled_demo_html() -> bool:
        """
        从 mcdr 内置资源里提取 static/demo.html 到 config/webui_file_manager/demo.html。

        参考 guguwebui 的 file_util.py：使用 server.open_bundled_file() 读取打包资源，
        避免依赖 static/ 是否已经被解压到磁盘。
        """
        try:
            _CONFIG_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
            with server.open_bundled_file("static/demo.html") as file_handler:
                data = file_handler.read()
            with open(_CONFIG_HTML_PATH, "wb") as f:
                f.write(data)
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            server.logger.warning("[%s] 从内置资源提取 demo.html 失败: %s", PLUGIN_ID, e)
            return False

    # HTML 加载策略：
    # 1) 始终尝试从 mcdr 内置 static/demo.html 覆盖 config/webui_file_manager/demo.html
    # 2) 内置资源不存在时，才回退到文件系统的 static/demo.html 或已存在的 config
    config_exists = _CONFIG_HTML_PATH.is_file()
    extracted = _extract_bundled_demo_html()

    if not extracted:
        if _HTML_FILE.is_file():
            try:
                _CONFIG_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_HTML_FILE, _CONFIG_HTML_PATH)
                server.logger.info(
                    "[%s] 已覆盖 config/webui_file_manager/demo.html（来源：文件系统 static/demo.html）",
                    PLUGIN_ID,
                )
            except Exception as e:
                server.logger.warning("[%s] 覆盖 config 失败: %s", PLUGIN_ID, e)
        elif config_exists:
            server.logger.warning(
                "[%s] static/demo.html（内置资源/文件系统）不存在，回退使用 config 内的 demo.html: %s",
                PLUGIN_ID,
                _CONFIG_HTML_PATH,
            )
        else:
            server.logger.error(
                "[%s] 找不到 HTML 文件：内置 static/demo.html 或文件系统 static=%s 或 config=%s",
                PLUGIN_ID,
                _HTML_FILE,
                _CONFIG_HTML_PATH,
            )
            return

    webui.register_plugin_page(
        PLUGIN_ID,
        str(_CONFIG_HTML_PATH.resolve())
        if _CONFIG_HTML_PATH.exists() else str(_HTML_FILE),
        name="文件管理器",
        api_handler=_api_handler,
        upload_max_bytes=_UPLOAD_LIMIT_BYTES,
    )
    server.logger.info("[%s] 已注册文件管理器页面与 API", PLUGIN_ID)


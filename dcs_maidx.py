
from __future__ import annotations

import hashlib
import os
import re
import ssl
from typing import Any, Dict, List, Optional

MAIDX_GET_INFO_URL = "https://maidx-auth.mug.icu/getInfo"
MAIDX_GET_CAB_INFO_URL = "https://maidx-auth.mug.icu/getCabInfo"
MAIDX_ACCEPT = "240927"
MAIDX_TIMEOUT = 15

_CAB_LABELS = ("一号机", "二号机")

_SERVER_NAMES = {
    "wahlap": "国服",
    "wahlap-pending": "国服（正在切换）",
    "2077": "2077",
    "jp": "日版",
    "jp-from-wahlap": "日版（正在切换）",
    "uninstall": "正在卸载",
}

_NO_CERT_REPLY = "还没配置 maidx 证书，请在插件配置里把 maidx_cert 填成证书文件（.p12）的绝对路径后再试。"
_CERT_READ_ERROR = "读取 maidx 证书失败：{detail}"
_CERT_PASSWORD_ERROR = "maidx 证书密码不对，请检查插件配置里的 maidx_cert_password。"


def _server_name(value: Any) -> str:
    key = str(value or "").strip()
    return _SERVER_NAMES.get(key, key or "未知")


def _format_login_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.match(r"^(?:\d{4}[-/])?(\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})", raw)
    if match:
        return (
            f"{int(match.group(1)):02d}-{int(match.group(2)):02d} "
            f"{int(match.group(3)):02d}:{match.group(4)}"
        )
    return raw


def _cab_name(cab: Dict[str, Any]) -> str:
    return str(cab.get("remark") or cab.get("name") or "").strip()


def _latest_login(login: Any) -> Optional[str]:
    if not isinstance(login, list):
        return None
    for item in login:
        if not isinstance(item, dict):
            continue
        when = _format_login_date(item.get("login_date"))
        who = str(item.get("user_name") or "").strip()
        if when or who:
            return f"{when} {who}".strip()
    return None


def _pem_cache(cert_path: str, password: str, cache_dir: str) -> tuple[str, str]:
    """把 p12 解成 pem 缓存起来，返回 (证书, 私钥) 路径。"""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        pkcs12,
    )

    stat = os.stat(cert_path)
    tag = hashlib.sha1(
        f"{os.path.abspath(cert_path)}|{stat.st_mtime_ns}|{stat.st_size}|{password}".encode("utf-8")
    ).hexdigest()[:16]
    cert_pem = os.path.join(cache_dir, f"maidx_{tag}.crt.pem")
    key_pem = os.path.join(cache_dir, f"maidx_{tag}.key.pem")
    if os.path.exists(cert_pem) and os.path.exists(key_pem):
        return cert_pem, key_pem

    with open(cert_path, "rb") as fh:
        blob = fh.read()
    try:
        key_obj, cert_obj, _ = pkcs12.load_key_and_certificates(
            blob, password.encode("utf-8") if password else None, backend=default_backend()
        )
    except ValueError as exc:
        raise RuntimeError("password") from exc
    if cert_obj is None or key_obj is None:
        raise RuntimeError("badcert")

    # 清掉这个证书的旧缓存
    for name in os.listdir(cache_dir):
        if name.startswith("maidx_") and name.endswith(".pem") and tag not in name:
            try:
                os.remove(os.path.join(cache_dir, name))
            except OSError:
                pass

    with open(cert_pem, "wb") as fh:
        fh.write(cert_obj.public_bytes(Encoding.PEM))
    with open(key_pem, "wb") as fh:
        fh.write(key_obj.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()))
    return cert_pem, key_pem


def _ssl_context(cert_path: str, password: str, cache_dir: str):
    cert_pem, key_pem = _pem_cache(cert_path, password, cache_dir)
    ctx = ssl.create_default_context()
    ctx.load_cert_chain(cert_pem, key_pem)
    return ctx


async def query_dx_players(cert_path: str = "", password: str = "", cache_dir: str = "") -> str:
    import aiohttp

    path = str(cert_path or "").strip()
    if not path:
        return _NO_CERT_REPLY
    if not os.path.isfile(path):
        return _CERT_READ_ERROR.format(detail="找不到证书文件")

    cache = str(cache_dir or "").strip() or os.path.dirname(path)
    os.makedirs(cache, exist_ok=True)
    try:
        ctx = _ssl_context(path, str(password or "").strip(), cache)
    except RuntimeError as exc:
        if str(exc) == "password":
            return _CERT_PASSWORD_ERROR
        return _CERT_READ_ERROR.format(detail="证书内容解析失败")
    except Exception as exc:
        return _CERT_READ_ERROR.format(detail=str(exc)[:80])

    headers = {
        "Accept": MAIDX_ACCEPT,
        "User-Agent": "Mozilla/5.0",
    }
    timeout_cfg = aiohttp.ClientTimeout(total=MAIDX_TIMEOUT)

    try:
        connector = aiohttp.TCPConnector(ssl=ctx)
        async with aiohttp.ClientSession(timeout=timeout_cfg, connector=connector) as session:
            info = await _get_json(session, "GET", MAIDX_GET_INFO_URL, headers, None)

            if not isinstance(info, dict):
                return "查询机台列表失败，请稍后再试。"
            if not info.get("success"):
                message = str(info.get("message") or "").strip()
                if "证书" in message:
                    return f"maidx 拒绝了这张证书：{message}\n可能已过期或被替换，重新下载一张再试。"
                return message or "查询机台列表失败，请稍后再试。"

            cabs = info.get("cabs")
            if not isinstance(cabs, list) or not cabs:
                return "只发现 0 台机台。"

            lines: List[str] = []
            for index, cab in enumerate(cabs[:2]):
                if not isinstance(cab, dict):
                    continue
                keychip = str(cab.get("keychip") or "").strip()
                name = _cab_name(cab) or keychip or f"机台{index + 1}"
                label = _CAB_LABELS[index] if index < len(_CAB_LABELS) else f"{index + 1}号机"

                server_text = "未知"
                login_text: Optional[str] = None
                if keychip:
                    try:
                        cab_info = await _get_json(
                            session,
                            "POST",
                            MAIDX_GET_CAB_INFO_URL,
                            headers,
                            {"cab": keychip},
                        )
                    except Exception:
                        cab_info = None

                    if isinstance(cab_info, dict) and cab_info.get("success"):
                        cab_data = cab_info.get("cab") or {}
                        server_text = _server_name(cab_data.get("server_mode"))
                        pending = cab_data.get("server_mode_pending")
                        if pending and str(pending).strip() and str(pending) != str(cab_data.get("server_mode")):
                            server_text += f"（待切换：{_server_name(pending)}）"
                        login_text = _latest_login(cab_info.get("login"))

                lines.append(f"【{label} {name}】")
                if login_text:
                    lines.append(f"服务器：{server_text} ｜ 最近登录：{login_text}")
                else:
                    lines.append(f"服务器：{server_text} ｜ 暂无登录记录")

            result = "\n".join(lines)
            if len(cabs) < 2:
                result += f"\n只发现 {len(cabs)} 台机台。"
            return result
    except Exception:
        return "查询机台信息失败，请稍后再试。"


async def _get_json(
    session: Any,
    method: str,
    url: str,
    headers: Dict[str, str],
    data: Optional[Dict[str, str]],
) -> Any:
    async with session.request(method, url, headers=headers, data=data) as resp:
        return await resp.json(content_type=None)

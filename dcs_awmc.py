"""AWMC's signed bridge client and private score-import input parsing."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

BRIDGE_PATH = '/group-console/xf-bridge/v1/kanade'
QQ_RE = re.compile(r'[1-9][0-9]{4,11}')
_QR_RE = re.compile(r'SGWCMAID[A-Za-z0-9]{12,504}')
_QR_LINK_RE = re.compile(
    r'https?://wq\.wahlap\.net/qrcode/(?:img/(MAID[A-Za-z0-9]{20,160})\.png'
    r'|req/(MAID[A-Za-z0-9]{20,160})\.html)(?:[?#][^\s]*)?', re.IGNORECASE,
)
# Also catches malformed/truncated credentials before a chat logger or LLM sees them.
_SENSITIVE_RE = re.compile(r'SGWCMAID[A-Za-z0-9]+|wq\.wahlap\.net/qrcode/', re.IGNORECASE)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_QQ_IMAGE_HOSTS = {'gchat.qpic.cn', 'c2cpicdw.qpic.cn', 'multimedia.nt.qq.com.cn'}
JOB_LABELS = {'b50': '生成 B50', 'refresh': '无二维码更新', 'upload': '二维码传分', 'bind': '绑定舞萌账号'}
CHANNEL_LABELS = {'awmcnet': 'AWMCNET', 'fish': '水鱼', 'lxns': '落雪'}
CHANNEL_STATES = {'pending': '同步中', 'synced': '已同步', 'failed': '同步失败', 'skipped': '已跳过'}
COMMANDS = {
    'qbind': 'account', 'maiqbind': 'account', 'qbind状态': 'account', '绑定qq': 'account', 'mai状态': 'account', 'mymai': 'account',
    '用户协议': 'terms', 'mai用户协议': 'terms', '同意用户协议': 'accept', '撤回用户协议': 'revoke',
    '数据共享状态': 'sharing_status', '不同意共享我的数据': 'sharing_disable', '同意共享我的数据': 'sharing_enable',
    'u': 'refresh', '导': 'refresh', '更': 'refresh', '更新': 'refresh', 'mai对手同步': 'refresh',
    'maiu': 'upload', '上传水鱼': 'upload', 'mai上传b50': 'upload',
    'mai帮助': 'help', 'awmc帮助': 'help', 'mai账号': 'account',
    'mai绑定': 'bind', 'maibind': 'bind', '绑定舞萌': 'bind',
    '查进度': 'progress', '查任务': 'progress', 'mai查任务': 'progress',
}


def command_parts(text: str) -> tuple[str, str]:
    parts = text.strip().removeprefix('/').split(maxsplit=1)
    if not parts:
        return '', ''
    return COMMANDS.get(parts[0].lower(), ''), parts[1].strip() if len(parts) > 1 else ''


def extract_qr(text: str) -> str | None:
    text = text.strip()
    if match := _QR_RE.search(text):
        return match.group(0)
    if match := _QR_LINK_RE.search(text):
        return 'SGWC' + (match[1] or match[2]).upper()
    return None


def contains_qr(text: str) -> bool:
    return _SENSITIVE_RE.search(text) is not None


def safe_message(text: str) -> str:
    text = re.sub(r'https?://wq\.wahlap\.net/qrcode/\S+', '[二维码链接]', text, flags=re.IGNORECASE)
    return _SENSITIVE_RE.sub('[二维码]', text).replace('[二维码][REDACTED]', '[二维码]')


def _decode_image(blob: bytes) -> str | None:
    # Pillow checks dimensions before allocating and ZXing reads only QR codes.
    from io import BytesIO

    import zxingcpp
    from PIL import Image, ImageOps

    with Image.open(BytesIO(blob)) as image:
        if image.width * image.height > 25_000_000:
            return None
        image = ImageOps.exif_transpose(image).convert('L')
        image.thumbnail((4096, 4096))
        for barcode in zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode):
            if qr := extract_qr(barcode.text):
                return qr
    return None


async def qr_from_image(component, scan_gate: asyncio.Semaphore) -> str | None:
    """Decode an inline image or a trusted QQ CDN attachment without saving it."""
    try:
        # Do not queue images from general chat or let a burst pile up native decoders.
        await asyncio.wait_for(scan_gate.acquire(), timeout=0.1)
    except (asyncio.TimeoutError, TimeoutError):
        return None
    process = None
    try:
        options = [str(getattr(component, attr, '') or '') for attr in ('url', 'file')]
        blob = b''
        for raw in options:
            if raw.startswith('base64://'):
                encoded = raw.removeprefix('base64://')
                if len(encoded) <= (MAX_IMAGE_BYTES + 2) // 3 * 4:
                    blob = base64.b64decode(encoded, validate=True)
                break
            url = urlsplit(raw)
            if (url.scheme not in ('http', 'https') or url.hostname not in _QQ_IMAGE_HOSTS
                    or url.username or url.password or url.port not in (None, 80, 443)):
                continue
            import aiohttp

            secure_url = url._replace(scheme='https', netloc=url.hostname, fragment='').geturl()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.get(secure_url, allow_redirects=False) as response:
                    if response.status != 200 or (response.content_length or 0) > MAX_IMAGE_BYTES:
                        return None
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            return None
                    blob = bytes(data)
            break
        if not blob or len(blob) > MAX_IMAGE_BYTES:
            return None
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), '--decode-stdin',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(blob), timeout=5)
        return extract_qr(stdout.decode('ascii')) if process.returncode == 0 else None
    except (asyncio.TimeoutError, TimeoutError, OSError, ValueError):
        return None
    except Exception:
        # aiohttp's concrete errors vary; a broken remote image must never break chat.
        return None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        scan_gate.release()


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def request_id(event, kind: str) -> str:
    message_id = getattr(event.message_obj, 'message_id', None)
    if not message_id:
        return secrets.token_hex(16)
    identity = (str(event.unified_msg_origin), str(event.get_self_id()), str(event.get_sender_id()), str(message_id), kind)
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


def agreement_from(account: dict) -> dict:
    agreement = account.get('agreement')
    if (not isinstance(agreement, dict) or not isinstance(agreement.get('accepted'), bool)
            or not isinstance(agreement.get('version'), str) or not isinstance(agreement.get('message'), str)):
        raise BridgeError('response', '暂时无法读取 AWMC 用户协议，请稍后再试。')
    return agreement


def decode_bridge_image(value: object) -> bytes:
    try:
        if isinstance(value, str):
            image = base64.b64decode(value, validate=True)
            if image.startswith(b'\xff\xd8\xff'):
                return image
    except ValueError:
        pass
    raise BridgeError('response', 'AWMC 返回的 B50 图片无效。')


def format_progress(result: dict) -> str:
    lines = []
    if isinstance(job := result.get('job'), dict):
        label = JOB_LABELS.get(job.get('kind'), 'AWMC 任务')
        if job.get('status') == 'running':
            lines.append(f'{label}正在处理。')
        elif message := job.get('message'):
            lines.append(safe_message(str(message)))
    if isinstance(channels := result.get('channels'), dict):
        states = [f'{label} {CHANNEL_STATES[channels[name]]}' for name, label in CHANNEL_LABELS.items()
                  if channels.get(name) in CHANNEL_STATES]
        if states:
            lines.append('最近一次成绩同步 · ' + ' ｜ '.join(states))
    return '\n'.join(lines) or '暂时没有成绩任务记录。'


class AwmcBridge:
    def __init__(self, url: str, secret: str, *, allow_insecure_http: bool = False):
        self.url = url.strip().rstrip('/')
        self._secret = secret.strip()
        self._allow_insecure_http = bool(allow_insecure_http)
        self._session = None

    @property
    def enabled(self) -> bool:
        return bool(self.url or self._secret)

    def _origin(self) -> str:
        try:
            url = urlsplit(self.url)
            loopback = (url.hostname or '').lower() in {'127.0.0.1', 'localhost', '::1'}
            secure = url.scheme == 'https'
            # HTTP is explicit opt-in for an SSH tunnel or a trusted direct
            # origin. Prefer HTTPS: a score QR is a live account credential.
            insecure = url.scheme == 'http' and (loopback or self._allow_insecure_http)
            if ((secure or insecure) and url.hostname and not url.username and not url.password
                    and not url.query and not url.fragment and url.port != 0
                    and url.path in ('', BRIDGE_PATH, BRIDGE_PATH.removesuffix('/kanade'))
                    and len(self._secret) >= 32):
                return f'{url.scheme}://{url.netloc}'
        except ValueError:
            pass
        raise BridgeError('configuration', 'AWMC Bridge 需要 HTTPS；本机 SSH 隧道可用 127.0.0.1 的 HTTP。')

    async def request(self, method: str, qq: str, endpoint: str, payload: dict | None = None) -> dict:
        import aiohttp

        origin = self._origin()
        if not QQ_RE.fullmatch(qq):
            raise BridgeError('bad_qq', '请从 QQ 使用这项功能。')
        path = f'{BRIDGE_PATH}/users/{qq}{endpoint}'
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode() if payload is not None else b''
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        canonical = '\n'.join((ts, nonce, method, path, hashlib.sha256(body).hexdigest())).encode()
        headers = {
            'Content-Type': 'application/json',
            'X-AWMC-Ts': ts,
            'X-AWMC-Nonce': nonce,
            'X-AWMC-Sig': hmac.new(self._secret.encode(), canonical, hashlib.sha256).hexdigest(),
        }
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12, connect=5))
        try:
            async with self._session.request(method, origin + path, data=body, headers=headers,
                                             allow_redirects=False) as response:
                if response.status == 401:
                    raise BridgeError('authentication', 'AWMC Bridge 鉴权失败，请联系 KanadeBot 管理员。')
                if (response.content_length or 0) > MAX_RESPONSE_BYTES:
                    raise BridgeError('response', 'AWMC 响应过大。')
                blob = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    blob.extend(chunk)
                    if len(blob) > MAX_RESPONSE_BYTES:
                        raise BridgeError('response', 'AWMC 响应过大。')
                result = json.loads(blob)
                if isinstance(result, dict) and 200 <= response.status < 300 and result.get('ok') is True:
                    return result
        except (TimeoutError, aiohttp.ClientError, ValueError):
            raise BridgeError('network', '暂时无法连接 AWMC，请稍后再试。') from None
        if not isinstance(result, dict):
            raise BridgeError('response', 'AWMC 返回异常，请稍后再试。')
        code = str(result.get('error') or '')
        job = result.get('job')
        message = result.get('message') or (job.get('message') if isinstance(job, dict) else None)
        known_message = code in {'agreement_required', 'agreement_changed', 'bad_phrase', 'banned',
                                 'bad_qrcode', 'unknown_job', 'no_image'} or not code
        if known_message and isinstance(message, str) and message:
            raise BridgeError(code or 'failed', safe_message(message)[:1500])
        raise BridgeError(code or 'response', 'AWMC 暂时无法处理此请求，请稍后再试。')

    async def wait_job(self, qq: str, job: dict, *, seconds: float = 75) -> dict:
        job_id = str(job.get('id') or '')
        if not re.fullmatch(r'[0-9a-f]{16}', job_id):
            raise BridgeError('response', 'AWMC 任务状态暂不可用。')
        deadline = time.monotonic() + seconds
        while job.get('status') == 'running' and time.monotonic() < deadline:
            await asyncio.sleep(min(3, max(0, deadline - time.monotonic())))
            result = await self.request('GET', qq, f'/jobs/{job_id}')
            job = result.get('job')
            if not isinstance(job, dict) or job.get('id') != job_id:
                raise BridgeError('response', 'AWMC 任务状态暂不可用。')
        if job.get('status') not in ('running', 'done', 'failed'):
            raise BridgeError('response', 'AWMC 任务状态暂不可用。')
        return job

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


if __name__ == '__main__' and sys.argv[1:] == ['--decode-stdin']:
    try:
        qr = _decode_image(sys.stdin.buffer.read(MAX_IMAGE_BYTES + 1))
        if qr:
            sys.stdout.write(qr)
    except Exception:
        raise SystemExit(1) from None

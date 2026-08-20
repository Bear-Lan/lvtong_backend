"""HTTPS 自签证书生成工具

启动时自动检测 + 生成自签证书（含本机所有网卡 IP 的 SAN）。
Android 移动 app 第一次访问需要信任证书（系统设置 → 安全 → 安装证书）。
"""
import datetime
import logging
import os
import socket
import subprocess
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger('tls_cert')

CERT_DIR = Path(os.environ.get('TLS_CERT_DIR', './certs'))
CERT_FILE = CERT_DIR / 'cert.pem'
KEY_FILE = CERT_DIR / 'key.pem'


def _get_local_ips() -> list[str]:
    """收集本机 IPv4（双网卡场景必须全覆盖，如 192.168.88.252 + 192.168.0.138）。"""
    ips: set[str] = {'127.0.0.1'}

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        try:
            s.connect(('8.8.8.8', 80))
            ips.add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass

    # Windows: ipconfig；Linux: hostname -I
    try:
        if os.name == 'nt':
            out = subprocess.check_output(
                ['ipconfig'], text=True, stderr=subprocess.DEVNULL, timeout=3,
                encoding='utf-8', errors='ignore',
            )
            for line in out.splitlines():
                line = line.strip()
                if 'IPv4' not in line and 'IP Address' not in line:
                    continue
                part = line.split(':')[-1].strip()
                try:
                    ip_address(part)
                    ips.add(part)
                except ValueError:
                    pass
        else:
            out = subprocess.check_output(
                ['hostname', '-I'], text=True, stderr=subprocess.DEVNULL, timeout=2
            )
            for part in out.split():
                try:
                    ip_address(part)
                    ips.add(part)
                except ValueError:
                    pass
    except Exception:
        pass

    # 丢弃明显无用的地址
    cleaned: set[str] = set()
    for raw in ips:
        try:
            addr = ip_address(raw)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_private or addr.is_global:
            if not addr.is_link_local and not addr.is_multicast:
                cleaned.add(str(addr))
    return sorted(cleaned)


def _build_san(ips: list[str]) -> x509.SubjectAlternativeName:
    names: list[x509.GeneralName] = [x509.DNSName('localhost')]
    for raw in ips:
        try:
            names.append(x509.IPAddress(ip_address(raw)))
        except ValueError:
            names.append(x509.DNSName(raw))
    return x509.SubjectAlternativeName(names)


def _cert_san_ips(cert: x509.Certificate) -> set[str]:
    """读取证书 SAN 中的 IP（只认 IPAddress，DNS 伪装的 IP 视为无效）。"""
    found: set[str] = set()
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return found
    for name in san:
        if isinstance(name, x509.IPAddress):
            found.add(str(name.value))
    return found


def _cert_needs_regen(ips: list[str]) -> bool:
    """现有证书缺少当前网卡 IP、或把 IP 错写成 DNSName 时需要重签。"""
    if not CERT_FILE.exists() or not KEY_FILE.exists():
        return True
    try:
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
    except Exception:
        return True

    now = datetime.datetime.now(datetime.timezone.utc)
    if cert.not_valid_after_utc <= now:
        return True

    have = _cert_san_ips(cert)
    need = set(ips)
    missing = need - have
    if missing:
        logger.info(f'[TLS] 证书 SAN 缺少 IP: {", ".join(sorted(missing))}，将重签')
        return True
    return False


def _ensure_cert() -> tuple[str, str]:
    """确保证书存在且 SAN 覆盖本机 IP。返回 (cert_path, key_path)。"""
    CERT_DIR.mkdir(parents=True, exist_ok=True)

    ips = _get_local_ips()
    force = os.environ.get('TLS_FORCE_REGEN', '').strip().lower() in ('1', 'true', 'yes')
    if not force and not _cert_needs_regen(ips):
        logger.info(f'[TLS] 使用现有证书: {CERT_FILE}')
        return str(CERT_FILE), str(KEY_FILE)

    logger.info('[TLS] 生成自签证书（首次启动 / SAN 过期 / TLS_FORCE_REGEN）...')

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, 'gcms_dev_cert'),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'gcms_dev'),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(_build_san(ips), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    logger.info(f'[TLS] 自签证书生成: {CERT_FILE} (SAN: {", ".join(ips)})')
    print(f'[HTTPS] 证书已重签，SAN IP: {", ".join(ips)}')
    return str(CERT_FILE), str(KEY_FILE)


def get_ssl_context():
    """获取 Flask HTTPS 用的 ssl_context"""
    import ssl
    cert_path, key_path = _ensure_cert()
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx

from __future__ import annotations

import io

import pyotp
import qrcode
import qrcode.image.svg


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(email: str, secret: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def verify_code(secret: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code:
        return False
    # valid_window=1 → accept current step plus one step on each side (~±30s),
    # which absorbs minor clock skew between the server and the phone.
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def qr_svg(uri: str) -> str:
    """Render the otpauth:// URI as an SVG string the page can inline."""
    buf = io.BytesIO()
    qrcode.make(
        uri,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=10,
    ).save(buf)
    return buf.getvalue().decode("utf-8")

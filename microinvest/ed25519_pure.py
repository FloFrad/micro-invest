"""Signature Ed25519 en Python pur (RFC 8032), sans aucune dépendance.

Utilisé pour signer les requêtes de l'API Revolut X. Lent comparé à une
bibliothèque C (~quelques ms par signature), mais largement suffisant ici.
"""
import base64
import hashlib
import os

_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = (-121665 * pow(121666, _p - 2, _p)) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _sha512(b):
    return hashlib.sha512(b).digest()


def _inv(x):
    return pow(x, _p - 2, _p)


def _recover_x(y, sign):
    xx = (y * y - 1) * _inv(_d * y * y + 1)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != sign:
        x = _p - x
    return x


_By = (4 * _inv(5)) % _p
_Bx = _recover_x(_By, 0)
_B = (_Bx, _By, 1, (_Bx * _By) % _p)  # coordonnées étendues


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = 2 * P[3] * Q[3] * _d % _p
    D = 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _encode_point(P):
    zinv = _inv(P[2])
    x = P[0] * zinv % _p
    y = P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _secret_expand(seed):
    if len(seed) != 32:
        raise ValueError("La clé privée Ed25519 doit faire 32 octets")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed):
    a, _ = _secret_expand(seed)
    return _encode_point(_mul(a, _B))


def sign(seed, msg, pub=None):
    a, prefix = _secret_expand(seed)
    A = pub or _encode_point(_mul(a, _B))
    r = int.from_bytes(_sha512(prefix + msg), "little") % _L
    R = _encode_point(_mul(r, _B))
    h = int.from_bytes(_sha512(R + A + msg), "little") % _L
    s = (r + h * a) % _L
    return R + int.to_bytes(s, 32, "little")


# --- Formats PEM (compatibles OpenSSL) -------------------------------------

_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _pem(label, der):
    b64 = base64.b64encode(der).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


def generate_keypair():
    """Retourne (pem_privée, pem_publique)."""
    seed = os.urandom(32)
    return _pem("PRIVATE KEY", _PKCS8_PREFIX + seed), _pem("PUBLIC KEY", _SPKI_PREFIX + public_key(seed))


def load_private_pem(text):
    """Extrait la graine de 32 octets d'une clé PEM PKCS#8 Ed25519."""
    body = "".join(l.strip() for l in text.strip().splitlines() if l and not l.startswith("-----"))
    der = base64.b64decode(body)
    if len(der) == 48 and der.startswith(_PKCS8_PREFIX):
        return der[16:]
    marker = b"\x04\x22\x04\x20"
    i = der.find(marker)
    if b"\x2b\x65\x70" not in der or i < 0:
        raise ValueError("Ce fichier n'est pas une clé privée Ed25519 (PKCS#8)")
    return der[i + 4:i + 36]


def public_pem_from_seed(seed):
    return _pem("PUBLIC KEY", _SPKI_PREFIX + public_key(seed))

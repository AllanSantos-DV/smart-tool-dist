#!/usr/bin/env python3
"""Proteção em repouso de segredos gravados em disco via DPAPI (`CryptProtectData`/
`CryptUnprotectData`, escopo do usuário atual) — a mesma mecânica que o launcher
PowerShell usa com `ConvertFrom-SecureString`, aqui em `ctypes` para manter a regra
stdlib-only.

O que isto protege e o que não protege, para ninguém confiar mais do que deve:
o blob só é decifrável pela conta Windows que o gerou, na máquina em que foi gerado
(a chave-mestra DPAPI vive no perfil e é derivada da credencial de logon). Então uma
cópia *deste arquivo* — backup, sincronização de perfil, coleta de diagnóstico, pacote
de suporte, upload de EDR, pendrive — deixa de carregar credencial utilizável. Não é
barreira contra código rodando *como o próprio usuário*: esse código chama
`CryptUnprotectData` igual a este módulo. Também não protege o valor em memória.

A garantia vale por arquivo, não pelo segredo: enquanto o mesmo valor existir em outro
arquivo desprotegido, quem copia a pasta pega a cópia mais fácil.
"""
import base64
import os

PREFIX = "dpapi:v1:"
_PREFIX_BYTES = PREFIX.encode("ascii")

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretProtectionError(Exception):
    """DPAPI indisponível ou recusou o blob (perfil diferente, máquina diferente,
    chave-mestra recriada). Mensagem já pronta para exibir."""


def _load_api():
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob_p = ctypes.POINTER(DataBlob)
        crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        for name in ("CryptProtectData", "CryptUnprotectData"):
            fn = getattr(crypt32, name)
            fn.argtypes = [blob_p, ctypes.c_void_p, blob_p, ctypes.c_void_p,
                           ctypes.c_void_p, wintypes.DWORD, blob_p]
            fn.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        return ctypes, DataBlob, crypt32, kernel32
    except (ImportError, OSError, AttributeError):
        return None


_API = _load_api()


def available():
    return _API is not None


def _crypt(func_name, data):
    ctypes, DataBlob, crypt32, kernel32 = _API
    # `buf_in` precisa continuar referenciado até a chamada retornar: o blob aponta pra
    # dentro dele.
    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    ok = getattr(crypt32, func_name)(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise SecretProtectionError(
            f"{func_name} failed (error {ctypes.get_last_error()}): the protected secret "
            "is only readable by the same Windows account and machine that saved it."
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def is_protected(stored):
    return isinstance(stored, str) and stored.startswith(PREFIX)


def protect(plaintext):
    """Forma de armazenamento do segredo. Idempotente: um valor já protegido volta como
    está, pra regravar a config não cifrar duas vezes."""
    if not plaintext:
        return ""
    if is_protected(plaintext):
        return plaintext
    if not available():
        raise SecretProtectionError("DPAPI unavailable on this platform.")
    blob = _crypt("CryptProtectData", plaintext.encode("utf-8"))
    stored = PREFIX + base64.b64encode(blob).decode("ascii")
    # Invariante: nunca devolver uma forma de armazenamento que não volte a ser o segredo
    # original — quem chama grava isto em lugar do texto puro.
    if unprotect(stored) != plaintext:
        raise SecretProtectionError("DPAPI did not return the same value when decrypting what it just encrypted.")
    return stored


def unprotect(stored):
    """Aceita tanto a forma protegida quanto texto puro: um valor legado em claro é
    formato válido de entrada, e migrá-lo é responsabilidade de quem grava."""
    if not stored:
        return ""
    if not is_protected(stored):
        return stored
    if not available():
        raise SecretProtectionError("Secret is protected by DPAPI and DPAPI is unavailable on this platform.")
    try:
        blob = base64.b64decode(stored[len(PREFIX):], validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretProtectionError(f"Protected secret has invalid base64 ({exc}).")
    return _crypt("CryptUnprotectData", blob).decode("utf-8", "replace")

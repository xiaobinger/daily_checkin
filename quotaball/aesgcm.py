#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库 AES-256-GCM 解密。

用途：
  - Zcode 凭证解密（~/.zcode/v2/credentials.json 的 enc:v1 格式）
  - WorkBuddy 5.7+ 登录态信封（$wbEncrypted，AAD 绑定 keyId/套件/framing）
  - 后续如需复用 Qoder CN 的 vscdb 解密也可走这里

只实现 GCM 解密（本项目不需要加密）。无第三方依赖，Python 3.6+ 可用。
"""
import functools


@functools.lru_cache(maxsize=8)
def _sbox():
    def gmul(a, b):
        r = 0
        for _ in range(8):
            if b & 1:
                r ^= a
            a <<= 1
            if a & 0x100:
                a ^= 0x11B
            b >>= 1
        return r

    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if gmul(a, b) == 1:
                inv[a] = b
                break
    sb = [0x63]
    for a in range(1, 256):
        x = inv[a]
        s = (x ^ ((x << 1) | (x >> 7)) ^ ((x << 2) | (x >> 6))
             ^ ((x << 3) | (x >> 5)) ^ ((x << 4) | (x >> 4)))
        sb.append((s ^ 0x63) & 0xFF)
    return tuple(sb)


_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _gmul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
        b >>= 1
    return r & 0xFF


@functools.lru_cache(maxsize=8)
def _expand_key(key):
    """AES 密钥扩展，返回 (w, 轮数)。结果按 key 缓存，避免重复计算。"""
    nk = len(key) // 4
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    sb = _sbox()
    for i in range(nk, 4 * (nk + 7)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [sb[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [sb[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return tuple(tuple(x) for x in w), nk + 6


def _encrypt_block(block, w, nr):
    s = list(block)
    sb = _sbox()

    def add_round_key(rnd):
        for c in range(4):
            for i in range(4):
                s[4 * c + i] ^= w[rnd * 4 + c][i]

    add_round_key(0)
    for rnd in range(1, nr + 1):
        s = [sb[b] for b in s]
        t = list(s)
        for c in range(4):
            for i in range(4):
                t[4 * c + i] = s[4 * ((c + i) % 4) + i]
        s = t
        if rnd != nr:
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                s[4 * c + 0] = _gmul(col[0], 2) ^ _gmul(col[1], 3) ^ col[2] ^ col[3]
                s[4 * c + 1] = col[0] ^ _gmul(col[1], 2) ^ _gmul(col[2], 3) ^ col[3]
                s[4 * c + 2] = col[0] ^ col[1] ^ _gmul(col[2], 2) ^ _gmul(col[3], 3)
                s[4 * c + 3] = _gmul(col[0], 3) ^ col[1] ^ col[2] ^ _gmul(col[3], 2)
        add_round_key(rnd)
    return bytes(s)


def _inc32(cb):
    cb = list(cb)
    for j in range(len(cb) - 1, -1, -1):
        cb[j] = (cb[j] + 1) & 0xFF
        if cb[j]:
            break
    return cb


def _gctr(w, nr, icb, data):
    out = bytearray()
    cb = list(icb)
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        ks = _encrypt_block(cb, w, nr)
        out += bytes(x ^ y for x, y in zip(chunk, ks))
        cb = _inc32(cb)
    return bytes(out)


def _gfmul(x, y):
    """GF(2^128) 乘法。GCM 采用位反转表示，故乘以生成元对应「右移 + 条件约简」。

    注意：y 必须从最高位向最低位遍历，位序写反会导致认证标签校验失败。
    """
    r = 0
    v = x
    for i in range(128):
        if y & (1 << (127 - i)):
            r ^= v
        v = (v >> 1) ^ (0xE1000000000000000000000000000000 if v & 1 else 0)
    return r


def _ghash(h, buf):
    s = 0
    for i in range(0, len(buf), 16):
        blk = buf[i:i + 16]
        if len(blk) < 16:
            blk = blk + b"\x00" * (16 - len(blk))
        s = _gfmul(s ^ int.from_bytes(blk, "big"), h)
    return s


def gcm_decrypt(key, nonce, ciphertext, tag, aad=None):
    """AES-GCM 解密，返回 (明文 bytes, 校验是否通过)。

    key: 16/24/32 字节；nonce: 12 字节；tag: 16 字节
    aad: 可选附加认证数据（bytes 或 None）。GHASH 输入为
         aad || pad || len(aad)*8 || ciphertext || pad || len(ciphertext)*8。
    """
    if len(key) not in (16, 24, 32):
        raise ValueError("key 长度必须是 16/24/32 字节")
    if len(nonce) != 12:
        raise ValueError("nonce 必须是 12 字节")
    if len(tag) != 16:
        raise ValueError("tag 必须是 16 字节")

    w, nr = _expand_key(key)
    h = int.from_bytes(_encrypt_block([0] * 16, w, nr), "big")

    j0 = list(nonce) + [0, 0, 0, 1]
    plaintext = _gctr(w, nr, _inc32(j0), ciphertext)

    aad = b"" if aad is None else bytes(aad)
    # GHASH 输入顺序：aad || pad || ciphertext || pad || len(aad)*8 || len(ct)*8
    buf = (aad
           + b"\x00" * ((-len(aad)) % 16)      # 空 AAD 时补 0 字节，不是 16
           + ciphertext
           + b"\x00" * ((-len(ciphertext)) % 16)
           + (len(aad) * 8).to_bytes(8, "big")
           + (len(ciphertext) * 8).to_bytes(8, "big"))
    s = _ghash(h, buf)
    ek = _gctr(w, nr, j0, b"\x00" * 16)
    calc = bytes(a ^ b for a, b in zip(s.to_bytes(16, "big"), ek))
    return plaintext, calc == tag

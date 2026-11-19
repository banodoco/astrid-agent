"""Deterministic 1024x1024 PNG, padded to a requested size with a valid ancillary chunk."""
import struct, zlib

def chunk(kind, data):
    return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)

PIXELS=(b'\x00'+b'\x31\x72\xa0'*1024)*1024
COMPRESSED=zlib.compress(PIXELS,6)

def make_image(tag, size=1048576):
    header=chunk(b'IHDR',struct.pack('>IIBBBBB',1024,1024,8,2,0,0,0))
    # One megapixel of RGB, solid color; unique tag is metadata, not image texture.
    data=chunk(b'IDAT',COMPRESSED)
    fixed=b'\x89PNG\r\n\x1a\n'+header+data
    tag=tag.encode('ascii'); pad=size-len(fixed)-24
    if len(tag)>pad: raise ValueError('target size too small')
    return fixed+chunk(b'npAD',tag+bytes(pad-len(tag)))+chunk(b'IEND',b'')

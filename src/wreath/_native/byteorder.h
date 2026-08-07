/* Fixed-width integer load/store with the byte order named in the call.
 *
 * These lived in `wreathcore.h` and so were reachable only from the `_core`
 * extension. Every other extension that needed them assembled its own: the
 * PostgreSQL migration modules alone carried five copies of `read_u32_le` and
 * three of `write_u32_le`, and `codec.c` a `read_be_uint32` that is
 * `wreath_load_u32_be` under another name. Splitting the block out is what lets
 * `_postgres` reach the real thing; `wreathcore.h` includes this, so nothing
 * that already used them had to change.
 *
 * Deliberately free of `Python.h`, like `simd.h`, so any translation unit can
 * take it.
 *
 * Both orders are live and the name is what keeps them apart: protobuf and the
 * migration tapes are little-endian on the wire, while WebSocket frame lengths,
 * HTTP/2 frame headers, msgpack and the PostgreSQL protocol are network byte
 * order. A single order-agnostic helper would let a migration flip a wire format
 * with nothing to catch it.
 *
 * The shifts *establish* the order rather than assume the host's, so there is no
 * #if on host endianness, no memcpy of the host representation, and no htonl.
 * The compiler recognises each pattern and folds it back to one unaligned load
 * or store, plus a bswap where the orders differ.
 *
 * Written out rather than looped on purpose: gcc -O2 emits a loop over
 * `v >> (8 * i)` literally, a byte per iteration, and does not collapse it --
 * the looped form in protobuf.c measured 10-15x slower per call than this one.
 */
#ifndef WREATH_BYTEORDER_H
#define WREATH_BYTEORDER_H

#include <stdint.h>

static inline uint16_t
wreath_load_u16_le(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static inline uint16_t
wreath_load_u16_be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | (uint16_t)p[1]);
}

static inline uint32_t
wreath_load_u32_le(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static inline uint32_t
wreath_load_u32_be(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static inline uint64_t
wreath_load_u64_le(const uint8_t *p)
{
    return (uint64_t)p[0] | ((uint64_t)p[1] << 8) | ((uint64_t)p[2] << 16) |
           ((uint64_t)p[3] << 24) | ((uint64_t)p[4] << 32) |
           ((uint64_t)p[5] << 40) | ((uint64_t)p[6] << 48) |
           ((uint64_t)p[7] << 56);
}

static inline uint64_t
wreath_load_u64_be(const uint8_t *p)
{
    return ((uint64_t)p[0] << 56) | ((uint64_t)p[1] << 48) |
           ((uint64_t)p[2] << 40) | ((uint64_t)p[3] << 32) |
           ((uint64_t)p[4] << 24) | ((uint64_t)p[5] << 16) |
           ((uint64_t)p[6] << 8) | (uint64_t)p[7];
}

static inline void
wreath_store_u16_le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static inline void
wreath_store_u16_be(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v >> 8);
    p[1] = (uint8_t)v;
}

static inline void
wreath_store_u32_le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
}

static inline void
wreath_store_u32_be(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static inline void
wreath_store_u64_le(uint8_t *p, uint64_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
    p[2] = (uint8_t)(v >> 16);
    p[3] = (uint8_t)(v >> 24);
    p[4] = (uint8_t)(v >> 32);
    p[5] = (uint8_t)(v >> 40);
    p[6] = (uint8_t)(v >> 48);
    p[7] = (uint8_t)(v >> 56);
}

static inline void
wreath_store_u64_be(uint8_t *p, uint64_t v)
{
    p[0] = (uint8_t)(v >> 56);
    p[1] = (uint8_t)(v >> 48);
    p[2] = (uint8_t)(v >> 40);
    p[3] = (uint8_t)(v >> 32);
    p[4] = (uint8_t)(v >> 24);
    p[5] = (uint8_t)(v >> 16);
    p[6] = (uint8_t)(v >> 8);
    p[7] = (uint8_t)v;
}

#endif /* WREATH_BYTEORDER_H */

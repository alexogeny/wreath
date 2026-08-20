/* Internal decoder surface, shared between inflate.c and the per-ISA arms.
 *
 * SPDX-License-Identifier: MPL-2.0
 */
#ifndef GZ_INFL_H
#define GZ_INFL_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "gz.h"

/* ---- decode table entry -------------------------------------------------
 *
 * The literal flag is the sign bit because "is this a plain literal" is the
 * hottest test in the decoder and a sign test needs no immediate.
 *
 *   bit  31     literal
 *   bits 16..30 payload: length base, distance base, subtable offset
 *   bits 12..13 kind, for non-literals
 *   bits  8..15 the literal byte, for literals
 *   bits  8..11 extra bits following the code (subtable index width, for SUB)
 *   bits  0.. 7 bits of stream the code itself occupies
 *
 * The literal byte sits at 8..15 so that emitting it is a single byte store
 * out of %ah, rather than a copy, a 16-bit shift and a store. That overlaps the
 * kind and extra-bit fields, which is safe only because *every* kind test in
 * the decoder is already guarded by the sign bit -- a literal never reaches
 * one. Moving a kind test out from behind that guard would silently
 * misinterpret literals above 0x0F.
 */
#define GZ_E_LIT 0x80000000u
#define GZ_K_MATCH 0x0000u
#define GZ_K_EOB 0x1000u
#define GZ_K_SUB 0x2000u
#define GZ_K_BAD 0x3000u
#define GZ_K_MASK 0x3000u
#define GZ_IS_LIT(e) ((int32_t)(e) < 0)
#define GZ_E_XB(e) (((e) >> 8) & 0xFu)
#define GZ_E_VAL(e) ((e) >> 16)
#define GZ_E_BITS(e) ((e) & 0xFFu)
/* Where a literal entry keeps its byte. 8 puts it in %ah, so emitting it is one
 * store; 16 is the conventional place, and needs a copy and a shift first. The
 * knob exists because that difference is one of the largest single effects in
 * the ledger, and a claim that size deserves an ablation switch. */
#ifndef GZ_LIT_SHIFT
#define GZ_LIT_SHIFT 8
#endif
#define GZ_E_LITBYTE(e) ((uint8_t)((e) >> GZ_LIT_SHIFT))

/* ---- fused multi-symbol root entry (64 bits) ----------------------------
 *
 * Indexed by the same root bits as the literal/length table, so the fallback
 * costs one shift rather than a second table load.
 *
 *   bits  0.. 7  stream bits the whole fused run consumes
 *   bits  8..11  number of literals in the run, 1..4; 0 means "not fused"
 *   bits 32..63  when fused: the literal bytes, ready for one 32-bit store
 *                when not:   exactly the single-symbol entry for this index
 */
#define GZ_F_N 0x0F00u
#define GZ_F_MK(bits, n, pack) \
  ((uint64_t)(bits) | ((uint64_t)(n) << 8) | ((uint64_t)(pack) << 32))

/* Instruction costs of the three things the fused-table selector trades off,
 * counted off the disassembly of this build rather than guessed:
 *
 *   GZ_FUSE_LIT     one literal through the single-symbol slot: a table load,
 *                   a sign test, a byte store out of %ah, a shrx and a sub.
 *   GZ_FUSE_WINDOW  one fused window: a table load, a mask test, a 32-bit
 *                   store, a pointer add, a shrx and a sub, plus the loop.
 *   GZ_FUSE_BUILD_COST  per root entry of wreath_gzip_decoder_build_fused.
 *
 * The ratio of the first two is what decides whether fusing can ever pay, and
 * moving GZ_FUSE_LIT from 7 to 5 -- which is what relocating the literal byte
 * to bits 8..15 did -- is exactly why it no longer does. See RESULTS.md. */
#ifndef GZ_FUSE_LIT
#define GZ_FUSE_LIT 5.0
#endif
#ifndef GZ_FUSE_WINDOW
#define GZ_FUSE_WINDOW 10.0
#endif
#ifndef GZ_FUSE_BUILD_COST
#define GZ_FUSE_BUILD_COST 12.0
#endif
/* The measured selector chose no corpus block, while forcing the fused loop
 * lost 5.7%. Keep forced fusion for differential/ablation coverage but do not
 * run a floating-point profitability model on every dynamic block by default. */
#ifndef GZ_AUTO_FUSED
#define GZ_AUTO_FUSED 0
#endif

/* How many literals the fast lane may take before going back for a refill, and
 * the buffered-bit threshold below which the match path refills again before
 * reading a distance code. Both are bit budgets with hard upper bounds -- see
 * the derivations in inflate_body.h -- and both were swept; see RESULTS.md. */
#ifndef GZ_LIT_CHAIN
#define GZ_LIT_CHAIN 4
#endif
#ifndef GZ_DIST_REFILL
#define GZ_DIST_REFILL 28
#endif

/* Bytes ahead of the write pointer to prefetch for write, once per fast-lane
 * iteration; 0 disables. Prefetching is the one lever aimed only at the cache
 * axis and it usually loses -- measured, not assumed. */
#ifndef GZ_PREFETCH_OUT
#define GZ_PREFETCH_OUT 0
#endif

/* Table entries below which the doubling fill copies inline rather than calling
 * memcpy. The call is worth its overhead only once the copy is long enough;
 * swept in RESULTS.md. */
#ifndef GZ_BUILD_INLINE_COPY
#define GZ_BUILD_INLINE_COPY 16u
#endif
#ifndef GZ_FIXED_DIRECT
#define GZ_FIXED_DIRECT 1
#endif
#ifndef GZ_PRECODE_DIRECT
#define GZ_PRECODE_DIRECT 0
#endif
#ifndef GZ_TABLE_REUSE
#define GZ_TABLE_REUSE 1
#endif
/* A 16-bit front plane for the literal/length root.  Plain literals keep the
 * consumed-bit count in the low byte and the literal itself in the high byte,
 * exactly where the hot loop wants it.  Bit 7 of the low byte is impossible
 * for a real DEFLATE code length (1..15), so it marks entries that must be
 * fetched from the full 32-bit root table.  Subtables remain 32-bit. */
#ifndef GZ_COMPACT_LITROOT
#define GZ_COMPACT_LITROOT 0
#endif
#define GZ_CR_ESCAPE 0x80u

/* Cache the exact compressed dynamic-header bitstring together with the table
 * it built.  A hit can skip the precode and code-length RLE reader as well as
 * the already-existing table-reuse check. */
#ifndef GZ_HEADER_MEMO
#define GZ_HEADER_MEMO 0
#endif
#define GZ_HEADER_MAX_BITS 4608u
#define GZ_HEADER_WORDS (GZ_HEADER_MAX_BITS / 32u)
/* Let the high bits of the fast lane's bit count carry harmless table-entry
 * payload. Its low byte remains the exact count. This makes consuming a table
 * entry one subtract instead of first isolating its low byte; refill and handoff
 * explicitly read that byte. The representation is local to each ISA arm. */
#ifndef GZ_DIRTY_BITCOUNT
#define GZ_DIRTY_BITCOUNT 1
#endif

/* Root widths. Both were re-measured in this build; see RESULTS.md. */
#ifndef GZ_LIT_ROOT
#define GZ_LIT_ROOT 10
#endif
#ifndef GZ_DIST_ROOT
#define GZ_DIST_ROOT 8
#endif
#define GZ_PRE_ROOT 7

/* Arena sizes. A code longer than the root contributes a subtable of at most
 * 2^(15-root) entries. A complete code of depth d under one root prefix needs
 * at least d+1 symbols, so with 288 symbols the litlen subtables cannot exceed
 * 288/6 * 32 = 1536 entries and the distance subtables 30/8 * 128 = 480. These
 * arenas are three times that, and build_table hard-errors rather than
 * truncating if a stream ever runs past them. */
#define GZ_LIT_TAB ((1u << GZ_LIT_ROOT) + 4608u)
#define GZ_DIST_TAB ((1u << GZ_DIST_ROOT) + 3840u)
#define GZ_PRE_TAB 128u

struct wreath_gzip_decoder_dec {
  uint32_t lit[GZ_LIT_TAB];
#if GZ_COMPACT_LITROOT
  uint16_t litroot[1u << GZ_LIT_ROOT];
#endif
  uint32_t dist[GZ_DIST_TAB];
  uint32_t pre[GZ_PRE_TAB];
  uint64_t fused[1u << GZ_LIT_ROOT];
  uint32_t lbits, dbits, pbits;
  int arm;          /* runtime-selected ISA, owned by this decoder */
  int crc_arm;      /* runtime-selected CRC ISA, owned by this decoder */
  int format;       /* out-of-band GZ_FMT_* hint; never serialized */
  int fixed_loaded; /* lit/dist currently hold the fixed code */
  int fused_live;   /* the fused table matches the current lit table */
  uint32_t hlit;    /* litlen alphabet size of the cached dynamic table */
  uint32_t hdist;   /* distance alphabet size of the cached dynamic table */
  uint8_t lens_active;
  uint8_t have_dynamic;
  uint16_t count[16];  /* length histogram, literal/length alphabet */
  uint16_t dcount[16]; /* length histogram, distance alphabet */
  uint8_t lens[2][320]; /* inactive bank receives the next hostile header */
  uint16_t sorted[640]; /* 320 canonical slots, then a scratch dump for zero lengths */
#if GZ_HEADER_MEMO
  uint32_t header_bits[GZ_HEADER_WORDS];
  uint16_t header_nbits;
  uint8_t header_valid;
  uint8_t header_hot; /* exact header has repeated at least once */
#endif
};

/* Reader/writer state handed between the fast arm loop and the careful loop.
 * `in` is the next byte not represented in the low `bc` bits of `bb`. */
struct wreath_gzip_decoder_st {
  const uint8_t *in, *in_end;
  uint8_t *out, *out_end, *win;
  uint64_t bb;
  uint32_t bc;
  struct wreath_gzip_decoder_dec *d;
};

/* Fast-lane budget.
 *
 * Input: an iteration performs GZ_REFILLS refills -- one at the top, one before
 * the distance code, and one between each group of four literals in a chain
 * longer than four. Each refill advances at most 7 bytes and reads 8 bytes from
 * where it starts. An iteration begins with ip <= ifast, so refill j starts no
 * later than ifast + 7(j-1) and its last byte read is at ifast + 7j; the last
 * one therefore touches ifast + 7*GZ_REFILLS, and staying inside the buffer
 * needs GZ_IN_SLACK >= 7*GZ_REFILLS + 1. GZ_IN_STEP is the same 7*GZ_REFILLS,
 * because that is also the most an iteration can advance ip.
 *
 * Output: the plain loop writes at most GZ_LIT_CHAIN literals and a 258-byte
 * match whose copy may overshoot the match end by 31 bytes. The fused loop
 * writes at most three four-byte literal stores (advancing at most 12, touching
 * at most 15) and then the same match, so 15 + 289 = 304 touched. One pair of
 * constants covers both, and the proof that each guard is load-bearing -- and
 * can be made to bind on its own -- is in RESULTS.md.
 */
#define GZ_REFILLS ((GZ_LIT_CHAIN + 3) / 4 + 1)
/* 0 disables the fast lane entirely, for the ablation that prices it. */
#ifndef GZ_FAST_LANE
#define GZ_FAST_LANE 1
#endif

#ifndef GZ_IN_SLACK
#define GZ_IN_SLACK (7u * GZ_REFILLS + 1u)
#endif
#ifndef GZ_OUT_SLACK
#define GZ_OUT_SLACK 336u
#endif
#define GZ_IN_STEP (7u * GZ_REFILLS)
#define GZ_OUT_STEP (GZ_LIT_CHAIN + 289u > 304u ? GZ_LIT_CHAIN + 289u : 304u)

/* Arm entry points. Return 1 at end-of-block, 0 when the guaranteed room ran
 * out and the careful loop must take over, negative GZ_ERR_* on bad data. */
int wreath_gzip_decoder_inflate_fast_scalar(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fast_bmi2(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fast_bmi2_avx2(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fast_bmi2_avx2_long(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fused_scalar(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fused_bmi2(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fused_bmi2_avx2(struct wreath_gzip_decoder_st *s);
int wreath_gzip_decoder_inflate_fused_bmi2_avx2_long(struct wreath_gzip_decoder_st *s);

/* Shared, in inflate.c. */
int wreath_gzip_decoder_build_fused(struct wreath_gzip_decoder_dec *d);

static inline uint64_t wreath_gzip_decoder_ld64(const uint8_t *p) {
  uint64_t v;
  memcpy(&v, p, 8);
  return v;
}
static inline uint32_t wreath_gzip_decoder_ld32(const uint8_t *p) {
  uint32_t v;
  memcpy(&v, p, 4);
  return v;
}
static inline void wreath_gzip_decoder_st64(uint8_t *p, uint64_t v) { memcpy(p, &v, 8); }
static inline void wreath_gzip_decoder_st32(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }

extern const uint16_t wreath_gzip_decoder_len_base[29];
extern const uint8_t wreath_gzip_decoder_len_extra[29];
extern const uint16_t wreath_gzip_decoder_dist_base[30];
extern const uint8_t wreath_gzip_decoder_dist_extra[30];
extern const uint8_t wreath_gzip_decoder_clen_order[19];

#endif

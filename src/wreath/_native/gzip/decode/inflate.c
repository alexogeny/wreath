/* gzip / DEFLATE decoder: framing, decode-table construction, the careful
 * lane, and the once-per-block arm selection. The hot symbol loop lives in
 * inflate_core.h and is compiled once per ISA arm.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Written from RFC 1951 and RFC 1952. Everything a hostile stream can reach is
 * bounds-checked; the fast lane earns its lack of per-symbol checks by running
 * only where the remaining input and output are provably large enough.
 */
#include "infl.h"

#include <stdlib.h>

#include "cpu.h"
#include "crc32.h"
#include "decode_tables.h"

const uint16_t wreath_gzip_decoder_len_base[29] = {3,  4,  5,  6,  7,  8,  9,  10,  11,  13,
                                  15, 17, 19, 23, 27, 31, 35, 43,  51,  59,
                                  67, 83, 99, 115, 131, 163, 195, 227, 258};
const uint8_t wreath_gzip_decoder_len_extra[29] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2,
                                  2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
const uint16_t wreath_gzip_decoder_dist_base[30] = {
    1,    2,    3,    4,    5,    7,     9,     13,    17,   25,
    33,   49,   65,   97,   129,  193,   257,   385,   513,  769,
    1025, 1537, 2049, 3073, 4097, 6145,  8193,  12289, 16385, 24577};
const uint8_t wreath_gzip_decoder_dist_extra[30] = {0, 0, 0,  0,  1,  1,  2,  2,  3,  3,
                                   4, 4, 5,  5,  6,  6,  7,  7,  8,  8,
                                   9, 9, 10, 10, 11, 11, 12, 12, 13, 13};
const uint8_t wreath_gzip_decoder_clen_order[19] = {16, 17, 18, 0, 8,  7, 9,  6, 10, 5,
                                   11, 4,  12, 3, 13, 2, 14, 1, 15};

static const char *const wreath_gzip_decoder_format_names[GZ_FMT_COUNT] = {
    "unknown", "json", "chaotic-json", "html", "graphql", "log", "plaintext"};

const char *wreath_gzip_decoder_format_name(int format) {
  return format >= 0 && format < GZ_FMT_COUNT ? wreath_gzip_decoder_format_names[format] : "?";
}

int wreath_gzip_decoder_format_by_name(const char *name) {
  for (int i = 0; i < GZ_FMT_COUNT; i++)
    if (!strcmp(name, wreath_gzip_decoder_format_names[i])) return i;
  if (!strcmp(name, "chaotic")) return GZ_FMT_CHAOTIC_JSON;
  return -1;
}

static int pick_arm(unsigned features) {
  if ((features & GZ_CPU_BMI2) && (features & GZ_CPU_AVX2)) return GZ_DEC_BMI2_AVX2;
  if (features & GZ_CPU_BMI2) return GZ_DEC_BMI2;
  return GZ_DEC_SCALAR;
}

wreath_gzip_decoder_dec *wreath_gzip_decoder_dec_new(void) {
  wreath_gzip_decoder_dec *d = (wreath_gzip_decoder_dec *)malloc(sizeof *d);
  if (d) {
    unsigned features = wreath_gzip_decoder_cpu_features();
    d->arm = pick_arm(features);
    d->crc_arm = wreath_gzip_decoder_crc32_pick_arm_from_features(features);
    d->fixed_loaded = 0;
    d->format = GZ_FMT_UNKNOWN;
    d->fused_live = 0;
    d->lens_active = 0;
    d->have_dynamic = 0;
#if GZ_HEADER_MEMO
    d->header_valid = 0;
    d->header_nbits = 0;
    d->header_hot = 0;
#endif
  }
  return d;
}

#if GZ_COMPACT_LITROOT
static void build_compact_litroot(wreath_gzip_decoder_dec *d) {
  uint32_t n = 1u << d->lbits;
  for (uint32_t i = 0; i < n; i++) {
    uint32_t e = d->lit[i];
    d->litroot[i] = GZ_IS_LIT(e)
                        ? (uint16_t)(e & 0xff00u) | (uint16_t)GZ_E_BITS(e)
                        : (uint16_t)GZ_CR_ESCAPE;
  }
}
#endif

void wreath_gzip_decoder_dec_free(wreath_gzip_decoder_dec *d) { free(d); }
void wreath_gzip_decoder_dec_set_format(wreath_gzip_decoder_dec *d, int format) {
  d->format = format >= 0 && format < GZ_FMT_COUNT ? format : GZ_FMT_UNKNOWN;
}

/* ---- decode table construction ------------------------------------------
 *
 * This is the piece arm 3 named as its whole remaining deficit on small
 * bodies, and it was completely unoptimised there. Two things changed.
 *
 * The fill is by *doubling*. Writing a code of length l into a root table of
 * width r means writing 2^(r-l) strided copies of it. But if the table is
 * grown a bit at a time -- fill all length-l codes into a table of width l,
 * then duplicate the whole thing to width l+1 -- every code is written exactly
 * once and the replication becomes a memcpy that doubles in size. The total
 * copied is 2^r - 1 entries as straight-line contiguous stores, against 2^r
 * strided single-entry stores; and seeding table[0] with an "invalid" entry
 * before the first doubling propagates it into every unassigned slot for free,
 * so the incomplete-code prefill costs nothing.
 *
 * The codeword is carried in *already bit-reversed* form and advanced with a
 * reversed increment, so no per-symbol bit reversal is needed either. Going
 * from length l to l+1 appends a zero bit at the bottom of the forward code,
 * which is a zero bit at the top of the reversed one: the value is unchanged,
 * which is why the outer loop needs no adjustment at all.
 */

/* The length-free part of every entry, per alphabet, computed once. The fill
 * loop then costs one load and one OR per symbol instead of the four-way
 * branch that deriving it needs. */
static inline uint32_t log2_of(uint32_t pow2) {
  return 31u - (uint32_t)__builtin_clz(pow2);
}

/* Reversing the canonical codeword, which is where the fill loop spent a third
 * of its instructions.
 *
 * The obvious way to walk the reversed codewords is a reversed increment: find
 * the highest zero bit inside the width and clear everything below it. That is
 * branchless but it costs eleven instructions -- a bit scan, a shift to turn
 * the scan result back into a mask, and the two-sided merge -- and gcc rebuilds
 * the 0x80000000 it shifts on every iteration.
 *
 * Reversing the *forward* code through a 256-byte table is cheaper, because
 * canonical order means the forward code is just a counter. A code no longer
 * than eight bits reverses in a load and a shift; a longer one needs two loads.
 * 256 bytes is four cache lines, and nothing in the decode loop touches it. */
static inline uint32_t rev_code(uint32_t c, uint32_t sh) {
  return (uint32_t)((rev8[c & 0xFFu] << 8) | rev8[c >> 8]) >> sh;
}

/* Builds a canonical decode table from lens[0..n). `count` is the length
 * histogram, which the dynamic-header reader already had to compute. Returns
 * the root bit width, or 0 if the code is malformed or does not fit. */
static uint32_t build_table(wreath_gzip_decoder_dec *d, uint32_t *table, uint32_t cap,
                            const uint8_t *lens, uint32_t n, uint16_t *count,
                            const uint32_t *ents, int strict, uint32_t max_root) {
  count[0] = 0;
  uint32_t maxlen = 15;
  while (maxlen && count[maxlen] == 0) maxlen--;

  /* Kraft: `left` is the number of codewords still unassigned at each length.
   * Negative is over-subscribed and always malformed; positive is incomplete,
   * which RFC 1951 only really admits for a distance alphabet with a single
   * symbol -- a permissive decoder here would accept streams zlib rejects. */
  int left = 1;
  uint32_t total = 0;
  for (uint32_t l = 1; l <= 15; l++) {
    left <<= 1;
    left -= (int)count[l];
    if (left < 0) return 0;
    total += count[l];
  }
  if (maxlen == 0) {
    /* No codes at all: legal only for the distance alphabet of a block that
     * contains no matches. Every slot rejects. */
    if (strict) return 0;
    table[0] = GZ_K_BAD | 1;
    table[1] = GZ_K_BAD | 1;
    return 1;
  }
  if (left > 0 && (strict || total != 1)) return 0;

  uint32_t root = maxlen < max_root ? maxlen : max_root;
  const uint32_t rmask = (1u << root) - 1;

  /* Canonical order: symbols sorted by (length, symbol). Zero-length symbols
   * are sent to a scratch region rather than tested for, which removes an
   * unpredictable branch from a pass over 286 symbols. */
  uint16_t *sorted = d->sorted;
  {
    uint16_t next[17];
    uint32_t run = 0;
    for (uint32_t l = 1; l <= 15; l++) {
      next[l] = (uint16_t)run;
      run += count[l];
    }
    next[0] = 320;
    /* Unused symbols cost as much to place as used ones, and most of a
     * literal/length alphabet is unused -- a body drawn from 70-odd distinct
     * bytes leaves well over half of the 286 symbols at length zero, in long
     * runs. Testing eight lengths as one word skips those runs outright; the
     * dense case pays two extra instructions per eight symbols for it. */
    uint32_t s = 0;
    for (; s + 8 <= n; s += 8) {
      if (!wreath_gzip_decoder_ld64(lens + s)) continue;
      for (uint32_t j = s; j < s + 8; j++) sorted[next[lens[j]]++] = (uint16_t)j;
    }
    for (; s < n; s++) sorted[next[lens[s]]++] = (uint16_t)s;
  }

  /* The forward canonical code of the next symbol to place. Both fills advance
   * it the same way -- first_code[l+1] = (first_code[l] + count[l]) << 1 -- and
   * the subtable loop below picks it up from whichever ran. */
  uint32_t k = 0, code = 0;

  /* Levels below the shortest code contain nothing but rejects, so they are
   * filled directly instead of being reached by (lmin - 1) tiny memcpys. */
  uint32_t lmin = 1;
  while (count[lmin] == 0) lmin++;
  uint32_t root_half = 1u << (lmin - 1);
  for (uint32_t i = 0; i < root_half; i++) table[i] = GZ_K_BAD | 1;

  for (uint32_t l = lmin; l <= root; l++) {
    if (root_half <= GZ_BUILD_INLINE_COPY)
      for (uint32_t i = 0; i < root_half; i++) table[root_half + i] = table[i];
    else
      memcpy(table + root_half, table, (size_t)root_half * sizeof *table);
    root_half <<= 1;
    uint32_t end = code + count[l];
    /* A code of eight bits or fewer reverses in one load and one shift; the
     * general form needs a second load that is pure overhead here, and the
     * root levels of the distance and precode tables are entirely inside the
     * cheap case. The test is per level, not per symbol. */
    if (l <= 8) {
      uint32_t sh = 8u - l;
      for (; code < end; code++) table[rev8[code] >> sh] = ents[sorted[k++]] | l;
    } else {
      uint32_t sh = 16u - l;
      for (; code < end; code++) table[rev_code(code, sh)] = ents[sorted[k++]] | l;
    }
    code <<= 1;
  }

  if (maxlen <= root) return root;

  /* Codes longer than the root. Canonical order puts all the codes under one
   * root prefix together and in non-decreasing length, so a subtable can be
   * grown by the same doubling as the root table and its final width is only
   * needed when the run ends -- which removes the separate pass that computed
   * each prefix's depth up front, and the per-code bit reversal with it: the
   * reversed codeword carries straight over from the root loop. */
  uint32_t next_sub = 1u << root, base = 0, half = 0, low = ~0u;
  for (uint32_t l = root + 1; l <= maxlen; l++) {
    uint32_t rem = l - root, sh = 16u - l, end = code + count[l];
    for (; code < end; code++) {
      uint32_t rcode = rev_code(code, sh);
      uint32_t p = rcode & rmask;
      if (p != low) {
        if (low != ~0u) {
          next_sub += half;
          table[low] = (base << 16) | (log2_of(half) << 8) | GZ_K_SUB | root;
        }
        low = p;
        base = next_sub;
        half = 1;
        if (base >= cap) return 0;
        table[base] = GZ_K_BAD | 1;
      }
      /* Grow the subtable to hold a code this long. A subtable is at most
       * 2^(15-root) entries, so every one of these copies is short: calling
       * memcpy for four or eight bytes costs more in call overhead than the
       * copy itself, and there are several per subtable. The width the group
       * will end up needing is not known until the group ends -- that is what
       * the removed prefix-depth pre-pass used to compute -- but the final
       * width *of this code* is, so the arena bound is checked once per growth
       * rather than once per doubling. */
      uint32_t want = 1u << rem;
      if (half < want) {
        if (base + want > cap) return 0;
        do {
          uint32_t *sub = table + base;
          for (uint32_t i = 0; i < half; i++) sub[half + i] = sub[i];
          half <<= 1;
        } while (half < want);
      }
      table[base + (rcode >> root)] = ents[sorted[k++]] | rem;
    }
    code <<= 1;
  }
  next_sub += half;
  if (next_sub > cap) return 0;
  table[low] = (base << 16) | (log2_of(half) << 8) | GZ_K_SUB | root;
  return root;
}

/* ---- fused multi-symbol root table --------------------------------------
 *
 * One 64-bit entry per root index, carrying up to four literals and the total
 * bit width of the run. Where the run is empty the entry carries the ordinary
 * single-symbol entry in its high half, so the fallback costs a shift rather
 * than a second table load and the hot loop needs no second branch.
 *
 * Building it is O(2^root) and only pays back on blocks long enough to amortise
 * it -- which is the whole reason it is selected rather than always on. See
 * pick_fused() and RESULTS.md.
 */
int wreath_gzip_decoder_build_fused(wreath_gzip_decoder_dec *d) {
  const uint32_t root = d->lbits, n = 1u << root;
  const uint32_t *lt = d->lit;
  uint64_t *fu = d->fused;
  for (uint32_t i = 0; i < n; i++) {
    uint32_t e = lt[i];
    if (!GZ_IS_LIT(e)) {
      fu[i] = GZ_F_MK(0, 0, e);
      continue;
    }
    uint32_t bits = GZ_E_BITS(e), cnt = 1, pack = GZ_E_LITBYTE(e);
    uint32_t idx = i >> bits;
    while (cnt < 4) {
      uint32_t f = lt[idx];
      uint32_t l = GZ_E_BITS(f);
      /* Only the low (root - bits) bits of idx are real; a code that would
       * reach past them is decided by padding, not by the stream. */
      if (!GZ_IS_LIT(f) || bits + l > root) break;
      pack |= (uint32_t)GZ_E_LITBYTE(f) << (8 * cnt);
      bits += l;
      idx >>= l;
      cnt++;
    }
    fu[i] = GZ_F_MK(bits, cnt, pack);
  }
  d->fused_live = 1;
  return 0;
}

/* ---- careful lane -------------------------------------------------------
 *
 * Every read and every write is checked. This runs for the last few input
 * bytes of a block, for whole streams too short for the fast lane, and -- the
 * part that matters for hostile input -- for the tail of a truncated stream,
 * where it must stop rather than decode zero padding into literals until the
 * caller's buffer fills.
 */

static int need_bits(struct wreath_gzip_decoder_st *s, uint32_t n, uint32_t *v) {
  while (s->bc < n) {
    if (s->in == s->in_end) return GZ_ERR_TRUNCATED;
    s->bb |= (uint64_t)*s->in++ << s->bc;
    s->bc += 8;
  }
  *v = (uint32_t)(s->bb & ((1u << n) - 1));
  s->bb >>= n;
  s->bc -= n;
  return GZ_OK;
}

static void copy_match_slow(uint8_t *op, uint32_t dist, uint32_t len) {
  const uint8_t *src = op - dist;
  while (len--) *op++ = *src++;
}

static int inflate_careful(wreath_gzip_decoder_dec *d, struct wreath_gzip_decoder_st *s) {
  const uint8_t *in = s->in, *in_end = s->in_end;
  uint8_t *out = s->out, *out_end = s->out_end, *win = s->win;
  uint64_t bb = s->bb;
  uint32_t bc = s->bc;
  const uint32_t *lt = d->lit, *dt = d->dist;
  const uint32_t lmask = (1u << d->lbits) - 1;
  const uint32_t dmask = (1u << d->dbits) - 1;
  int rc;

#define FILL()                       \
  while (bc < 56 && in < in_end) {   \
    bb |= (uint64_t)*in++ << bc;     \
    bc += 8;                         \
  }
#define FILLN(n)                     \
  while (bc < (n) && in < in_end) {  \
    bb |= (uint64_t)*in++ << bc;     \
    bc += 8;                         \
  }

  for (;;) {
    uint32_t e, xb, len, dist;

    FILL();
    e = lt[bb & lmask];
    if (!GZ_IS_LIT(e) && (e & GZ_K_MASK) == GZ_K_SUB) {
      if (GZ_E_BITS(e) > bc) {
        rc = GZ_ERR_TRUNCATED;
        goto fail;
      }
      bb >>= d->lbits;
      bc -= d->lbits;
      e = lt[GZ_E_VAL(e) + (uint32_t)(bb & ((1u << GZ_E_XB(e)) - 1))];
    }
    if (GZ_E_BITS(e) > bc) {
      rc = GZ_ERR_TRUNCATED;
      goto fail;
    }
    if (GZ_IS_LIT(e)) {
      if (out == out_end) {
        rc = GZ_ERR_SPACE;
        goto fail;
      }
      *out++ = GZ_E_LITBYTE(e);
      bb >>= GZ_E_BITS(e);
      bc -= GZ_E_BITS(e);
      continue;
    }
    if ((e & GZ_K_MASK) == GZ_K_EOB) {
      bb >>= GZ_E_BITS(e);
      bc -= GZ_E_BITS(e);
      rc = GZ_OK;
      goto done;
    }
    if ((e & GZ_K_MASK) != GZ_K_MATCH) {
      rc = GZ_ERR_DATA;
      goto fail;
    }
    bb >>= GZ_E_BITS(e);
    bc -= GZ_E_BITS(e);
    xb = GZ_E_XB(e);
    FILLN(xb);
    if (xb > bc) {
      rc = GZ_ERR_TRUNCATED;
      goto fail;
    }
    len = GZ_E_VAL(e) + (uint32_t)(bb & ((1u << xb) - 1));
    bb >>= xb;
    bc -= xb;

    FILL();
    e = dt[bb & dmask];
    if ((e & GZ_K_MASK) == GZ_K_SUB) {
      if (GZ_E_BITS(e) > bc) {
        rc = GZ_ERR_TRUNCATED;
        goto fail;
      }
      bb >>= d->dbits;
      bc -= d->dbits;
      e = dt[GZ_E_VAL(e) + (uint32_t)(bb & ((1u << GZ_E_XB(e)) - 1))];
    }
    if (e & GZ_K_MASK) {
      rc = GZ_ERR_DATA;
      goto fail;
    }
    if (GZ_E_BITS(e) > bc) {
      rc = GZ_ERR_TRUNCATED;
      goto fail;
    }
    bb >>= GZ_E_BITS(e);
    bc -= GZ_E_BITS(e);
    xb = GZ_E_XB(e);
    FILLN(xb);
    if (xb > bc) {
      rc = GZ_ERR_TRUNCATED;
      goto fail;
    }
    dist = GZ_E_VAL(e) + (uint32_t)(bb & ((1u << xb) - 1));
    bb >>= xb;
    bc -= xb;
    if ((size_t)dist > (size_t)(out - win)) {
      rc = GZ_ERR_DATA;
      goto fail;
    }
    if ((size_t)len > (size_t)(out_end - out)) {
      rc = GZ_ERR_SPACE;
      goto fail;
    }
    copy_match_slow(out, dist, len);
    out += len;
  }
#undef FILL
#undef FILLN

done:
fail:
  s->in = in;
  s->out = out;
  s->bb = bb;
  s->bc = bc;
  return rc;
}

/* ---- block headers ------------------------------------------------------- */

#if GZ_FIXED_DIRECT
static void fill_fixed_run(uint32_t *table, uint32_t root, uint32_t first,
                           uint32_t nsym, uint32_t bits, uint32_t first_code) {
  uint32_t step = 1u << bits, end = 1u << root;
  for (uint32_t i = 0; i < nsym; i++) {
    uint32_t code = first_code + i;
    uint32_t r = bits <= 8 ? (uint32_t)rev8[code] >> (8 - bits)
                           : rev_code(code, 16 - bits);
    uint32_t e = ent_lit[first + i] | bits;
    for (uint32_t j = r; j < end; j += step) table[j] = e;
  }
}
#endif

static int load_fixed(wreath_gzip_decoder_dec *d) {
  if (d->fixed_loaded) return GZ_OK;
  d->have_dynamic = 0;
#if GZ_FIXED_DIRECT
  /* RFC 1951 fixes both the symbols and their forward canonical code ranges.
   * Filling those four ranges directly avoids manufacturing 288 lengths,
   * histogramming and sorting them, then running the hostile-input general
   * builder on a tree known at compile time.  Dynamic trees continue through
   * the fully checked builder; this path is deliberately decode-only. */
  fill_fixed_run(d->lit, 9, 256, 24, 7, 0);
  fill_fixed_run(d->lit, 9, 0, 144, 8, 48);
  fill_fixed_run(d->lit, 9, 280, 8, 8, 192);
  fill_fixed_run(d->lit, 9, 144, 112, 9, 400);
  for (uint32_t s = 0; s < 32; s++)
    d->dist[rev8[s] >> 3] = ent_dist[s] | 5;
  d->lbits = 9;
  d->dbits = 5;
#else
  uint8_t lens[288];
  uint16_t count[16];
  memset(count, 0, sizeof count);
  for (uint32_t i = 0; i < 288; i++) {
    lens[i] = (uint8_t)(i < 144 ? 8 : i < 256 ? 9 : i < 280 ? 7 : 8);
    count[lens[i]]++;
  }
  d->lbits = build_table(d, d->lit, GZ_LIT_TAB, lens, 288, count, ent_lit, 0, GZ_LIT_ROOT);
  if (!d->lbits) return GZ_ERR_DATA;
  memset(count, 0, sizeof count);
  for (uint32_t i = 0; i < 32; i++) lens[i] = 5;
  count[5] = 32;
  d->dbits = build_table(d, d->dist, GZ_DIST_TAB, lens, 32, count, ent_dist, 0, GZ_DIST_ROOT);
  if (!d->dbits) return GZ_ERR_DATA;
#endif
  d->fixed_loaded = 1;
  d->fused_live = 0;
#if GZ_COMPACT_LITROOT
  build_compact_litroot(d);
#endif
  return GZ_OK;
}

#if GZ_PRECODE_DIRECT
/* The dynamic-header precode is a complete code over only 19 symbols, with a
 * maximum length of seven and therefore no subtables. Build it straight from
 * canonical next-code counters: the general builder's zero-run sorting and
 * subtable machinery cannot contribute anything to this shape. */
static uint32_t build_precode(uint32_t *table, const uint8_t *lens,
                              uint16_t *count) {
  count[0] = 0;
  uint32_t maxlen = 7;
  while (maxlen && count[maxlen] == 0) maxlen--;
  if (!maxlen) return 0;

  int left = 1;
  for (uint32_t l = 1; l <= 7; l++) {
    left = (left << 1) - (int)count[l];
    if (left < 0) return 0;
  }
  if (left != 0) return 0;

  uint16_t next[8];
  uint32_t code = 0;
  for (uint32_t l = 1; l <= maxlen; l++) {
    code = (code + count[l - 1]) << 1;
    next[l] = (uint16_t)code;
  }
  uint32_t end = 1u << maxlen;
  for (uint32_t s = 0; s < 19; s++) {
    uint32_t l = lens[s];
    if (!l) continue;
    uint32_t r = (uint32_t)rev8[next[l]++] >> (8 - l);
    uint32_t e = ent_pre[s] | l;
    for (uint32_t i = r; i < end; i += 1u << l) table[i] = e;
  }
  return maxlen;
}
#endif

#if GZ_HEADER_MEMO
/* Consume up to 32 logical bits from a copied reader.  This deliberately uses
 * the careful byte refill: header memoisation is entered once per dynamic
 * block, and exact comparison is required before validated tables can be
 * reused. */
static int header_take(uint64_t *bb, uint32_t *bc, const uint8_t **in,
                       const uint8_t *in_end, uint32_t n, uint32_t *v) {
  while (*bc < n && *in < in_end) {
    *bb |= (uint64_t)*(*in)++ << *bc;
    *bc += 8;
  }
  if (*bc < n) return 0;
  *v = n == 32 ? (uint32_t)*bb : (uint32_t)*bb & ((1u << n) - 1u);
  *bb >>= n;
  *bc -= n;
  return 1;
}

static inline int header_memo_hit(wreath_gzip_decoder_dec *d, struct wreath_gzip_decoder_st *s) {
  if (!d->header_valid || !d->have_dynamic || !d->header_nbits) return 0;
  uint64_t bb = s->bb;
  uint32_t bc = s->bc;
  const uint8_t *in = s->in;
  uint32_t left = d->header_nbits, word = 0;
  while (left) {
    uint32_t n = left < 32 ? left : 32, v;
    if (!header_take(&bb, &bc, &in, s->in_end, n, &v) ||
        v != d->header_bits[word++])
      return 0;
    left -= n;
  }
  s->bb = bb;
  s->bc = bc;
  s->in = in;
  d->fixed_loaded = 0;
  d->fused_live = 0;
  return 1;
}

static void remember_header(wreath_gzip_decoder_dec *d, uint64_t start_bb, uint32_t start_bc,
                            const uint8_t *start_in, const struct wreath_gzip_decoder_st *s) {
  ptrdiff_t bytes = s->in - start_in;
  if (bytes < 0) return;
  uint64_t loaded = (uint64_t)start_bc + (uint64_t)bytes * 8u;
  if (loaded < s->bc) return;
  uint64_t nbits64 = loaded - s->bc;
  if (!nbits64 || nbits64 > GZ_HEADER_MAX_BITS) {
    d->header_valid = 0;
    return;
  }
  uint64_t bb = start_bb;
  uint32_t bc = start_bc;
  const uint8_t *in = start_in;
  uint32_t left = (uint32_t)nbits64, word = 0;
  while (left) {
    uint32_t n = left < 32 ? left : 32, v;
    if (!header_take(&bb, &bc, &in, s->in_end, n, &v)) {
      d->header_valid = 0;
      return;
    }
    d->header_bits[word++] = v;
    left -= n;
  }
  d->header_nbits = (uint16_t)nbits64;
  d->header_valid = 1;
}
#endif

static int load_dynamic(wreath_gzip_decoder_dec *d, struct wreath_gzip_decoder_st *s) {
#if GZ_HEADER_MEMO
  if (d->header_hot && header_memo_hit(d, s)) return GZ_OK;
  const uint64_t start_bb = s->bb;
  const uint32_t start_bc = s->bc;
  const uint8_t *const start_in = s->in;
#endif
  uint64_t bb = s->bb;
  uint32_t bc = s->bc;
  const uint8_t *in = s->in;
  const uint8_t *const in_end = s->in_end;
  int rc = GZ_OK;

  /* Refill a whole word wherever eight bytes remain, and fall back to the
   * byte loop only at the very end of the input. The header reader used to be
   * a byte-at-a-time loop with two tests per byte; on a 10 kB body it decodes
   * ~320 code-length symbols against ~4600 data symbols, so it is a real share
   * of the decode and not a cold path at all. */
#define HFILL(need)                                       \
  do {                                                    \
    if (in_end - in >= 8) {                               \
      bb |= wreath_gzip_decoder_ld64(in) << bc;                            \
      in += (63 - bc) >> 3;                               \
      bc |= 56;                                           \
    } else {                                              \
      while (bc < (need) && in < in_end) {                \
        bb |= (uint64_t)*in++ << bc;                      \
        bc += 8;                                          \
      }                                                   \
      if (bc < (need)) {                                  \
        rc = GZ_ERR_TRUNCATED;                            \
        goto out;                                         \
      }                                                   \
    }                                                     \
  } while (0)
#define HTAKE(n) (bb >>= (n), bc -= (n))

  if (bc < 14) HFILL(14);
  uint32_t hlit = (uint32_t)(bb & 31) + 257;
  uint32_t hdist = (uint32_t)((bb >> 5) & 31) + 1;
  uint32_t hclen = (uint32_t)((bb >> 10) & 15) + 4;
  HTAKE(14);
  if (hlit > 286 || hdist > 30) {
    rc = GZ_ERR_DATA;
    goto out;
  }

  uint8_t clens[19];
  uint16_t ccount[16];
  memset(clens, 0, sizeof clens);
  memset(ccount, 0, sizeof ccount);
  for (uint32_t i = 0; i < hclen; i++) {
    if (bc < 3) HFILL(3);
    uint32_t v = (uint32_t)(bb & 7);
    HTAKE(3);
    clens[wreath_gzip_decoder_clen_order[i]] = (uint8_t)v;
    ccount[v]++;
  }
#if GZ_PRECODE_DIRECT
  d->pbits = build_precode(d->pre, clens, ccount);
#else
  d->pbits = build_table(d, d->pre, GZ_PRE_TAB, clens, 19, ccount, ent_pre, 1,
                         GZ_PRE_ROOT);
#endif
  if (!d->pbits) {
    rc = GZ_ERR_DATA;
    goto out;
  }

  /* The length histograms are accumulated here rather than in a second pass
   * over lens[] inside build_table: this reader has to touch every length
   * anyway, so the histogram costs one add per symbol instead of a pass. */
  uint16_t *cl = d->count, *cd = d->dcount, *cc = cl;
  memset(cl, 0, sizeof d->count);
  memset(cd, 0, sizeof d->dcount);

  uint8_t *lens = d->lens[d->lens_active ^ 1u];
  uint32_t n = hlit + hdist, i = 0;
  const uint32_t pmask = (1u << d->pbits) - 1;
  while (i < n) {
    /* One refill covers a code (<= 7 bits) and its repeat count (<= 7). */
    if (bc < 14) HFILL(14);
    uint32_t e = d->pre[bb & pmask];
    if (!GZ_IS_LIT(e)) {
      rc = GZ_ERR_DATA;
      goto out;
    }
    HTAKE(GZ_E_BITS(e));
    uint32_t sym = (e >> 8) & 0xFFu;
    if (sym < 16) {
      lens[i] = (uint8_t)sym;
      cc[sym]++;
      i++;
      if (i == hlit) cc = cd;
      continue;
    }
    uint32_t rep, fill;
    if (sym == 16) {
      if (i == 0) {
        rc = GZ_ERR_DATA;
        goto out;
      }
      rep = 3 + (uint32_t)(bb & 3);
      HTAKE(2);
      fill = lens[i - 1];
    } else if (sym == 17) {
      rep = 3 + (uint32_t)(bb & 7);
      HTAKE(3);
      fill = 0;
    } else {
      rep = 11 + (uint32_t)(bb & 127);
      HTAKE(7);
      fill = 0;
    }
    if (i + rep > n) {
      rc = GZ_ERR_DATA;
      goto out;
    }
    memset(lens + i, (int)fill, rep);
    if (i + rep <= hlit) {
      cl[fill] += (uint16_t)rep;
    } else if (i >= hlit) {
      cd[fill] += (uint16_t)rep;
    } else {
      cl[fill] += (uint16_t)(hlit - i);
      cd[fill] += (uint16_t)(i + rep - hlit);
    }
    i += rep;
    if (i >= hlit) cc = cd;
  }
  s->bb = bb;
  s->bc = bc;
  s->in = in;
  if (lens[256] == 0) return GZ_ERR_DATA; /* no end-of-block code */
  d->fixed_loaded = 0;
  d->fused_live = 0;
#if GZ_TABLE_REUSE
  /* A matching alphabet means the already-validated tables are exact. The
   * inactive bank lets hostile input be parsed and compared without destroying
   * the cached lengths first. */
  if (d->have_dynamic && d->hlit == hlit && d->hdist == hdist &&
      memcmp(d->lens[d->lens_active], lens, n) == 0) {
#if GZ_HEADER_MEMO
    remember_header(d, start_bb, start_bc, start_in, s);
    d->header_hot = d->header_valid;
#endif
    return GZ_OK;
  }
#endif
  d->have_dynamic = 0; /* the builds below overwrite the cached tables */
  /* build_table only reads count[1..15] and clears count[0]. The header reader
   * already produced exactly those histograms, and pick_fused likewise ignores
   * count[0], so copying both 32-byte arrays per build was redundant. */
  cl[0] = 0;
  cd[0] = 0;
  d->lbits = build_table(d, d->lit, GZ_LIT_TAB, lens, hlit, cl, ent_lit, 0,
                         GZ_LIT_ROOT);
  if (!d->lbits) return GZ_ERR_DATA;
  d->dbits = build_table(d, d->dist, GZ_DIST_TAB, lens + hlit, hdist, cd, ent_dist, 0,
                         GZ_DIST_ROOT);
  if (!d->dbits) return GZ_ERR_DATA;
  d->lens_active ^= 1u;
  d->hlit = hlit;
  d->hdist = hdist;
  d->have_dynamic = 1;
#if GZ_COMPACT_LITROOT
  build_compact_litroot(d);
#endif
#if GZ_HEADER_MEMO
  remember_header(d, start_bb, start_bc, start_in, s);
  d->header_hot = 0;
#endif
  return GZ_OK;

out:
  s->bb = bb;
  s->bc = bc;
  s->in = in;
  return rc;
#undef HFILL
#undef HTAKE
}

/* Is a fused root table worth building for the block that is about to be
 * decoded? The table costs 2^root entries to build and repays about six
 * instructions on each literal it fuses beyond the first, so the question is
 * how many literals the block will decode and how fusible they are.
 *
 * Neither quantity is known exactly, and both are estimated from the code
 * itself rather than from the compressed length -- a compressed length says
 * nothing about how many blocks share it, which is why arm 4's note calls that
 * signal unsafe outside its two benchmark bodies.
 *
 *   fusibility  the Kraft mass of the literal alphabet, sum over literal
 *               symbols of 2^-len. That is exactly the probability that the
 *               next symbol is a literal, if the code is optimal for the
 *               block -- which is what the encoder was trying to make it.
 *   short-mass  the same sum restricted to codes of at most root/2 bits, which
 *               is the probability that two literals fit in one root lookup.
 *
 * The block is worth fusing when the expected number of fused literals per
 * root-sized window exceeds one -- i.e. when a fused lookup does strictly more
 * work than the single-symbol lookup it replaces -- and there are enough
 * symbols left in the input to pay the build off. Both terms are thresholds on
 * ratios computed from the length histogram, not on this corpus.
 */
static int pick_fused(const wreath_gzip_decoder_dec *d, size_t in_left) {
#if !GZ_AUTO_FUSED
  (void)d;
  (void)in_left;
  return 0;
#else
  const uint32_t root = d->lbits;
  if (root != GZ_LIT_ROOT) return 0;

  /* Three sums, all scaled by 2^15, over the length histogram the header
   * reader already had to accumulate.
   *
   *   M  = sum over *literal* symbols of 2^-len -- the Kraft mass of the
   *        literal alphabet, which is the probability that the next symbol is
   *        a literal if the code is optimal for this block. That is what the
   *        encoder was trying to make it.
   *   W  = sum over literal symbols of len * 2^-len, so W/M is the mean
   *        literal code length.
   *   Wa = the same over the whole literal/length alphabet, so Wa/2^15 is the
   *        mean bits per token.
   *
   * The literal-only sums are obtained by subtracting the 30 length codes from
   * the whole-alphabet ones, rather than by histogramming 256 symbols a second
   * time: the length codes are the only part of the alphabet small enough to
   * walk directly. */
  uint32_t M = 32768, W = 0, Wa = 0;
  for (uint32_t l = 1; l <= 15; l++) {
    uint32_t m = (uint32_t)d->count[l] << (15 - l);
    Wa += m * l;
  }
  W = Wa;
  for (uint32_t s = 256; s < d->hlit; s++) {
    uint32_t l = d->lens[d->lens_active][s];
    if (!l) continue;
    uint32_t m = 1u << (15 - l);
    M -= m;
    W -= m * l;
  }
  if (!M || !W || !Wa) return 0;

  /* Expected literals emitted by one fused root lookup: the mass that is
   * literal, times how many mean-length literal codes fit in `root` bits. */
  double per_window = (double)root * (double)M * (double)M / (32768.0 * (double)W);

  /* A fused window costs GZ_FUSE_WINDOW instructions and replaces GZ_FUSE_LIT
   * per literal, so it has to carry more than their ratio in literals to be
   * worth taking at all. Both are counted off the disassembly of the two loops
   * in this build; neither is fitted to a corpus, and the whole reason the
   * fused table lost here is that the single-symbol literal path got cheap
   * enough to move that ratio from 1.43 to 2.0. */
  if (per_window < GZ_FUSE_WINDOW / GZ_FUSE_LIT) return 0;

  /* Amortisation. The build is 2^root entries; each fused window saves
   * (GZ_FUSE_LIT*k - GZ_FUSE_WINDOW) instructions and the block holds about
   * N_lit/k of them, so the saving has to beat the build by a margin. N_lit is
   * estimated from the input still unread and the mean bits per token --
   * deliberately from the *code*, not from the compressed length, which says
   * nothing about how many blocks share it. */
  double n_lit = (double)in_left * 8.0 * (double)M / (double)Wa;
  double saving = n_lit * (GZ_FUSE_LIT - GZ_FUSE_WINDOW / per_window);
  double build = (double)(1u << root) * GZ_FUSE_BUILD_COST;
  return saving > 2.0 * build;
#endif
}

static int inflate_member(wreath_gzip_decoder_dec *d, struct wreath_gzip_decoder_st *s) {
  const int arm = d->arm;
  for (;;) {
    uint32_t final, type;
    int rc;
    if ((rc = need_bits(s, 1, &final))) return rc;
    if ((rc = need_bits(s, 2, &type))) return rc;
    if (type == 0) {
      /* Stored: discard the partial byte, then one bulk copy. */
      uint32_t drop = s->bc & 7;
      s->bb >>= drop;
      s->bc -= drop;
      uint32_t lo, hi, nlo, nhi;
      if ((rc = need_bits(s, 8, &lo))) return rc;
      if ((rc = need_bits(s, 8, &hi))) return rc;
      if ((rc = need_bits(s, 8, &nlo))) return rc;
      if ((rc = need_bits(s, 8, &nhi))) return rc;
      uint32_t len = lo | (hi << 8), nlen = nlo | (nhi << 8);
      if ((len ^ 0xFFFFu) != nlen) return GZ_ERR_DATA;
      /* Whole bytes may still be sitting in the bit buffer. */
      while (s->bc >= 8 && len) {
        if (s->out == s->out_end) return GZ_ERR_SPACE;
        *s->out++ = (uint8_t)s->bb;
        s->bb >>= 8;
        s->bc -= 8;
        len--;
      }
      if ((size_t)len > (size_t)(s->in_end - s->in)) return GZ_ERR_TRUNCATED;
      if ((size_t)len > (size_t)(s->out_end - s->out)) return GZ_ERR_SPACE;
      memcpy(s->out, s->in, len);
      s->in += len;
      s->out += len;
    } else if (type == 3) {
      return GZ_ERR_DATA;
    } else {
      if (type == 1) {
        rc = load_fixed(d);
      } else {
        rc = load_dynamic(d, s);
      }
      if (rc) return rc;
      int fused = pick_fused(d, (size_t)(s->in_end - s->in));
      if (fused) {
        wreath_gzip_decoder_build_fused(d);
      }
      for (;;) {
        int r;
        int long_copy = d->format == GZ_FMT_HTML || d->format == GZ_FMT_GRAPHQL;
        if (fused) {
          if (arm == GZ_DEC_BMI2_AVX2)
            r = long_copy ? wreath_gzip_decoder_inflate_fused_bmi2_avx2_long(s)
                          : wreath_gzip_decoder_inflate_fused_bmi2_avx2(s);
          else if (arm == GZ_DEC_BMI2) r = wreath_gzip_decoder_inflate_fused_bmi2(s);
          else r = wreath_gzip_decoder_inflate_fused_scalar(s);
        } else if (arm == GZ_DEC_BMI2_AVX2) {
          r = long_copy ? wreath_gzip_decoder_inflate_fast_bmi2_avx2_long(s)
                        : wreath_gzip_decoder_inflate_fast_bmi2_avx2(s);
        } else if (arm == GZ_DEC_BMI2) {
          r = wreath_gzip_decoder_inflate_fast_bmi2(s);
        } else {
          r = wreath_gzip_decoder_inflate_fast_scalar(s);
        }
        if (r < 0) return r;
        if (r == 1) break;
        r = inflate_careful(d, s);
        if (r) return r;
        break;
      }
    }
    if (final) return GZ_OK;
  }
}

int wreath_gzip_decoder_decompress(wreath_gzip_decoder_dec *d, const void *in_, size_t in_len, void *out_,
                  size_t out_cap, size_t *out_len) {
  struct wreath_gzip_decoder_st s;
  s.in = (const uint8_t *)in_;
  s.in_end = s.in + in_len;
  s.out = (uint8_t *)out_;
  s.out_end = s.out + out_cap;
  s.win = s.out;
  s.bb = 0;
  s.bc = 0;
  s.d = d;
  *out_len = 0;

  for (;;) {
    /* RFC 1952 section 2.3 */
    if ((size_t)(s.in_end - s.in) < 18) return GZ_ERR_TRUNCATED;
    if (s.in[0] != 0x1F || s.in[1] != 0x8B) return GZ_ERR_HEADER;
    if (s.in[2] != 8) return GZ_ERR_HEADER;
    uint32_t flg = s.in[3];
    if (flg & 0xE0) return GZ_ERR_HEADER; /* reserved bits must be zero */
    const uint8_t *hstart = s.in;
    s.in += 10;
    if (flg & 0x04) { /* FEXTRA */
      if ((size_t)(s.in_end - s.in) < 2) return GZ_ERR_TRUNCATED;
      uint32_t xlen = (uint32_t)s.in[0] | ((uint32_t)s.in[1] << 8);
      s.in += 2;
      if ((size_t)(s.in_end - s.in) < xlen) return GZ_ERR_TRUNCATED;
      s.in += xlen;
    }
    if (flg & 0x08) { /* FNAME */
      while (s.in < s.in_end && *s.in) s.in++;
      if (s.in == s.in_end) return GZ_ERR_TRUNCATED;
      s.in++;
    }
    if (flg & 0x10) { /* FCOMMENT */
      while (s.in < s.in_end && *s.in) s.in++;
      if (s.in == s.in_end) return GZ_ERR_TRUNCATED;
      s.in++;
    }
    if (flg & 0x02) { /* FHCRC */
      if ((size_t)(s.in_end - s.in) < 2) return GZ_ERR_TRUNCATED;
      uint32_t want = (uint32_t)s.in[0] | ((uint32_t)s.in[1] << 8);
      if ((wreath_gzip_decoder_crc32_arm(d->crc_arm, 0, hstart,
                                         (size_t)(s.in - hstart)) & 0xFFFFu) != want)
        return GZ_ERR_HEADER;
      s.in += 2;
    }
    if ((size_t)(s.in_end - s.in) < 8) return GZ_ERR_TRUNCATED;

    uint8_t *member_start = s.out;
    s.win = s.out;
    s.bb = 0;
    s.bc = 0;
    int rc = inflate_member(d, &s);
    if (rc) return rc;

    /* RFC 1952 puts the trailer immediately after the deflate data, so that is
     * where it is read from -- not at the end of whatever buffer we were
     * handed. Deriving it from the buffer end makes a second concatenated
     * member look like a corrupt CRC instead of what it is, and makes the
     * trailing-garbage check untestable. Whole bytes still held in the bit
     * buffer were read ahead and belong to the trailer. */
    s.in -= (s.bc >> 3);
    s.bb = 0;
    s.bc = 0;
    if ((size_t)(s.in_end - s.in) < 8) return GZ_ERR_TRUNCATED;
    uint32_t want_crc = wreath_gzip_decoder_ld32(s.in);
    uint32_t want_len = wreath_gzip_decoder_ld32(s.in + 4);
    s.in += 8;

    size_t produced = (size_t)(s.out - member_start);
    if ((uint32_t)produced != want_len) return GZ_ERR_LENGTH;
    if (wreath_gzip_decoder_crc32_arm(d->crc_arm, 0, member_start, produced) != want_crc)
      return GZ_ERR_CRC;

    if (s.in == s.in_end) break;
    /* RFC 1952 permits a file to be a series of members; anything that is not
     * another member's header is trailing garbage. */
    if ((size_t)(s.in_end - s.in) < 18 || s.in[0] != 0x1F || s.in[1] != 0x8B)
      return GZ_ERR_TRAILING;
  }

  *out_len = (size_t)(s.out - (uint8_t *)out_);
  return GZ_OK;
}

/* The LZ77 parse, instantiated once per profile class.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Arm 2 measured that making the search knobs compile-time constants in the
 * inner loop, rather than loads from a level struct, is worth having; arm 3
 * measured that one extra well-predicted branch per symbol costs 2.3%. Both
 * point the same way, so each shape is its own function and the choice is made
 * once per wreath_gzip_encoder_encode() call.
 *
 * Macros the includer must define:
 *   GZP_NAME     function-name suffix
 *   GZP_CHAINED  1 = walk a hash chain, 0 = one probe against the head only
 *   GZP_INSERT   1 = insert every position into the hash, 0 = only searched ones
 *   GZP_LAZY     1 = try a longer match one byte later
 *   GZP_MIN3     1 = also look for three-byte matches at short distance
 */
#include "defl.h"

#ifndef GZP_NAME
#error "define GZP_NAME before including parse_core.h"
#endif
#define GZP_CAT2(a, b) a##b
#define GZP_CAT(a, b) GZP_CAT2(a, b)
#define GZP(name) GZP_CAT(name, GZP_NAME)

/* Search knobs: constants when the instantiation names them, otherwise loads
 * from the profile (the generic shape the ablation overrides go through). */
#ifndef GZP_LAZYDEPTH
#define GZP_LAZYDEPTH 1
#endif
#ifndef GZP_SHORTPRICE
#define GZP_SHORTPRICE 0
#endif
#ifndef GZP_SHORTMODE
#define GZP_SHORTMODE GZ_SHORTMODE
#endif
#ifndef GZP_CACHE_BEST
#define GZP_CACHE_BEST GZ_CACHE_BEST_BYTE
#endif
#ifndef GZP_STORE_CHAIN
#define GZP_STORE_CHAIN GZP_CHAINED
#endif
#ifdef GZP_CHAIN_C
#define GZP_CHAIN_V GZP_CHAIN_C
#define GZP_NICE_V GZP_NICE_C
#define GZP_LAZY_V GZP_LAZY_C
#define GZP_GOOD_V GZP_GOOD_C
#else
#define GZP_CHAIN_V chain_
#define GZP_NICE_V nice_
#define GZP_LAZY_V lazy_
#define GZP_GOOD_V good_
#endif

/* Common-prefix length of p and q, capped at max. The caller guarantees p+max
 * and q+max are both inside the input buffer, so the wide loads never read past
 * the mapping. Arm 3 measured an AVX2 version of this as a 1.56% loss — most
 * extensions die inside the first eight bytes — and a same-source -mavx2 build
 * put the loss in the vector code rather than in codegen. Not rebuilt. */
static inline unsigned GZP(wreath_gzip_encoder_ext_)(const uint8_t *p, const uint8_t *q, unsigned max) {
  unsigned i = 0;
  while (i + 8 <= max) {
    uint64_t x = wreath_gzip_encoder_ld64(p + i) ^ wreath_gzip_encoder_ld64(q + i);
    if (x) return i + (unsigned)(__builtin_ctzll(x) >> 3);
    i += 8;
  }
#if GZ_EXT_WIDE_TAIL
  /* A capped match commonly reaches the six-byte tail of the 258-byte limit.
   * Finish that tail as 4/2/1-byte comparisons instead of up to seven scalar
   * byte iterations. memcpy-based loads keep this valid on strict-alignment
   * targets; ctz is reached only for a non-zero XOR. */
  if (i + 4 <= max) {
    uint32_t x = wreath_gzip_encoder_ld32(p + i) ^ wreath_gzip_encoder_ld32(q + i);
    if (x) return i + (unsigned)(__builtin_ctz(x) >> 3);
    i += 4;
  }
  if (i + 2 <= max) {
    unsigned x = (unsigned)(wreath_gzip_encoder_ld16(p + i) ^ wreath_gzip_encoder_ld16(q + i));
    if (x) return i + (unsigned)(__builtin_ctz(x) >> 3);
    i += 2;
  }
  if (i < max && p[i] == q[i]) i++;
#else
  while (i < max && p[i] == q[i]) i++;
#endif
  return i;
}

#if GZP_CHAINED
/* Walks the hash chain from `cand`. Returns 0 unless it beat `best`, otherwise
 * the new length, with the distance in *bestdist. */
/* Inlined on purpose. Left out of line, GCC passes four of the nine arguments
 * on the stack and spills the out-parameter, which measured as a third of the
 * whole encode at chain 1 -- arm 1 saw the same effect from the other side
 * (+5.2% when it forced its find_match out of line). */
static inline __attribute__((always_inline)) unsigned
GZP(wreath_gzip_encoder_find_)(const uint8_t *in, uint32_t pos, uint32_t n, uint32_t cand,
                              const wreath_gzip_encoder_link *prev, unsigned chain, unsigned nice,
                              unsigned good, unsigned best, unsigned *bestdist) {
  /* An empty chain is the common case on high-redundancy input and does not
   * deserve the loads and shifts below. On low-redundancy input it never fires,
   * which is exactly why plaintext is the expensive shape. */
  if (cand >= pos) return 0;
  unsigned maxlen = n - pos;
  if (maxlen > GZ_MAX_MATCH) maxlen = GZ_MAX_MATCH;
  if (maxlen < best + 1 || maxlen < 4) return 0;

  const uint8_t *p = in + pos;
  uint32_t p4 = wreath_gzip_encoder_ld32(p);
  uint32_t limit = pos > GZ_WINDOW ? pos - GZ_WINDOW : 0;
  uint32_t span = pos - limit;
  uint32_t cur = cand;
  unsigned bl = best, bd = 0;
#if GZP_CACHE_BEST
  uint8_t pbest = p[best];
#endif

  /* Once a decent match is already in hand the tail of the chain is nearly all
   * loss: quarter the remaining budget. Arm 1 measured this at 13% of encode
   * for 0.36% of output. */
  if (good && bl >= good) chain >>= 2;

  while (cur - limit < span) {
    const uint8_t *q = in + cur;
#if GZ_PREFETCH
    /* The one lever aimed straight at the cache axis. The walk is a pointer
     * chase: `cur = prev[cur]` then two loads from `in + cur`, all scattered
     * over the last 32 KiB of input, which is exactly L1d-sized. Loading the
     * next link before using the current one turns a dependent chain into a
     * one-step lookahead and lets the input line start moving early. Measured
     * both ways -- see RESULTS.md. */
    uint32_t nxt = GZ_LINK_GET(prev, cur);
    __builtin_prefetch(in + nxt, 0, 1);
#endif
    /* Two cheap rejects before the wide compare: the byte that would have to
     * improve on the incumbent, then the four bytes the hash claimed. Removing
     * the first one measured +37.9% in arm 3. */
#if GZP_CACHE_BEST
    if (q[bl] == pbest && wreath_gzip_encoder_ld32(q) == p4) {
#else
    if (q[bl] == p[bl] && wreath_gzip_encoder_ld32(q) == p4) {
#endif
      unsigned l = 4 + GZP(wreath_gzip_encoder_ext_)(p + 4, q + 4, maxlen - 4);
      if (l > bl) {
        bl = l;
        bd = pos - cur;
        if (l >= nice || l >= maxlen) break;
#if GZP_CACHE_BEST
        pbest = p[bl];
#endif
      }
    }
    if (--chain == 0) break;
#if GZ_PREFETCH
    cur = nxt;
#else
    cur = GZ_LINK_GET(prev, cur);
#endif
  }
  if (bd == 0) return 0;
  *bestdist = bd;
  return bl;
}
#endif

size_t GZP(wreath_gzip_encoder_parse_)(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t pos_in, size_t stop_in,
                      size_t n_in, const struct wreath_gzip_encoder_prof *P) {
  (void)P; /* compile-time-specialised instantiations do not load the profile */
  uint32_t *head = e->head;
  uint32_t base = e->base;
#ifdef GZP_HASH_BITS
  const unsigned hb = GZP_HASH_BITS;
#else
  unsigned hb = e->hash_bits;
#endif
  uint32_t pos = (uint32_t)pos_in, stop = (uint32_t)stop_in, n = (uint32_t)n_in;
#if GZP_CHAINED
  wreath_gzip_encoder_link *prev = e->prev;
#ifndef GZP_CHAIN_C
  const unsigned chain_ = P->chain, nice_ = P->nice, good_ = P->good, lazy_ = P->max_lazy;
#endif
#endif
#if GZP_MIN3
  uint32_t *head3 = e->head3;
#endif
#if GZP_SHORTPRICE || GZP_MIN3
  const unsigned *maxdist = e->maxdist;
#endif
#if GZP_SHORTPRICE && GZP_SHORTMODE >= 2
  const uint8_t *lit_cost = e->lit_cost;
  const uint8_t *short_lcost = e->short_lcost;
  const uint32_t *dmax_bits = e->dmax_bits;
#endif
#if GZP_CHAINED || GZP_INSERT
  uint32_t hpos = pos;
#endif
  /* One past the last position with four hashable bytes; zero when there are
   * fewer than four bytes, so the loop is skipped rather than over-reading. */
  uint32_t hend = n >= 4 ? n - 3 : 0;

  while (pos < hend && pos < stop) {
#if GZP_CHAINED
    while (hpos < pos) {
      uint32_t hh = wreath_gzip_encoder_hash4(wreath_gzip_encoder_ld32(in + hpos), hb);
#if GZP_STORE_CHAIN
      prev[GZ_PREV_IDX(hpos)] = GZ_LINK_PUT(head[hh] - base);
#endif
      head[hh] = base + hpos;
#if GZP_MIN3
      head3[wreath_gzip_encoder_hash3(wreath_gzip_encoder_ld32(in + hpos))] = base + hpos;
#endif
      hpos++;
    }
    uint32_t h = wreath_gzip_encoder_hash4(wreath_gzip_encoder_ld32(in + pos), hb);
    uint32_t cand = head[h] - base;
#if GZP_STORE_CHAIN
    prev[GZ_PREV_IDX(pos)] = GZ_LINK_PUT(cand);
#endif
    head[h] = base + pos;
    hpos = pos + 1;

    unsigned dist = 0;
#ifdef GZ_ABLATE_NOSEARCH
    unsigned len = 0;
    (void)cand;
#else
    unsigned len = GZP(wreath_gzip_encoder_find_)(in, pos, n, cand, prev, GZP_CHAIN_V, GZP_NICE_V,
                                 GZP_GOOD_V, 3, &dist);
#endif
#if GZP_SHORTPRICE
    /* A short match far away costs more bits than the literals it replaces.
     * At GZ_SHORTMODE 2 those literals are priced one by one -- they are the
     * bytes this match would actually cover, and they are systematically
     * cheaper than the block's average literal, which is the error mode 1 makes.
     * Only reached when a short match was found, so the loop is short and rare
     * relative to the chain walk that found it. */
    if (len && len <= GZ_SHORT_MAX) {
#if GZP_SHORTMODE >= 2
      unsigned bud = 0;
      for (unsigned k = 0; k < len; k++) bud += lit_cost[in[pos + k]];
      /* bud <= GZ_SHORT_MAX*15 and lcb >= 1, so the index is in range by
       * construction -- see GZ_DMAX_SLOTS. */
      unsigned lcb = short_lcost[len], cap = 0;
      if (bud > lcb) cap = dmax_bits[bud - lcb - 1];
      if (cap > maxdist[len]) cap = maxdist[len];
      if (dist > cap) len = 0;
#else
      if (dist > maxdist[len]) len = 0;
#endif
    }
#endif
#if GZP_MIN3
    uint32_t h3 = wreath_gzip_encoder_hash3(wreath_gzip_encoder_ld32(in + pos));
    if (!len) {
      uint32_t c3 = head3[h3] - base;
      uint32_t d3 = pos - c3;
      if (c3 < pos && d3 <= maxdist[3] && n - pos >= 3 && in[c3] == in[pos] &&
          in[c3 + 1] == in[pos + 1] && in[c3 + 2] == in[pos + 2]) {
        len = 3;
        dist = d3;
      }
    }
    head3[h3] = base + pos;
#endif
    if (len) {
#if GZP_LAZY
      if (len < GZP_LAZY_V && pos + 1 < hend) {
        uint32_t h2 = wreath_gzip_encoder_hash4(wreath_gzip_encoder_ld32(in + pos + 1), hb);
        uint32_t cand2 = head[h2] - base;
#if GZP_STORE_CHAIN
        prev[GZ_PREV_IDX(pos + 1)] = GZ_LINK_PUT(cand2);
#endif
        head[h2] = base + pos + 1;
#if GZP_MIN3
        head3[wreath_gzip_encoder_hash3(wreath_gzip_encoder_ld32(in + pos + 1))] = base + pos + 1;
#endif
        hpos = pos + 2;
        unsigned d2 = 0;
        /* The lazy probe is speculative: it only has to beat a match already in
         * hand, so it gets a quarter of the budget. GZP_LAZYDEPTH is the knob. */
        unsigned l2 = GZP(wreath_gzip_encoder_find_)(in, pos + 1, n, cand2, prev,
                                    GZP_CHAIN_V > 4 ? GZP_CHAIN_V / GZP_LAZYDEPTH : 1,
                                    GZP_NICE_V, GZP_GOOD_V, len, &d2);
        if (l2) {
          wreath_gzip_encoder_push_lit(e, in[pos]);
          if (e->ntok >= e->gate) wreath_gzip_encoder_defl_gate(e, in, pos + 1);
          pos++;
          len = l2;
          dist = d2;
        }
      }
#endif
      wreath_gzip_encoder_push_match(e, len, dist);
      pos += len;
      if (e->ntok >= e->gate) wreath_gzip_encoder_defl_gate(e, in, pos);
    } else {
      wreath_gzip_encoder_push_lit(e, in[pos]);
      pos++;
      if (e->ntok >= e->gate) wreath_gzip_encoder_defl_gate(e, in, pos);
    }
#else /* single probe against the hash head; no chain array is ever touched */
#if GZP_INSERT
    while (hpos < pos) {
      head[wreath_gzip_encoder_hash4(wreath_gzip_encoder_ld32(in + hpos), hb)] = base + hpos;
      hpos++;
    }
    hpos = pos + 1;
#endif
    uint32_t h = wreath_gzip_encoder_hash4(wreath_gzip_encoder_ld32(in + pos), hb);
    uint32_t cand = head[h] - base;
    head[h] = base + pos;
    unsigned len = 0, dist = 0;
    uint32_t d = pos - cand;
#ifdef GZ_ABLATE_NOSEARCH
    if (0) {
#else
    if (cand < pos && d <= GZ_WINDOW) {
#endif
      const uint8_t *p = in + pos, *q = in + cand;
      if (wreath_gzip_encoder_ld32(q) == wreath_gzip_encoder_ld32(p)) {
        unsigned maxlen = n - pos;
        if (maxlen > GZ_MAX_MATCH) maxlen = GZ_MAX_MATCH;
        if (maxlen >= 4) {
          len = 4 + GZP(wreath_gzip_encoder_ext_)(p + 4, q + 4, maxlen - 4);
          dist = d;
        }
      }
    }
    if (len) {
      wreath_gzip_encoder_push_match(e, len, dist);
      pos += len;
    } else {
      wreath_gzip_encoder_push_lit(e, in[pos]);
      pos++;
    }
    if (e->ntok >= e->gate) wreath_gzip_encoder_defl_gate(e, in, pos);
#endif
  }

  /* The tail: fewer than four bytes left to hash, so nothing can start a
   * match. Only reached when stop == n. */
  while (pos < stop) {
    wreath_gzip_encoder_push_lit(e, in[pos]);
    pos++;
    if (e->ntok >= e->gate) wreath_gzip_encoder_defl_gate(e, in, pos);
  }
  return pos;
}

#undef GZP_CAT2
#undef GZP_CAT
#undef GZP
#undef GZP_LAZYDEPTH
#undef GZP_SHORTPRICE
#undef GZP_SHORTMODE
#undef GZP_CACHE_BEST
#undef GZP_STORE_CHAIN
#undef GZP_CHAIN_V
#undef GZP_NICE_V
#undef GZP_LAZY_V
#undef GZP_GOOD_V
#ifdef GZP_CHAIN_C
#undef GZP_CHAIN_C
#undef GZP_NICE_C
#undef GZP_LAZY_C
#undef GZP_GOOD_C
#endif
#undef GZP_NAME
#undef GZP_CHAINED
#undef GZP_INSERT
#undef GZP_LAZY
#undef GZP_MIN3
#ifdef GZP_HASH_BITS
#undef GZP_HASH_BITS
#endif

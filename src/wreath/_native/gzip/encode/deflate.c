/* DEFLATE encoder: Huffman construction, block pricing, adaptive block
 * splitting, the bit writer and the gzip frame.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Written from RFC 1951 and RFC 1952. The per-position match loop lives in
 * parse_core.h; this file is everything that happens once per block.
 */
#include "defl.h"

#include <stdlib.h>

#include "cpu.h"
#include "crc32.h"
#include "encode_tables.h"
#include "huff.h"
#include "profiles.h"

/* Model of one extra dynamic header, in bits: 14 bits of counts, the
 * code-length alphabet's own tree, one end-of-block, and about four bits per
 * live symbol for the run-length-coded lengths. */
#ifndef GZ_SPLIT_HDR_FIX
#define GZ_SPLIT_HDR_FIX 79
#endif
#ifndef GZ_SPLIT_HDR_PER
#define GZ_SPLIT_HDR_PER 4
#endif

int wreath_gzip_encoder_tok_is_match(uint32_t t) { return (t & GZ_TOK_MATCH) != 0; }
unsigned wreath_gzip_encoder_tok_len(uint32_t t) { return GZ_TOK_LEN(t); }
unsigned wreath_gzip_encoder_tok_dist(uint32_t t) { return GZ_TOK_DIST(t); }
unsigned wreath_gzip_encoder_len_symbol(unsigned len) { return wreath_gzip_encoder_len_sym[len]; }
unsigned wreath_gzip_encoder_dist_symbol(unsigned dist) { return wreath_gzip_encoder_dist_code(dist); }

/* The profile ladder. Every row is a measured point; RESULTS.md has the
 * numbers. Nothing here is fitted to the measurement corpus: the search knobs
 * are the conventional doubling ladder, and the two triage constants and the
 * split criterion are derived from the data being encoded. */
const struct wreath_gzip_encoder_prof wreath_gzip_encoder_profiles[GZ_P_COUNT] = {
    /*                        parse            chain nice lazy good m3 spl tri */
    /* Splitting is off for the two single-probe profiles: measured on the whole
     * corpus it buys 0.07% of bytes there and costs 15.6% of the encode, which
     * is the wrong side of a trade whose entire point is cheapness. From
     * `light` up it buys 0.25% for 6-8%. */
    {"fast",  GZ_PARSE_GREEDY1_NOINS, 1, 0, 0, 0, 4, 0, 1},
    {"quick", GZ_PARSE_GREEDY1,       1, 0, 0, 0, 4, 0, 1},
    {"light", GZ_PARSE_CHAIN, GZP_LIGHT_CHAIN, GZP_LIGHT_NICE, GZP_LIGHT_LAZY,
     GZP_LIGHT_GOOD, 4, 1, 1},
    {"default", GZ_PARSE_CHAIN, GZP_DEF_CHAIN, GZP_DEF_NICE, GZP_DEF_LAZY, GZP_DEF_GOOD,
     4, 1, 1},
    {"high", GZ_PARSE_CHAIN, GZP_HIGH_CHAIN, GZP_HIGH_NICE, GZP_HIGH_LAZY, GZP_HIGH_GOOD,
     4, 1, 1},
    {"max", GZ_PARSE_CHAIN, GZP_MAX_CHAIN, GZP_MAX_NICE, GZP_MAX_LAZY, GZP_MAX_GOOD,
     4, 1, 1},
};

/* The specialised instantiation per profile, and the generic shapes the
 * ablation overrides fall back to. */
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_spec_parse[GZ_P_COUNT] = {
    wreath_gzip_encoder_parse_greedy1_noins, wreath_gzip_encoder_parse_greedy1, wreath_gzip_encoder_parse_light,
    wreath_gzip_encoder_parse_deflt,         wreath_gzip_encoder_parse_high,    wreath_gzip_encoder_parse_max};
#if GZ_HASH_SPECIALIZE_PROFILES
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_spec_parse18[GZ_P_COUNT] = {
    wreath_gzip_encoder_parse_greedy1_noins18, wreath_gzip_encoder_parse_greedy18, wreath_gzip_encoder_parse_light18,
    wreath_gzip_encoder_parse_deflt18,         wreath_gzip_encoder_parse_high18,   wreath_gzip_encoder_parse_max18};
#endif
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_fmt_json[5] = {
    wreath_gzip_encoder_parse_json14, wreath_gzip_encoder_parse_json15, wreath_gzip_encoder_parse_json16, wreath_gzip_encoder_parse_json17, wreath_gzip_encoder_parse_json18};
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_fmt_graphql[5] = {wreath_gzip_encoder_parse_graphql14, wreath_gzip_encoder_parse_graphql15,
    wreath_gzip_encoder_parse_graphql16, wreath_gzip_encoder_parse_graphql17, wreath_gzip_encoder_parse_graphql18};
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_fmt_text[5] = {
    wreath_gzip_encoder_parse_text14, wreath_gzip_encoder_parse_text15, wreath_gzip_encoder_parse_text16, wreath_gzip_encoder_parse_text17, wreath_gzip_encoder_parse_text18};
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_fmt_text_shallow[5] = {
    wreath_gzip_encoder_parse_textshallow14, wreath_gzip_encoder_parse_textshallow15,
    wreath_gzip_encoder_parse_textshallow16, wreath_gzip_encoder_parse_textshallow17,
    wreath_gzip_encoder_parse_textshallow18};
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_fmt_plaintext[5] = {wreath_gzip_encoder_parse_plaintext14, wreath_gzip_encoder_parse_plaintext15,
    wreath_gzip_encoder_parse_plaintext16, wreath_gzip_encoder_parse_plaintext17, wreath_gzip_encoder_parse_plaintext18};
static const wreath_gzip_encoder_parse_fn wreath_gzip_encoder_fmt_log[5] = {
    wreath_gzip_encoder_parse_log14, wreath_gzip_encoder_parse_log15, wreath_gzip_encoder_parse_log16, wreath_gzip_encoder_parse_log17, wreath_gzip_encoder_parse_log18};

const char *wreath_gzip_encoder_profile_name(int p) {
  return (p >= 0 && p < GZ_P_COUNT) ? wreath_gzip_encoder_profiles[p].name : "?";
}

static const char *const wreath_gzip_encoder_format_names[GZ_FMT_COUNT] = {
    "unknown", "json", "chaotic-json", "html", "graphql", "log", "plaintext"};

const char *wreath_gzip_encoder_format_name(int format) {
  return format >= 0 && format < GZ_FMT_COUNT ? wreath_gzip_encoder_format_names[format] : "?";
}

int wreath_gzip_encoder_format_by_name(const char *name) {
  for (int i = 0; i < GZ_FMT_COUNT; i++)
    if (!strcmp(name, wreath_gzip_encoder_format_names[i])) return i;
  if (!strcmp(name, "chaotic")) return GZ_FMT_CHAOTIC_JSON;
  return -1;
}

int wreath_gzip_encoder_profile_by_name(const char *name) {
  for (int i = 0; i < GZ_P_COUNT; i++)
    if (!strcmp(name, wreath_gzip_encoder_profiles[i].name)) return i;
  return -1;
}

/* ---- RFC 1951 constant tables -------------------------------------------- */

static const uint8_t cl_order[19] = {16, 17, 18, 0, 8,  7, 9,  6, 10, 5,
                                     11, 4,  12, 3, 13, 2, 14, 1, 15};

/* log2(x) in Q16 for the split criterion. Six instructions: the exponent from
 * clz, the mantissa's fractional part from a 256-entry table. */
static inline uint32_t lg_q16(uint32_t x) { /* x >= 1 */
  unsigned e = 31u - (unsigned)__builtin_clz(x);
  uint32_t m = e >= 8 ? (x >> (e - 8)) : (x << (8 - e)); /* 256..511 */
  return (e << 16) + lg_frac[m - 256];
}

/* ---- bit writer ---------------------------------------------------------- */

static inline void bw_put(struct wreath_gzip_encoder_bw *w, uint64_t v, unsigned n) {
  w->acc |= v << w->cnt;
  w->cnt += n;
  memcpy(w->out, &w->acc, 8); /* unconditional: eight bytes of slack always exist */
  w->out += w->cnt >> 3;
  w->acc >>= (w->cnt & 56);
  w->cnt &= 7;
}

/* ---- block emission ------------------------------------------------------ */

struct rle {
  uint8_t sym[320];
  uint8_t xb[320];
  uint8_t xv[320];
  unsigned n;
};

static void rle_push(struct rle *r, unsigned sym, unsigned xb, unsigned xv) {
  r->sym[r->n] = (uint8_t)sym;
  r->xb[r->n] = (uint8_t)xb;
  r->xv[r->n] = (uint8_t)xv;
  r->n++;
}

static void rle_lengths(const uint8_t *all, unsigned total, struct rle *r, uint32_t *cl_freq) {
  r->n = 0;
  memset(cl_freq, 0, 19 * sizeof *cl_freq);
  unsigned i = 0;
  while (i < total) {
    unsigned cur = all[i], run = 1;
    while (i + run < total && all[i + run] == cur) run++;
    i += run;
    if (cur == 0) {
      while (run >= 11) {
        unsigned k = run > 138 ? 138 : run;
        rle_push(r, 18, 7, k - 11);
        cl_freq[18]++;
        run -= k;
      }
      while (run >= 3) {
        unsigned k = run > 10 ? 10 : run;
        rle_push(r, 17, 3, k - 3);
        cl_freq[17]++;
        run -= k;
      }
      while (run--) { rle_push(r, 0, 0, 0); cl_freq[0]++; }
    } else {
      rle_push(r, cur, 0, 0);
      cl_freq[cur]++;
      run--;
      while (run >= 3) {
        unsigned k = run > 6 ? 6 : run;
        rle_push(r, 16, 2, k - 3);
        cl_freq[16]++;
        run -= k;
      }
      while (run--) { rle_push(r, cur, 0, 0); cl_freq[cur]++; }
    }
  }
}

static uint64_t data_bits(const uint32_t *llf, const uint32_t *df, const uint8_t *ll,
                          const uint8_t *dl) {
  uint64_t b = 0;
  for (unsigned s = 0; s < 256; s++) b += (uint64_t)llf[s] * ll[s];
  b += ll[256];
  for (unsigned s = 0; s < 29; s++)
    b += (uint64_t)llf[257 + s] * (ll[257 + s] + wreath_gzip_encoder_len_ext[s]);
  for (unsigned s = 0; s < 30; s++)
    b += (uint64_t)df[s] * (dl[s] + wreath_gzip_encoder_dist_ext[s]);
  return b;
}

static void build_pack(wreath_gzip_encoder_enc *e, const uint8_t *ll, const uint16_t *llc,
                       const uint8_t *dl, const uint16_t *dlc, uint32_t *lit_pack) {
  for (unsigned s = 0; s < 257; s++) lit_pack[s] = llc[s] | ((uint32_t)ll[s] << 16);
  for (unsigned len = 3; len <= GZ_MAX_MATCH; len++) {
    unsigned ls = wreath_gzip_encoder_len_sym[len], sym = 257 + ls, nb = ll[sym];
    e->len_pack[len] = llc[sym] | ((uint32_t)(len - wreath_gzip_encoder_len_base[ls]) << nb);
    e->len_nb[len] = (uint8_t)(nb + wreath_gzip_encoder_len_ext[ls]);
  }
  for (unsigned s = 0; s < 30; s++) {
    e->d_pack[s] = dlc[s];
    e->d_nb[s] = dl[s];
    e->d_tot[s] = (uint8_t)(dl[s] + wreath_gzip_encoder_dist_ext[s]);
  }
}

/* Ablation-only builds. GZ_ABLATE_NOEMIT produces a stream that is NOT valid --
 * it is a measurement of what symbol emission costs and nothing else, and the
 * differential suite refuses to run against it. GZ_ABLATE_STATIC and
 * GZ_ABLATE_NOSEARCH both still produce correct streams, just larger ones. */
static void emit_tokens(wreath_gzip_encoder_enc *e, const wreath_gzip_encoder_item *tok, unsigned n,
                        const uint32_t *lit_pack) {
#ifdef GZ_ABLATE_NOEMIT
  (void)tok; (void)n; (void)lit_pack;
  return;
#else
  struct wreath_gzip_encoder_bw w = e->bw;
#if GZ_SEQUENCE_IR
  const uint8_t *ip = e->in;
  for (unsigned i = 0; i < n; i++) {
    uint32_t rl = tok[i].run_and_len;
    unsigned run = rl & GZ_SEQ_RUN_MASK;
    unsigned len = rl >> GZ_SEQ_LEN_SHIFT;
    while (run--) {
      uint32_t p = lit_pack[*ip++];
      bw_put(&w, p & 0xffff, p >> 16);
    }
    if (len) {
      unsigned dist = tok[i].dist;
      unsigned ds = wreath_gzip_encoder_dist_code(dist);
      uint64_t d = e->d_pack[ds] | ((uint64_t)(dist - wreath_gzip_encoder_dist_base[ds]) << e->d_nb[ds]);
      unsigned lb = e->len_nb[len];
      bw_put(&w, e->len_pack[len] | (d << lb), lb + e->d_tot[ds]);
      ip += len;
    }
  }
#else
  for (unsigned i = 0; i < n; i++) {
    uint32_t t = tok[i];
    if (t & GZ_TOK_MATCH) {
      unsigned len = GZ_TOK_LEN(t), dist = GZ_TOK_DIST(t);
      unsigned ds = wreath_gzip_encoder_dist_code(dist);
      uint64_t d = e->d_pack[ds] | ((uint64_t)(dist - wreath_gzip_encoder_dist_base[ds]) << e->d_nb[ds]);
      unsigned lb = e->len_nb[len];
      bw_put(&w, e->len_pack[len] | (d << lb), lb + e->d_tot[ds]);
    } else {
      uint32_t p = lit_pack[t];
      bw_put(&w, p & 0xffff, p >> 16);
    }
  }
#endif
  e->bw = w;
#endif
}

/* ---- short-match pricing -------------------------------------------------
 *
 * A short match at a long distance can cost more bits than the literals it
 * replaces, and a parser that maximises length alone takes it every time.
 * The question is what to charge for those literals.
 *
 * GZ_SHORTMODE 1 charges the block's *average* literal cost. That is what this
 * encoder shipped, and it is measurably wrong in one direction: the bytes an
 * accidental four-byte match covers are, by construction, ones that have just
 * occurred nearby, so they are the *cheap* literals, not average ones. Charging
 * the average over-values the match. Measured on json-0500k that error was
 * worth 9,354 four-byte matches against libdeflate's 316, and their distance
 * bits were the entire 1.23% ratio gap.
 *
 * GZ_SHORTMODE 2 charges the actual bytes: sum of this stream's own literal
 * code lengths over the len bytes the match would cover. Same tree, same
 * derivation, no new constant -- it just stops averaging over the wrong set.
 *
 * `dmax_bits[b]` inverts the distance side once per block: the largest distance
 * whose code plus extra bits costs at most b, so the parse spends one indexed
 * load instead of a distance-code lookup and a comparison chain. */
void wreath_gzip_encoder_price_short(wreath_gzip_encoder_enc *e, const uint32_t *llf, const uint8_t *ll_len,
                    const uint8_t *d_len) {
  int mode = GZ_SHORTMODE;
  if (e->active_format == GZ_FMT_JSON) mode = 2;
  else if (e->active_format == GZ_FMT_GRAPHQL || e->active_format == GZ_FMT_HTML ||
           e->active_format == GZ_FMT_PLAINTEXT || e->active_format == GZ_FMT_CHAOTIC_JSON)
    mode = 0;
  else if (e->active_format == GZ_FMT_LOG) mode = 3;

  if (mode >= 2) {
  unsigned maxl = 1;
  for (unsigned s = 0; s < 256; s++)
    if (ll_len[s] > maxl) maxl = ll_len[s];
  /* A byte this block never used has no code length. It will get one if it
   * turns up, and it will be a long one; charging one bit more than the
   * block's deepest literal is the cheapest honest guess. */
  unsigned unseen = maxl + 1 > 15 ? 15 : maxl + 1;
  for (unsigned s = 0; s < 256; s++)
    e->lit_cost[s] = ll_len[s] ? ll_len[s] : (uint8_t)unseen;
  for (unsigned l = 3; l <= GZ_SHORT_MAX; l++) {
    unsigned ls = wreath_gzip_encoder_len_sym[l];
    e->short_lcost[l] =
        (uint8_t)(ll_len[257 + ls] ? ll_len[257 + ls] + wreath_gzip_encoder_len_ext[ls] : 15 + wreath_gzip_encoder_len_ext[ls]);
    e->maxdist[l] = GZ_WINDOW;
  }
  /* dmax_bits[b] = max{ top(s) : cost(s) <= b }, built in 30 + 64 steps rather
   * than the 64x30 the definition suggests: bucket each distance code by its
   * cost, then take a running maximum over the buckets. Written the obvious way
   * this loop cost 9.4% (json) to 12.2% (graphql) of a 10 kB encode -- it runs
   * once per block, and a 10 kB body is only two or three blocks. */
  uint32_t bestat[GZ_DMAX_SLOTS];
  memset(bestat, 0, sizeof bestat);
  for (unsigned s = 0; s < 30; s++) {
    /* The margin is folded in here, once per block, so the parse pays nothing
     * for it: a distance is admissible at budget b only if it beats b by the
     * margin as well. */
    unsigned dc = (d_len[s] ? d_len[s] : 15u) + wreath_gzip_encoder_dist_ext[s] + GZ_SHORT_MARGIN;
    if (dc >= GZ_DMAX_SLOTS) continue;
    unsigned top = (unsigned)wreath_gzip_encoder_dist_base[s] + ((1u << wreath_gzip_encoder_dist_ext[s]) - 1);
    if (top > GZ_WINDOW) top = GZ_WINDOW;
    if (top > bestat[dc]) bestat[dc] = top;
  }
  uint32_t run = 0;
  for (unsigned b = 0; b < GZ_DMAX_SLOTS; b++) {
    if (bestat[b] > run) run = bestat[b];
    e->dmax_bits[b] = run;
  }
  }
  if (mode == 1 || mode == 3) {
  uint64_t litn = 0, litb = 0;
  for (unsigned s = 0; s < 256; s++) {
    litn += llf[s];
    litb += (uint64_t)llf[s] * ll_len[s];
  }
  /* Average literal cost in sixteenths of a bit, from the tree just built. */
  unsigned avg_q4 = litn ? (unsigned)((litb * 16 + litn / 2) / litn) : 16 * 8;
  for (unsigned len = 3; len <= GZ_SHORT_MAX; len++) {
    unsigned ls = wreath_gzip_encoder_len_sym[len], lsym = 257 + ls;
    unsigned lcost = ll_len[lsym] ? ll_len[lsym] + wreath_gzip_encoder_len_ext[ls] : 15;
    unsigned budget = len * avg_q4;
    unsigned md = 0;
    for (unsigned s = 0; s < 30; s++) {
      unsigned dc = d_len[s] ? d_len[s] : 15;
      if ((lcost + dc + wreath_gzip_encoder_dist_ext[s]) * 16 + GZ_SHORT_MARGIN_Q4 < budget) {
        unsigned top = (unsigned)wreath_gzip_encoder_dist_base[s] + ((1u << wreath_gzip_encoder_dist_ext[s]) - 1);
        if (top > md) md = top;
      }
    }
    e->maxdist[len] = md > GZ_WINDOW ? GZ_WINDOW : md;
  }
  }
  if (mode == 0) {
  for (unsigned l = 3; l <= GZ_SHORT_MAX; l++) e->maxdist[l] = GZ_WINDOW;
  }
}

/* Price the three block kinds exactly, in bits, and emit the cheapest.
 * `llf`/`df` must be the symbol counts for exactly tok[0..n), with the
 * end-of-block symbol already counted. */
/* Give an alphabet with fewer than two live symbols exactly two one-bit codes,
 * preserving whichever symbol is already in use. */
static void complete_pair(uint8_t *len, unsigned nsym, unsigned prefer) {
  unsigned a = nsym;
  for (unsigned i = 0; i < nsym; i++)
    if (len[i]) { a = i; break; }
  if (a == nsym) { len[0] = 1; len[prefer] = 1; return; }
  len[a] = 1;
  len[a == 0 ? 1 : 0] = 1;
}

static void emit_block(wreath_gzip_encoder_enc *e, const uint8_t *in, const wreath_gzip_encoder_item *tok, unsigned n,
                       uint32_t *llf, uint32_t *df, size_t ipos0, size_t ipos1, int final) {
  size_t raw = ipos1 - ipos0;
  int ll_max = 15;
  int d_max = 15;

  uint8_t ll_len[288], d_len[32];
  uint16_t ll_code[288], d_code[32];
  int nll = wreath_gzip_encoder_huff_lengths(llf, 286, ll_max, ll_len);
  int nd = wreath_gzip_encoder_huff_lengths(df, 30, d_max, d_len);
  if (nll < 0 || nd < 0) { e->failed = 1; return; }
  /* A code with fewer than two symbols is incomplete, and a strict inflater is
   * right to reject an incomplete table (zlib: "invalid distances set"). Two
   * one-bit codes always are complete. The symbol that *is* used has to keep
   * its code -- padding around it instead of over it is the whole point, and
   * getting that wrong is what the differential suite caught. */
  if (nll < 2) complete_pair(ll_len, 286, 256);
  if (nd < 2) complete_pair(d_len, 30, 1);
  memset(ll_len + 286, 0, 2);
  memset(d_len + 30, 0, 2);
  wreath_gzip_encoder_huff_codes(ll_len, 288, ll_code);
  wreath_gzip_encoder_huff_codes(d_len, 32, d_code);

  unsigned hlit = 286, hdist = 30;
  while (hlit > 257 && ll_len[hlit - 1] == 0) hlit--;
  while (hdist > 1 && d_len[hdist - 1] == 0) hdist--;

  uint8_t all[320];
  memcpy(all, ll_len, hlit);
  memcpy(all + hlit, d_len, hdist);
  struct rle r;
  uint32_t cl_freq[19];
  rle_lengths(all, hlit + hdist, &r, cl_freq);

  uint8_t cl_len[19];
  uint16_t cl_code[19];
  int ncl = wreath_gzip_encoder_huff_lengths(cl_freq, 19, 7, cl_len);
  if (ncl < 0) { e->failed = 1; return; }
  if (ncl < 2) complete_pair(cl_len, 19, 1);
  wreath_gzip_encoder_huff_codes(cl_len, 19, cl_code);
  unsigned hclen = 19;
  while (hclen > 4 && cl_len[cl_order[hclen - 1]] == 0) hclen--;

  uint64_t hdr_bits = 3 + 5 + 5 + 4 + 3 * (uint64_t)hclen;
  for (unsigned i = 0; i < r.n; i++) hdr_bits += cl_len[r.sym[i]] + r.xb[i];
  uint64_t dyn_bits = hdr_bits + data_bits(llf, df, ll_len, d_len);
  uint64_t sta_bits = 3 + data_bits(llf, df, s_ll_len, s_d_len);
  uint64_t sto_bits = (uint64_t)-1;
  if (raw <= 65535)
    sto_bits = 3 + ((8 - ((e->bw.cnt + 3) & 7)) & 7) + 32 + 8 * (uint64_t)raw;

  uint64_t best = dyn_bits;
  int kind = 2;
  if (sta_bits < best) { best = sta_bits; kind = 1; }
  if (sto_bits < best) { best = sto_bits; kind = 0; }
#ifdef GZ_ABLATE_STATIC
  best = sta_bits;
  kind = 1; /* prices every bit of dynamic tree work, as arm 3's L10 did */
#endif

  /* Exact capacity check, before a single bit is written. The two constants
   * here mean exactly one thing each and do not overlap: `out_end` is the true
   * end of the caller's buffer, and the `+ 8` is the branchless writer's
   * unconditional eight-byte store at the block's last position. Arm 1's
   * warning is why: two guards that shadow each other are indistinguishable
   * from one guard and one bug, and the falsification probes could not tell
   * them apart until the double counting came out. */
  size_t need = (size_t)((best + 7) / 8) + 8;
  if (e->bw.out > e->out_end || (size_t)(e->out_end - e->bw.out) < need) {
    e->failed = 1;
    return;
  }
  if (kind == 0) {
    bw_put(&e->bw, (uint64_t)(unsigned)final, 3);
    if (e->bw.cnt) bw_put(&e->bw, 0, 8 - e->bw.cnt);
    bw_put(&e->bw, (uint64_t)(raw & 0xffff), 16);
    bw_put(&e->bw, (uint64_t)((~raw) & 0xffff), 16);
    memcpy(e->bw.out, in + ipos0, raw);
    e->bw.out += raw;
    return;
  }

  uint32_t lit_pack[257];
  if (kind == 1) {
    bw_put(&e->bw, (uint64_t)(unsigned)final | 2, 3);
    build_pack(e, s_ll_len, s_ll_code, s_d_len, s_d_code, lit_pack);
    e->in = in + ipos0;
    emit_tokens(e, tok, n, lit_pack);
    bw_put(&e->bw, s_ll_code[256], s_ll_len[256]);
    return;
  }

  bw_put(&e->bw, (uint64_t)(unsigned)final | 4, 3);
  bw_put(&e->bw,
         (uint64_t)(hlit - 257) | ((uint64_t)(hdist - 1) << 5) |
             ((uint64_t)(hclen - 4) << 10),
         14);
  for (unsigned i = 0; i < hclen; i++) bw_put(&e->bw, cl_len[cl_order[i]], 3);
  for (unsigned i = 0; i < r.n; i++) {
    unsigned s = r.sym[i];
    bw_put(&e->bw, (uint64_t)cl_code[s] | ((uint64_t)r.xv[i] << cl_len[s]),
           cl_len[s] + r.xb[i]);
  }
  build_pack(e, ll_len, ll_code, d_len, d_code, lit_pack);
    e->in = in + ipos0;
    emit_tokens(e, tok, n, lit_pack);
  bw_put(&e->bw, ll_code[256], ll_len[256]);

  wreath_gzip_encoder_price_short(e, llf, ll_len, d_len);
}

/* Code lengths from a histogram's own entropy: round(log2(total/f)), clamped
 * to what a DEFLATE code length can express. The short-match model wants
 * *costs*, not codes, so this is the same answer as a Huffman build to within
 * a bit for 2,500 instructions instead of 25,000 -- which on a 10 kB body was
 * 4 to 6 percent of the whole encode. */
static void entropy_lengths(const uint32_t *f, unsigned n, uint8_t *len) {
  uint64_t tot = 0;
  for (unsigned s = 0; s < n; s++) tot += f[s];
  if (!tot) {
    memset(len, 0, n);
    return;
  }
  uint32_t lt = lg_q16((uint32_t)tot);
  for (unsigned s = 0; s < n; s++) {
    if (!f[s]) { len[s] = 0; continue; }
    /* log2(tot) - log2(f), rounded to the nearest bit and clamped to what a
     * DEFLATE code length can express. */
    int32_t q = (int32_t)lt - (int32_t)lg_q16(f[s]);
    int b = (q + (1 << 15)) >> 16;
    len[s] = (uint8_t)(b < 1 ? 1 : b > 15 ? 15 : b);
  }
}

/* ---- adaptive block splitting -------------------------------------------- */

/* Entropy cost, in Q16 bits, of coding the symbols in `f` with their own
 * optimal code: sum f_i log2(T/f_i). A real Huffman code is at most about a
 * bit per symbol worse and the excess is common to both sides of the
 * comparison, so the ordering is what matters, not the absolute value. */
struct costs {
  uint64_t block, prefix, chunk;
  unsigned nz_chunk;
};

static void split_costs(const uint32_t *bf, const uint32_t *cf, unsigned n,
                        struct costs *c) {
  uint64_t tb = 0, tp = 0, tc = 0, sb = 0, sp = 0, sc = 0;
  unsigned nz = 0;
  for (unsigned i = 0; i < n; i++) {
    uint32_t b = bf[i], ch = cf[i], p = b - ch;
    if (b) { tb += b; sb += (uint64_t)b * lg_q16(b); }
    if (p) { tp += p; sp += (uint64_t)p * lg_q16(p); }
    if (ch) { tc += ch; sc += (uint64_t)ch * lg_q16(ch); nz++; }
  }
  c->block += tb ? tb * lg_q16((uint32_t)tb) - sb : 0;
  c->prefix += tp ? tp * lg_q16((uint32_t)tp) - sp : 0;
  c->chunk += tc ? tc * lg_q16((uint32_t)tc) - sc : 0;
  c->nz_chunk += nz;
}

/* Rebuild the chunk's symbol counts from its tokens. Doing it here rather than
 * keeping a second live histogram keeps the parse loop's per-token cost
 * unchanged -- arm 2 measured the histogram at ~6% of a cheap encode, and a
 * second one would have doubled that for a decision taken once per 4096
 * tokens. */
#if GZ_DEFER_HIST
#if GZ_SEQUENCE_IR
static size_t count_sequences(const uint8_t *in, const struct wreath_gzip_encoder_seq *seq,
                              unsigned first, unsigned last, size_t pos,
                              uint32_t *ll, uint32_t *dd) {
  for (unsigned i = first; i < last; i++) {
    uint32_t rl = seq[i].run_and_len;
    unsigned run = rl & GZ_SEQ_RUN_MASK;
    unsigned len = rl >> GZ_SEQ_LEN_SHIFT;
    size_t end = pos + run;
    while (pos < end) ll[in[pos++]]++;
    if (len) {
      ll[257 + wreath_gzip_encoder_len_sym[len]]++;
      dd[wreath_gzip_encoder_dist_code(seq[i].dist)]++;
      pos += len;
    }
  }
  return pos;
}

static size_t count_sequences_both(const uint8_t *in, const struct wreath_gzip_encoder_seq *seq,
                                   unsigned first, unsigned last, size_t pos,
                                   uint32_t *ll, uint32_t *dd,
                                   uint32_t *cl, uint32_t *cd) {
  for (unsigned i = first; i < last; i++) {
    uint32_t rl = seq[i].run_and_len;
    unsigned run = rl & GZ_SEQ_RUN_MASK;
    unsigned len = rl >> GZ_SEQ_LEN_SHIFT;
    size_t end = pos + run;
    while (pos < end) {
      unsigned b = in[pos++];
      ll[b]++;
      cl[b]++;
    }
    if (len) {
      unsigned ls = 257 + wreath_gzip_encoder_len_sym[len];
      unsigned ds = wreath_gzip_encoder_dist_code(seq[i].dist);
      ll[ls]++;
      dd[ds]++;
      cl[ls]++;
      cd[ds]++;
      pos += len;
    }
  }
  return pos;
}

static void sync_histograms(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t pos,
                            int need_chunk) {
  wreath_gzip_encoder_seal_literals(e);
  if (need_chunk && e->hist_seq == e->ck_seq) {
    memset(e->ck_ll, 0, sizeof e->ck_ll);
    memset(e->ck_d, 0, sizeof e->ck_d);
    e->hist_pos = count_sequences_both(in, e->tokens, e->hist_seq, e->nseq,
                                       e->hist_pos, e->ll_freq, e->d_freq,
                                       e->ck_ll, e->ck_d);
    e->hist_seq = e->nseq;
    return;
  }
  e->hist_pos = count_sequences(in, e->tokens, e->hist_seq, e->nseq,
                                e->hist_pos, e->ll_freq, e->d_freq);
  e->hist_seq = e->nseq;
  if (!need_chunk) return;
  memset(e->ck_ll, 0, sizeof e->ck_ll);
  memset(e->ck_d, 0, sizeof e->ck_d);
  (void)count_sequences(in, e->tokens, e->ck_seq, e->nseq, e->ck_pos,
                        e->ck_ll, e->ck_d);
  (void)pos;
}
#else
static void count_tokens(const uint32_t *tok, unsigned first, unsigned last,
                         uint32_t *ll, uint32_t *dd) {
  for (unsigned i = first; i < last; i++) {
    uint32_t t = tok[i];
    if (t & GZ_TOK_MATCH) {
      ll[257 + wreath_gzip_encoder_len_sym[GZ_TOK_LEN(t)]]++;
      dd[wreath_gzip_encoder_dist_code(GZ_TOK_DIST(t))]++;
    } else {
      ll[t]++;
    }
  }
}

/* Synchronise token histograms at the phase boundary instead of mutating them
 * in the hash-chain loop.  Parsing then touches only input, heads, links and
 * the sequential token stream; the small frequency arrays are updated in a
 * dense pass while the tokens are still hot.  When the split chunk begins at
 * the same token as the unsynchronised suffix (the steady state), one token
 * walk feeds both histograms. */
static void sync_histograms(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t pos,
                            int need_chunk) {
  (void)in;
  (void)pos;
  if (need_chunk && e->hist_tok == e->ck_tok) {
    memset(e->ck_ll, 0, sizeof e->ck_ll);
    memset(e->ck_d, 0, sizeof e->ck_d);
    const uint32_t *tok = e->tokens;
    for (unsigned i = e->hist_tok; i < e->ntok; i++) {
      uint32_t t = tok[i];
      if (t & GZ_TOK_MATCH) {
        unsigned ls = 257 + wreath_gzip_encoder_len_sym[GZ_TOK_LEN(t)];
        unsigned ds = wreath_gzip_encoder_dist_code(GZ_TOK_DIST(t));
        e->ll_freq[ls]++;
        e->d_freq[ds]++;
        e->ck_ll[ls]++;
        e->ck_d[ds]++;
      } else {
        e->ll_freq[t]++;
        e->ck_ll[t]++;
      }
    }
    e->hist_tok = e->ntok;
    return;
  }
  count_tokens(e->tokens, e->hist_tok, e->ntok, e->ll_freq, e->d_freq);
  e->hist_tok = e->ntok;
  if (!need_chunk) return;
  memset(e->ck_ll, 0, sizeof e->ck_ll);
  memset(e->ck_d, 0, sizeof e->ck_d);
  count_tokens(e->tokens, e->ck_tok, e->ntok, e->ck_ll, e->ck_d);
}
#endif
#else
static void chunk_hist(wreath_gzip_encoder_enc *e) {
  memset(e->ck_ll, 0, sizeof e->ck_ll);
  memset(e->ck_d, 0, sizeof e->ck_d);
  const uint32_t *tok = e->tokens;
  for (unsigned i = e->ck_tok; i < e->ntok; i++) {
    uint32_t t = tok[i];
    if (t & GZ_TOK_MATCH) {
      e->ck_ll[257 + wreath_gzip_encoder_len_sym[GZ_TOK_LEN(t)]]++;
      e->ck_d[wreath_gzip_encoder_dist_code(GZ_TOK_DIST(t))]++;
    } else {
      e->ck_ll[t]++;
    }
  }
}
#endif

static int should_split(wreath_gzip_encoder_enc *e) {
#if !GZ_DEFER_HIST
  chunk_hist(e);
#endif
  struct costs c = {0, 0, 0, 0};
  split_costs(e->ll_freq, e->ck_ll, 286, &c);
  split_costs(e->d_freq, e->ck_d, 30, &c);
  /* One extra dynamic header: 14 bits of counts, the code-length alphabet's own
   * tree, and the run-length-coded lengths themselves. Four bits per live
   * symbol is what the code-length tree costs in practice; the zeros between
   * them are nearly free under symbols 17/18. Plus one end-of-block. */
  uint64_t hdr_q16 = ((uint64_t)(GZ_SPLIT_HDR_FIX + GZ_SPLIT_HDR_PER * c.nz_chunk)) << 16;
  return c.prefix + c.chunk + hdr_q16 < c.block;
}

/* Emit tokens [0,ntok) as one or two blocks and reset the block state. */
void wreath_gzip_encoder_defl_flush_block(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t end, int final) {
#if GZ_DEFER_HIST
  sync_histograms(e, in, end, e->split_now);
#endif
  if (e->split_now && e->ck_tok > 0 && e->ck_tok < e->ntok && should_split(e)) {
    uint32_t pll[288], pd[32];
    for (unsigned i = 0; i < 288; i++) pll[i] = e->ll_freq[i] - e->ck_ll[i];
    for (unsigned i = 0; i < 32; i++) pd[i] = e->d_freq[i] - e->ck_d[i];
    pll[256]++;
#if GZ_SEQUENCE_IR
    emit_block(e, in, e->tokens, e->ck_seq, pll, pd, e->block_start, e->ck_pos, 0);
#else
    emit_block(e, in, e->tokens, e->ck_tok, pll, pd, e->block_start, e->ck_pos, 0);
#endif
    if (e->failed) goto reset;
    e->ck_ll[256]++;
#if GZ_SEQUENCE_IR
    emit_block(e, in, e->tokens + e->ck_seq, e->nseq - e->ck_seq, e->ck_ll, e->ck_d,
               e->ck_pos, end, final);
#else
    emit_block(e, in, e->tokens + e->ck_tok, e->ntok - e->ck_tok, e->ck_ll, e->ck_d,
               e->ck_pos, end, final);
#endif
    goto reset;
  }
  e->ll_freq[256]++;
#if GZ_SEQUENCE_IR
  emit_block(e, in, e->tokens, e->nseq, e->ll_freq, e->d_freq, e->block_start, end, final);
#else
  emit_block(e, in, e->tokens, e->ntok, e->ll_freq, e->d_freq, e->block_start, end, final);
#endif

reset:
  e->ntok = 0;
#if GZ_SEQUENCE_IR
  e->nseq = 0;
  e->ck_seq = 0;
  e->hist_seq = 0;
  e->litrun = 0;
  e->hist_pos = end;
#endif
  e->ck_tok = 0;
  e->hist_tok = 0;
  e->ck_pos = end;
  e->block_start = end;
  e->gate = GZ_CHUNK_TOK;
  memset(e->ll_freq, 0, sizeof e->ll_freq);
  memset(e->d_freq, 0, sizeof e->d_freq);
}

void wreath_gzip_encoder_defl_gate(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t pos) {
#if GZ_DEFER_HIST
  sync_histograms(e, in, pos, e->split_now || e->warmup);
#endif
  if (e->ntok >= GZ_TOKENS_MAX) {
    wreath_gzip_encoder_defl_flush_block(e, in, pos, 0);
    return;
  }
  /* Until a block has been emitted the short-match cost model is whatever the
   * last one left behind -- for the first block of a stream, the triage sample
   * or RFC 1951's fixed table. A checkpoint is a free place to correct it: the
   * whole-block histogram is already live, and re-pricing costs one pass over
   * 316 symbols and no header at all.
   *
   * GZ_REPRICE 1 does it at the first checkpoint only, which is where the model
   * is worst; 2 does it at every checkpoint. Forcing a block *boundary* here
   * instead (GZ_WARM_BLOCK) buys the same correction and pays a dynamic header
   * for it, which is why it is off: it wins on 500 kB bodies and loses on
   * 30 kB ones, and re-pricing wins on both. */
#if GZ_REPRICE
  if (e->reprice && e->ntok) {
#if GZ_REPRICE == 1
    e->reprice = 0;
#endif
    uint8_t sll[288], sdl[32];
    entropy_lengths(e->ll_freq, 286, sll);
    entropy_lengths(e->d_freq, 30, sdl);
    wreath_gzip_encoder_price_short(e, e->ll_freq, sll, sdl);
  }
#endif
  /* The warm split buys a corrected model for the input that is still to come,
   * and pays one dynamic header for it. On a body with no tail left there is
   * nothing to buy: measured at 30 kB it cost 2.2% of the encode for two bytes,
   * and at 500 kB it was cheaper *and* smaller. So it fires only when the
   * remaining input is a multiple of what the first chunk consumed. */
  int warm = e->warmup && e->ck_tok > 0 &&
             (uint64_t)(e->n_in - pos) >=
                 (uint64_t)GZ_WARM_MIN_TAIL * (pos - e->block_start);
  if (warm) e->warmup = 0;
  if (e->split_now && e->ck_tok > 0 && (warm || should_split(e))) {
#if !GZ_DEFER_HIST
    if (warm) chunk_hist(e);
#endif
    uint32_t pll[288], pd[32];
    for (unsigned i = 0; i < 288; i++) pll[i] = e->ll_freq[i] - e->ck_ll[i];
    for (unsigned i = 0; i < 32; i++) pd[i] = e->d_freq[i] - e->ck_d[i];
    pll[256]++;
#if GZ_SEQUENCE_IR
    emit_block(e, in, e->tokens, e->ck_seq, pll, pd, e->block_start, e->ck_pos, 0);
#else
    emit_block(e, in, e->tokens, e->ck_tok, pll, pd, e->block_start, e->ck_pos, 0);
#endif
    unsigned keep = e->ntok - e->ck_tok;
#if GZ_SEQUENCE_IR
    unsigned keep_seq = e->nseq - e->ck_seq;
    memmove(e->tokens, e->tokens + e->ck_seq, (size_t)keep_seq * sizeof *e->tokens);
    e->nseq = keep_seq;
    e->hist_seq = keep_seq;
    e->hist_pos = pos;
#else
    memmove(e->tokens, e->tokens + e->ck_tok, (size_t)keep * sizeof *e->tokens);
#endif
    e->ntok = keep;
    memcpy(e->ll_freq, e->ck_ll, sizeof e->ll_freq);
    memcpy(e->d_freq, e->ck_d, sizeof e->d_freq);
    e->block_start = e->ck_pos;
#if GZ_DEFER_HIST
#if GZ_SEQUENCE_IR
    /* All retained sequences are already represented by the retained
     * histograms copied above. */
#else
    e->hist_tok = keep;
#endif
#endif
  }
  if (e->failed) return;
  e->ck_tok = e->ntok;
#if GZ_SEQUENCE_IR
  e->ck_seq = e->nseq;
#endif
  e->ck_pos = pos;
  e->gate = e->ntok + GZ_CHUNK_TOK;
  if (e->gate > GZ_TOKENS_MAX) e->gate = GZ_TOKENS_MAX;
}

/* ---- public encoder ------------------------------------------------------ */

const char *wreath_gzip_encoder_encode_arm_name(void) { return "scalar"; }

wreath_gzip_encoder_enc *wreath_gzip_encoder_enc_new(void) {
  wreath_gzip_encoder_enc *e = calloc(1, sizeof *e);
  if (!e) return NULL;
  e->crc_arm = wreath_gzip_encoder_crc32_pick_arm();
  /* glibc returns every one of these from mmap, so head, prev and tokens all
   * start page-aligned and therefore all start at L1 set 0. GZ_SKEW_LINES
   * pushes `head` off that alignment by whole cache lines, so that its random
   * probes and the sequential window scan stop indexing the same sets in step.
   * Swept in RESULTS.md. The allocation is oversized by the skew and the
   * original pointer is kept for free(). */
  e->head_alloc = malloc(((size_t)1 << GZ_HASH_BITS_MAX) * sizeof *e->head +
                         (size_t)GZ_SKEW_LINES * 64);
  e->head = e->head_alloc
                ? (uint32_t *)((uint8_t *)e->head_alloc + (size_t)GZ_SKEW_LINES * 64)
                : NULL;
  e->head3 = malloc(((size_t)1 << GZ_HASH3_BITS) * sizeof *e->head3);
  e->tokens = malloc((size_t)GZ_TOKENS_MAX * sizeof *e->tokens);
  if (!e->head || !e->head3 || !e->tokens) { wreath_gzip_encoder_enc_free(e); return NULL; }
  /* Rebasing means `head` is never cleared again after this one memset. A
   * stale entry is always more than a window away from the current position and
   * is rejected by the bound test the loop already had to do. */
  memset(e->head, 0, ((size_t)1 << GZ_HASH_BITS_MAX) * sizeof *e->head);
  memset(e->head3, 0, ((size_t)1 << GZ_HASH3_BITS) * sizeof *e->head3);
  e->base = GZ_WINDOW + 1;
  e->format = GZ_FMT_UNKNOWN;
  return e;
}

void wreath_gzip_encoder_enc_free(wreath_gzip_encoder_enc *e) {
  if (!e) return;
  free(e->head_alloc);
  free(e->head3);
  free(e->prev);
  free(e->tokens);
  free(e);
}

void wreath_gzip_encoder_enc_set_format(wreath_gzip_encoder_enc *e, int format) {
  e->format = format >= 0 && format < GZ_FMT_COUNT ? format : GZ_FMT_UNKNOWN;
}

size_t wreath_gzip_encoder_encode_bound(size_t n) {
  /* Every block that cannot be compressed is emitted stored, so the floor is
   * the input plus framing and up to seven bits of alignment per block. The
   * extra 1/16 is headroom for blocks whose raw span exceeded the 65535 stored
   * limit and so had to take the Huffman path; emit_block checks each block's
   * exact bit cost against the remaining capacity before writing anything, so a
   * bound that were ever too small fails loudly rather than overrunning. */
  return n + n / 16 + (n / 4096 + 1) * 8 + 512;
}

static unsigned pick_hash_bits(const uint8_t *in, size_t n) {
  unsigned b = GZ_HASH_BITS_MIN;
  while (b < GZ_HASH_BITS_MAX && ((size_t)1 << b) < n) b++;
#if GZ_ADAPT_HASH_CONTENT
  /* A 256-bit Bloom sketch of 128 evenly spaced four-byte words.  Only very
   * repetitive input shrinks: random collisions leave about 101 bits set, so
   * the default threshold is deliberately well below that. */
  if (b > GZ_HASH_BITS_MIN && n >= 512) {
    uint64_t seen[4] = {0, 0, 0, 0};
    for (unsigned i = 0; i < 128; i++) {
      size_t p = ((n - 4) * i) / 127;
      uint32_t h = wreath_gzip_encoder_ld32(in + p) * 0x9E3779B1u;
      unsigned k = h >> 24;
      seen[k >> 6] |= (uint64_t)1 << (k & 63);
    }
    unsigned distinct = (unsigned)__builtin_popcountll(seen[0]) +
                        (unsigned)__builtin_popcountll(seen[1]) +
                        (unsigned)__builtin_popcountll(seen[2]) +
                        (unsigned)__builtin_popcountll(seen[3]);
    if (distinct < GZ_ADAPT_HASH_DISTINCT) b--;
  }
#else
  (void)in;
#endif
  return b;
}

#if GZ_ADAPT_SEARCH
static int sample_wants_light(const wreath_gzip_encoder_enc *e, size_t span) {
  uint64_t literals = 0, matches = 0;
  for (unsigned s = 0; s < 256; s++) literals += e->ll_freq[s];
  for (unsigned s = 257; s < 286; s++) matches += e->ll_freq[s];
  uint64_t matched = span > literals ? span - literals : 0;
  return matches && matched * 100 >= span * GZ_ADAPT_COVER_PCT &&
         matched >= matches * GZ_ADAPT_AVG_MATCH;
}

/* Deep search has stopped paying when even the matches it did find average
 * less than one eight-byte extension step.  Finish mixed HTML with one
 * verified probe per position; the format-specific loop is chosen once here,
 * so the match finder itself gains no policy branch. */
static int sample_wants_shallow_html(const wreath_gzip_encoder_enc *e, size_t span) {
  uint64_t literals = 0, matches = 0;
  for (unsigned s = 0; s < 256; s++) literals += e->ll_freq[s];
  for (unsigned s = 257; s < 286; s++) matches += e->ll_freq[s];
  uint64_t matched = span > literals ? span - literals : 0;
  return matches && matched < matches * 8;
}
#endif

#if GZ_ADAPT_SPARSE_SEARCH
static int raw_wants_sparse(const uint8_t *in, size_t n) {
  if (n < GZ_TRIAGE_SPAN) return 0;
  uint16_t freq[256] = {0};
  for (unsigned i = 0; i < 256; i++) freq[in[i * (GZ_TRIAGE_SPAN / 256)]]++;
  unsigned sq = 0;
  for (unsigned i = 0; i < 256; i++) sq += (unsigned)freq[i] * freq[i];
  return sq < GZ_ADAPT_SPARSE_RAW_IC * 256u;
}

static int sample_wants_sparse(const wreath_gzip_encoder_enc *e, size_t span) {
  uint64_t literals = 0;
  for (unsigned s = 0; s < 256; s++) literals += e->ll_freq[s];
  return literals * 100 >= span * GZ_ADAPT_SPARSE_LITERAL_PCT;
}
#endif

/* Two questions, both answered from counters the parse produced anyway.
 *
 * 1. Is LZ77 paying? If matches cover less than a sixteenth of the sample,
 *    there is nothing for the window to exploit.
 * 2. Is entropy coding paying? sum(f_i^2) over the literal histogram is the
 *    index of coincidence; for bytes uniform over 256 it equals tot^2/256.
 *    Requiring a one-eighth margin above that corresponds to a Renyi-2 entropy
 *    of about 7.83 bits per byte, i.e. at most ~2% left for Huffman to win.
 *
 * Both are needed. Arm 2 shipped only the first, and it emitted a stored block
 * for a body with 5.46 bits/byte of order-0 entropy that the same encoder takes
 * to ratio 0.693 -- a heuristic that cost 31% of the body and announced
 * nothing. Neither constant is fitted to a corpus: "a sixteenth" is the LZ77
 * question, and the other falls out of the uniform distribution itself. */
static int triage_says_stored(const wreath_gzip_encoder_enc *e, size_t span) {
  uint64_t tot = 0, sq = 0;
  for (unsigned s = 0; s < 256; s++) {
    uint64_t f = e->ll_freq[s];
    tot += f;
    sq += f * f;
  }
  if (span - tot >= span / 16) return 0;
  return sq * 256 * 8 < tot * tot * 9;
}

#if GZ_SEED_MODEL
/* Re-price short matches off the sample the triage pass just produced.
 *
 * Before this, the cost model for everything up to the first block boundary was
 * RFC 1951's fixed table, which is optimistic about matches by about three bits
 * a literal on this kind of data -- so a body small enough to be one or two
 * blocks was parsed almost entirely under the wrong model. That showed up as
 * the 30 kB row being the worst size on four of five shapes.
 *
 * What the model wants is *costs*, not codes, so this takes them from the
 * sample's own entropy (round(log2(total/f))) rather than building two real
 * Huffman trees. Same answer to within a bit, and measurably cheaper: swapping
 * the Huffman build for this was worth 1.0% (plaintext) to 2.2% (graphql) of a
 * whole 10 kB encode. GZ_SEED_MODEL 2 selects the Huffman build, so the two are
 * an A/B. */

static void seed_short_model(wreath_gzip_encoder_enc *e) {
  uint8_t sll[288], sdl[32];
#if GZ_SEED_MODEL == 2
  if (wreath_gzip_encoder_huff_lengths(e->ll_freq, 286, 15, sll) < 1) return;
  if (wreath_gzip_encoder_huff_lengths(e->d_freq, 30, 15, sdl) < 0) return;
#else
  entropy_lengths(e->ll_freq, 286, sll);
  entropy_lengths(e->d_freq, 30, sdl);
#endif
  wreath_gzip_encoder_price_short(e, e->ll_freq, sll, sdl);
}
#endif

static void emit_stored_run(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t from, size_t to) {
  for (;;) {
    size_t raw = to - from;
    int final = raw <= 65535;
    if (!final) raw = 65535;
    if (e->bw.out > e->out_end || (size_t)(e->out_end - e->bw.out) < raw + 16 + 8) {
      e->failed = 1;
      return;
    }
    bw_put(&e->bw, (uint64_t)(unsigned)final, 3);
    if (e->bw.cnt) bw_put(&e->bw, 0, 8 - e->bw.cnt);
    bw_put(&e->bw, (uint64_t)(raw & 0xffff), 16);
    bw_put(&e->bw, (uint64_t)((~raw) & 0xffff), 16);
    memcpy(e->bw.out, in + from, raw);
    e->bw.out += raw;
    from += raw;
    if (final) return;
  }
}

size_t wreath_gzip_encoder_encode(wreath_gzip_encoder_enc *e, const void *inv, size_t n, void *outv, size_t cap, int profile) {
  const uint8_t *in = (const uint8_t *)inv;
  uint8_t *out = (uint8_t *)outv;
  if (profile < 0) profile = 0;
  if (profile >= GZ_P_COUNT) profile = GZ_P_COUNT - 1;
  e->active_format = profile == GZ_P_DEFAULT ? e->format : GZ_FMT_UNKNOWN;
  const struct wreath_gzip_encoder_prof *P = &wreath_gzip_encoder_profiles[profile];
  if (cap < 18 + 16) return 0;
  if (n > 0x7fffff00u) return 0; /* chain links are 32-bit positions */

#if GZ_PREV_RING_BITS
  /* The ring is a fixed 2x window and is allocated once, at the first chained
   * call; nothing about it depends on `n`. */
  if (P->parse >= GZ_PARSE_CHAIN && !e->prev) {
    e->prev = malloc(GZ_PREV_SLOTS * sizeof *e->prev);
    if (!e->prev) return 0;
    e->prev_cap = GZ_PREV_SLOTS;
  }
#else
  if (P->parse >= GZ_PARSE_CHAIN && n > e->prev_cap) {
    free(e->prev);
    size_t want = n + 16;
    e->prev = malloc(want * sizeof *e->prev);
    if (!e->prev) { e->prev_cap = 0; return 0; }
    e->prev_cap = want;
  }
#endif
  e->hash_bits = pick_hash_bits(in, n);
  /* A 17-bit uint32_t head is 512 KiB instead of 1 MiB.  With the known
   * structured-text parsers' shallower probe budgets this removes cold LLC
   * walks without giving the collision work back as extra instructions.
   * Chaotic JSON already uses its own 12-bit sparse table. */
  if ((e->active_format == GZ_FMT_JSON || e->active_format == GZ_FMT_GRAPHQL ||
       e->active_format == GZ_FMT_HTML || e->active_format == GZ_FMT_PLAINTEXT ||
       e->active_format == GZ_FMT_LOG) &&
      e->hash_bits > GZ_FORMAT_HASH_BITS_MAX)
    e->hash_bits = GZ_FORMAT_HASH_BITS_MAX;
  e->hash_size = 1u << e->hash_bits;

  /* Advance the rebase past anything the last call could have written. When it
   * would wrap, and only then, clear the tables. */
  if (e->base > 0xffffffffu - (uint32_t)(n + GZ_WINDOW + 2)) {
    memset(e->head, 0, ((size_t)1 << GZ_HASH_BITS_MAX) * sizeof *e->head);
    memset(e->head3, 0, ((size_t)1 << GZ_HASH3_BITS) * sizeof *e->head3);
    e->base = GZ_WINDOW + 1;
  }

  out[0] = 0x1f;
  out[1] = 0x8b;
  out[2] = 8;
  out[3] = 0;
  out[4] = out[5] = out[6] = out[7] = 0;
  out[8] = profile >= GZ_P_MAX ? 2 : profile <= GZ_P_QUICK ? 4 : 0;
  out[9] = 3;

  e->bw.out = out + 10;
  e->bw.acc = 0;
  e->bw.cnt = 0;
  e->out_end = out + cap; /* the true end; per-block checks carry the slack */
  e->failed = 0;
  e->ntok = 0;
#if GZ_SEQUENCE_IR
  e->nseq = 0;
  e->ck_seq = 0;
  e->hist_seq = 0;
  e->litrun = 0;
  e->hist_pos = 0;
#endif
  e->ck_tok = 0;
  e->hist_tok = 0;
  e->ck_pos = 0;
  e->gate = GZ_CHUNK_TOK;
  e->warmup = GZ_WARM_BLOCK;
  e->n_in = n;
  e->reprice = 1;
  e->block_start = 0;
  /* Until the first block's tree prices them, use the fixed Huffman table of
   * RFC 1951 3.2.6 as the cost model. It is the only tree available before any
   * data has been coded, and it is a real one rather than an invented constant.
   * It is also optimistic about matches, which is why the triage sample below
   * replaces it as soon as there is a histogram to replace it with. */
  {
    uint32_t zero_llf[288] = {0};
    wreath_gzip_encoder_price_short(e, zero_llf, s_ll_len, s_d_len);
  }
  memset(e->ll_freq, 0, sizeof e->ll_freq);
  memset(e->d_freq, 0, sizeof e->d_freq);

  e->split_now = P->split;
  wreath_gzip_encoder_parse_fn parse = wreath_gzip_encoder_spec_parse[profile];
  if (profile == GZ_P_DEFAULT) {
#if GZ_HASH_SPECIALIZE_SMALL
    static const wreath_gzip_encoder_parse_fn by_hash[5] = {
        wreath_gzip_encoder_parse_deflt14, wreath_gzip_encoder_parse_deflt15, wreath_gzip_encoder_parse_deflt16,
        wreath_gzip_encoder_parse_deflt17, wreath_gzip_encoder_parse_deflt18};
    if (e->hash_bits >= 14 && e->hash_bits <= 18)
      parse = by_hash[e->hash_bits - 14];
#else
    if (e->hash_bits == 18) parse = wreath_gzip_encoder_parse_deflt18;
#endif
  }
#if GZ_HASH_SPECIALIZE_PROFILES
  if (profile != GZ_P_DEFAULT && e->hash_bits == 18)
    parse = wreath_gzip_encoder_spec_parse18[profile];
#endif
  if (profile == GZ_P_DEFAULT && e->hash_bits >= 14 && e->hash_bits <= 18) {
    unsigned fi = e->hash_bits - 14;
    if (e->format == GZ_FMT_JSON) parse = wreath_gzip_encoder_fmt_json[fi];
    else if (e->format == GZ_FMT_GRAPHQL) parse = wreath_gzip_encoder_fmt_graphql[fi];
    else if (e->format == GZ_FMT_HTML) parse = wreath_gzip_encoder_fmt_text[fi];
    else if (e->format == GZ_FMT_PLAINTEXT) parse = wreath_gzip_encoder_fmt_plaintext[fi];
    else if (e->format == GZ_FMT_LOG) parse = wreath_gzip_encoder_fmt_log[fi];
    else if (e->format == GZ_FMT_CHAOTIC_JSON) parse = wreath_gzip_encoder_parse_sparse;
  }
  size_t pos = 0;
  int stored = 0;
#if GZ_ADAPT_SPARSE_SEARCH
  int pre_sparse = profile == GZ_P_DEFAULT &&
                   (e->format == GZ_FMT_CHAOTIC_JSON ||
                    (e->format == GZ_FMT_UNKNOWN && e->hash_bits == 18 &&
                     raw_wants_sparse(in, n)));
  if (pre_sparse) parse = wreath_gzip_encoder_parse_sparse;
#endif

  if (P->triage && n >= GZ_TRIAGE_SPAN) {
    /* The first segment is a separate *call*, not a chunked loop: arm 2
     * measured that chunking the inner loop costs 3.1% even when the check
     * itself is free, because of the code the extra nesting generates. */
    e->gate = GZ_TOKENS_MAX; /* no split checkpoint inside the sample */
    pos = parse(e, in, 0, GZ_TRIAGE_SPAN, n, P);
#if GZ_DEFER_HIST
    sync_histograms(e, in, pos, 0);
#endif
    if (triage_says_stored(e, pos)) stored = 1;
#if GZ_SEED_MODEL
    if (!stored) seed_short_model(e);
#endif
#if GZ_ADAPT_SEARCH
    /* The large-input default loop and light loop are both specialised for an
     * 18-bit hash.  Switch only after the sample has demonstrated that matches
     * are both common and long; easy repetition does not need 48 probes. */
    if (!stored && profile == GZ_P_DEFAULT &&
        e->format == GZ_FMT_UNKNOWN && e->hash_bits == 18 &&
        sample_wants_light(e, pos))
      parse = wreath_gzip_encoder_parse_adapt18;
    if (!stored && profile == GZ_P_DEFAULT &&
        e->format == GZ_FMT_HTML && e->hash_bits >= 14 && e->hash_bits <= 18 &&
        sample_wants_shallow_html(e, pos))
      parse = wreath_gzip_encoder_fmt_text_shallow[e->hash_bits - 14];
#endif
#if GZ_ADAPT_SPARSE_SEARCH
    /* Do not pay the 1 MiB head-table and chain-link working set after a sample
     * in which three quarters of the bytes survived as literals.  Fully
     * incompressible input was already caught by the stored-block test; this
     * arm is for structured framing around high-entropy fields. */
    if (!stored && profile == GZ_P_DEFAULT &&
        (e->format == GZ_FMT_CHAOTIC_JSON ||
         (e->format == GZ_FMT_UNKNOWN && e->hash_bits == 18 &&
          sample_wants_sparse(e, pos))))
      parse = wreath_gzip_encoder_parse_sparse;
#endif
    e->gate = e->ntok + GZ_CHUNK_TOK;
    if (e->gate > GZ_TOKENS_MAX) e->gate = GZ_TOKENS_MAX;
  }

  if (stored) {
    e->ntok = 0;
#if GZ_SEQUENCE_IR
    e->nseq = 0;
    e->litrun = 0;
#endif
    emit_stored_run(e, in, 0, n);
  } else {
    pos = parse(e, in, pos, n, n, P);
    wreath_gzip_encoder_defl_flush_block(e, in, n, 1);
  }
  e->base += (uint32_t)(n + GZ_WINDOW + 2);
  if (e->failed) return 0;

  if (e->bw.cnt) bw_put(&e->bw, 0, 8 - e->bw.cnt);
  uint8_t *p = e->bw.out;
  if ((size_t)(p + 8 - out) > cap) return 0;
  uint32_t crc = wreath_gzip_encoder_crc32_arm(e->crc_arm, 0, in, n);
  uint32_t isz = (uint32_t)n;
  memcpy(p, &crc, 4);
  memcpy(p + 4, &isz, 4);
  return (size_t)(p + 8 - out);
}

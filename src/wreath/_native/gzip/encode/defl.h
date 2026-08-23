/* Internal encoder surface, shared between the parse translation units.
 *
 * SPDX-License-Identifier: MPL-2.0
 */
#ifndef GZ_DEFL_H
#define GZ_DEFL_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "gz.h"

#define GZ_MAX_MATCH 258
#define GZ_WINDOW 32768
/* Swept. These two are #ifndef so a -D on the command line actually binds: the
 * first attempt to sweep them was silently overridden by the header, and the
 * whole sweep came back byte-identical, which is the only reason it was caught.
 * tools/sweep2.sh now fails the build on a redefinition warning. */
#ifndef GZ_HASH_BITS_MAX
#define GZ_HASH_BITS_MAX 18
#endif
#ifndef GZ_HASH_BITS_MIN
#define GZ_HASH_BITS_MIN 11
#endif
/* Known structured text uses a 128 KiB head, leaving the 512 KiB per-core L2
 * available for the live chain window, tokens, input and output.  Across the
 * 30-file corpus this cut encoder instructions 4.8%, L1D misses 12.9%, and
 * last-level proxy misses 52.1%; wire size moved from 23.44% to 23.71%.  The
 * generic path retains its size-derived policy. */
#ifndef GZ_FORMAT_HASH_BITS_MAX
#define GZ_FORMAT_HASH_BITS_MAX 15
#endif
#ifndef GZ_HASH3_BITS
#define GZ_HASH3_BITS 15
#endif

/* Chain-link storage, and a measured negative. The links are only ever followed
 * backwards inside one 32 KiB window, so the array indexed by absolute position
 * is `n` words long -- 2 MiB for a 500 kB body against a 512 KiB L2 -- to hold
 * data that is dead a window later. A power-of-two ring indexed by `pos & mask`
 * holds exactly the live part in 256 KiB.
 *
 * It loses. Measured both ways it costs 1.5% of the encode on json and 5.3% on
 * graphql (one extra AND per chain probe, and the probe loop is six
 * instructions) and moves L1D misses per kB by less than the run-to-run spread.
 * The reason it buys nothing is that the absolute array is *written*
 * sequentially and *read* only within the last 32 KiB of positions, so the live
 * 128 KiB is the only part that was ever resident; the other 1.9 MiB is a
 * streaming store the machine already handles well. Footprint is not the same
 * quantity as traffic. Kept behind a flag because the arithmetic is seductive
 * and the next person will want to see the number rather than the argument.
 *
 * The ring, when enabled, must be *twice* the window, not equal to it: at 32768
 * entries the oldest in-window candidate (pos - 32768) aliases `pos` itself,
 * whose slot was overwritten one line earlier, so the walk re-enters the chain
 * it already walked. Not a correctness bug -- matches are still verified by
 * comparison and distances are still <= 32768 -- but it is wasted probes. */
#ifndef GZ_PREV_RING_BITS
#define GZ_PREV_RING_BITS 0
#endif
/* Link width, 32 or 16 bits. At 16 the ring stores only the low half of the
 * candidate position and the walk reconstructs it: since pos - cand < 32768 <
 * 65536, `cur - (uint16_t)(cur - stored)` is exact. Halves the array again, at
 * two extra arithmetic operations per probe. Requires the ring. */
#ifndef GZ_PREV_W
#define GZ_PREV_W 32
#endif
#if GZ_PREV_W == 16
#if !GZ_PREV_RING_BITS || GZ_PREV_RING_BITS > 16
#error "16-bit links need a ring of at most 65536 slots"
#endif
typedef uint16_t wreath_gzip_encoder_link;
#define GZ_LINK_PUT(v) ((uint16_t)(v))
#define GZ_LINK_GET(prev, cur) ((cur) - (uint16_t)((uint16_t)(cur) - (prev)[GZ_PREV_IDX(cur)]))
#else
typedef uint32_t wreath_gzip_encoder_link;
#define GZ_LINK_PUT(v) (v)
#define GZ_LINK_GET(prev, cur) ((prev)[GZ_PREV_IDX(cur)])
#endif
#if GZ_PREV_RING_BITS
#if (1 << GZ_PREV_RING_BITS) <= GZ_WINDOW
#error "the chain ring must be larger than the window; see the aliasing note"
#endif
#define GZ_PREV_IDX(p) ((p) & ((1u << GZ_PREV_RING_BITS) - 1u))
#define GZ_PREV_SLOTS ((size_t)1 << GZ_PREV_RING_BITS)
#else
#define GZ_PREV_IDX(p) (p)
#endif

#ifndef GZ_SKEW_LINES
#define GZ_SKEW_LINES 0
#endif
#ifndef GZ_PREFETCH
#define GZ_PREFETCH 0
#endif
#ifndef GZ_EXT_WIDE_TAIL
#define GZ_EXT_WIDE_TAIL 1
#endif
#ifndef GZ_HASH_SPECIALIZE_SMALL
#define GZ_HASH_SPECIALIZE_SMALL 1
#endif
#ifndef GZ_HASH_SPECIALIZE_PROFILES
#define GZ_HASH_SPECIALIZE_PROFILES 1
#endif
#ifndef GZ_EMIT_LITERAL_TRIPLES
#define GZ_EMIT_LITERAL_TRIPLES 1
#endif
/* Stream-level experiments.  Both decisions are taken outside the parse loop,
 * so a winning policy changes which specialised loop runs rather than adding
 * another per-position branch. */
#ifndef GZ_ADAPT_SEARCH
#define GZ_ADAPT_SEARCH 1
#endif
#ifndef GZ_ADAPT_COVER_PCT
#define GZ_ADAPT_COVER_PCT 75
#endif
#ifndef GZ_ADAPT_AVG_MATCH
#define GZ_ADAPT_AVG_MATCH 16
#endif
#ifndef GZ_ADAPT_CHAIN
#define GZ_ADAPT_CHAIN 32
#endif
/* A sample dominated by literals has too little useful chain locality to pay
 * for random probes across the 18-bit head table.  Keep the triage sample's
 * full search, then finish with a cache-resident direct table. */
#ifndef GZ_ADAPT_SPARSE_SEARCH
#define GZ_ADAPT_SPARSE_SEARCH 1
#endif
#ifndef GZ_ADAPT_SPARSE_LITERAL_PCT
#define GZ_ADAPT_SPARSE_LITERAL_PCT 75
#endif
/* Start the triage call on the small table when 256 evenly spaced bytes have
 * an index of coincidence below 6/256.  This is integer-only and intentionally
 * well above a uniform byte stream's 1/256 floor. */
#ifndef GZ_ADAPT_SPARSE_RAW_IC
#define GZ_ADAPT_SPARSE_RAW_IC 6
#endif
#ifndef GZ_ADAPT_SPARSE_BITS
#define GZ_ADAPT_SPARSE_BITS 12
#endif
#ifndef GZ_ADAPT_HASH_CONTENT
#define GZ_ADAPT_HASH_CONTENT 0
#endif
#ifndef GZ_ADAPT_HASH_DISTINCT
#define GZ_ADAPT_HASH_DISTINCT 64
#endif
#ifndef GZ_SEQUENCE_IR
#define GZ_SEQUENCE_IR 1
#endif
#if GZ_SEQUENCE_IR
/* A run of literals followed by one match.  Literal bytes remain in the input
 * buffer; the parser writes one record per match/run instead of one word per
 * DEFLATE symbol. */
#define GZ_SEQ_RUN_BITS 14
#define GZ_SEQ_LEN_SHIFT GZ_SEQ_RUN_BITS
#define GZ_SEQ_LEN_MASK 0x1ffu
#define GZ_SEQ_DIST_SHIFT 23
#define GZ_SEQ_RUN_MASK ((1u << GZ_SEQ_RUN_BITS) - 1u)
#ifndef GZ_SEQUENCE_PACKED
#define GZ_SEQUENCE_PACKED 1
#endif
struct
#if GZ_SEQUENCE_PACKED
__attribute__((packed))
#endif
wreath_gzip_encoder_seq {
  uint32_t run_and_len;
  uint16_t dist;
#if !GZ_SEQUENCE_PACKED
  uint16_t reserved;
#endif
};
typedef struct wreath_gzip_encoder_seq wreath_gzip_encoder_item;
#else
typedef uint32_t wreath_gzip_encoder_item;
#endif
#ifndef GZ_CACHE_BEST_BYTE
#define GZ_CACHE_BEST_BYTE 0
#endif
#ifndef GZ_SHORT_MAX
#define GZ_SHORT_MAX 4
#endif
/* 0 = take every match the finder returns; 1 = price short matches against the
 * block's average literal cost; 2 = against the actual bytes covered. See
 * wreath_gzip_encoder_price_short() in deflate.c for why 2 is not the same question as 1. */
#ifndef GZ_SHORTMODE
#define GZ_SHORTMODE 3
#endif
/* Slots in dmax_bits[]. The index is (sum of up to GZ_SHORT_MAX literal code
 * lengths) - (a length code's cost) - 1, and a DEFLATE code length is at most
 * 15, so this many slots make the index provably in range and there is no
 * runtime clamp to write. A guard that cannot be reached cannot be falsified,
 * which is a worse thing to ship than one more table entry. */
#define GZ_DMAX_SLOTS (GZ_SHORT_MAX * 15)

/* Margin by which a short match must beat the literals it replaces before it is
 * taken -- whole bits for mode 2, sixteenths of a bit for mode 1, because those
 * are the resolutions the two budgets are computed at. Zero is the break-even
 * rule both modes compute. Break-even is the wrong threshold because the local
 * comparison systematically under-prices a match: taking one also occupies the
 * two or three positions behind it, where a longer match may have started, and
 * adds a symbol to the distance alphabet that the comparison never charges for.
 * Swept per body and per size in RESULTS.md. */
#ifndef GZ_SHORT_MARGIN
#define GZ_SHORT_MARGIN 0
#endif
#ifndef GZ_SHORT_MARGIN_Q4
#define GZ_SHORT_MARGIN_Q4 0
#endif

/* Seed the short-match cost model from the triage sample instead of leaving it
 * on the fixed-Huffman bootstrap until the first block ends. */
#ifndef GZ_WARM_BLOCK
#define GZ_WARM_BLOCK 1
#endif
/* 0 = never, 1 = at the first checkpoint of a stream, 2 = at every checkpoint. */
#ifndef GZ_WARM_MIN_TAIL
#define GZ_WARM_MIN_TAIL 4
#endif
#ifndef GZ_REPRICE
#define GZ_REPRICE 1
#endif
#ifndef GZ_SEED_MODEL
#define GZ_SEED_MODEL 1
#endif

/* Tokens per block before the buffer forces a flush. Adaptive splitting
 * normally decides first; this is the ceiling, not the policy. */
#ifndef GZ_TOKENS_MAX
#define GZ_TOKENS_MAX 65536
#endif
/* Split decisions are taken on this granularity. */
#ifndef GZ_CHUNK_TOK
#define GZ_CHUNK_TOK 2048
#endif
#ifndef GZ_DEFER_HIST
#define GZ_DEFER_HIST 1
#endif
#if GZ_SEQUENCE_IR && !GZ_DEFER_HIST
#error "the sequence IR currently requires deferred histograms"
#endif
/* Triage looks at the first this many input bytes and then never again. */
#define GZ_TRIAGE_SPAN 8192
#if GZ_SEQUENCE_IR
_Static_assert(GZ_TRIAGE_SPAN <= GZ_SEQ_RUN_MASK,
               "sequence literal-run field must hold the unsynchronised triage sample");
_Static_assert(GZ_CHUNK_TOK <= GZ_SEQ_RUN_MASK,
               "sequence literal-run field must hold one unsynchronised chunk");
_Static_assert(GZ_MAX_MATCH <= GZ_SEQ_LEN_MASK,
               "sequence length field must hold the maximum DEFLATE match");
_Static_assert(30 <= (1u << (32 - GZ_SEQ_DIST_SHIFT)),
               "sequence distance field must hold every DEFLATE distance code");
#endif

/* Token layout: bit 8 distinguishes the two cases, so the emit loop branches on
 * a single test and never reloads. */
#define GZ_TOK_MATCH 0x100u
#define GZ_TOK_LIT(b) ((uint32_t)(b))
#define GZ_TOK_MAKE(len, dist) \
  (GZ_TOK_MATCH | ((uint32_t)((len) - 3) << 9) | ((uint32_t)((dist) - 1) << 17))
#define GZ_TOK_LEN(t) ((((t) >> 9) & 0xffu) + 3u)
#define GZ_TOK_DIST(t) (((t) >> 17) + 1u)

struct wreath_gzip_encoder_prof {
  const char *name;
  uint8_t parse;      /* which instantiation */
  uint16_t chain;     /* hash-chain probe budget */
  uint16_t nice;      /* stop the chain once a match this long is found */
  uint16_t max_lazy;  /* do not try a lazy improvement past this length */
  uint16_t good;      /* quarter the remaining chain past this length; 0 = off */
  uint8_t min_match;  /* 3 or 4 */
  uint8_t split;      /* adaptive block splitting */
  uint8_t triage;     /* incompressible-input detection */
};

enum {
  GZ_PARSE_GREEDY1_NOINS = 0, /* one probe, skipped positions not inserted */
  GZ_PARSE_GREEDY1 = 1,       /* one probe, every position inserted */
  GZ_PARSE_CHAIN = 2,         /* hash chain + lazy */
  GZ_PARSE_CHAIN3 = 3,        /* ... and 3-byte matches */
};

extern const struct wreath_gzip_encoder_prof wreath_gzip_encoder_profiles[GZ_P_COUNT];

struct wreath_gzip_encoder_bw {
  uint8_t *out;
  uint64_t acc;
  unsigned cnt;
};

struct wreath_gzip_encoder_enc {
  uint32_t *head;   /* 4-byte hash heads, biased by `base` */
  void *head_alloc; /* what to free: head may be skewed off it */
  uint32_t *head3;  /* 3-byte hash heads, biased by `base` */
  wreath_gzip_encoder_link *prev;    /* chain links; see GZ_PREV_W */
  size_t prev_cap;
  unsigned hash_bits;
  unsigned hash_size;
  uint32_t base;    /* rebasing counter: makes clearing `head` unnecessary */

  wreath_gzip_encoder_item *tokens;
  unsigned ntok;
#if GZ_SEQUENCE_IR
  unsigned nseq;
  unsigned ck_seq;
  unsigned hist_seq;
  unsigned litrun;
  size_t hist_pos;
#endif
  size_t block_start; /* input offset the current block's tokens began at */

  /* Whole-block symbol counts, and the counts for the chunk since the last
   * split checkpoint. The split test needs both. */
  uint32_t ll_freq[288];
  uint32_t d_freq[32];
  uint32_t ck_ll[288];
  uint32_t ck_d[32];
  unsigned ck_tok;    /* token index the current chunk began at */
  unsigned hist_tok;  /* first token not reflected in ll_freq/d_freq */
  size_t ck_pos;      /* input offset the current chunk began at */
  unsigned gate;      /* act when ntok reaches this: checkpoint or forced flush */
  unsigned split_now; /* resolved for the current call */
  int crc_arm;        /* runtime-selected CRC ISA, owned by this encoder */
  int format;         /* out-of-band GZ_FMT_* hint; never serialized */
  int active_format;  /* resolved for this call; default profile only */

  unsigned warmup;   /* force a block boundary at the first checkpoint */
  unsigned reprice;  /* re-price the short-match model at a checkpoint */
  size_t n_in;       /* this call's input length, for the warm-split test */

  /* Per-block emit tables: one shift-or chain per token instead of four
   * dependent table walks. */
  uint32_t len_pack[GZ_MAX_MATCH + 1];
  uint8_t len_nb[GZ_MAX_MATCH + 1];
  uint32_t d_pack[30];
  uint8_t d_nb[30];
  uint8_t d_tot[30];

  /* Largest distance at which a match of length 3..6 is priced as a win against
   * the literals it replaces. Recomputed from the previous block's code lengths,
   * so it is a measurement of the data, not a constant fitted to a corpus. A
   * long-distance short match is a real ratio loss -- five bytes at distance
   * 20000 costs about 25 bits where five literals cost about 24 -- and a parser
   * that maximises length alone takes it every time. */
  unsigned maxdist[16];
  /* GZ_SHORTMODE 2's model, all from the previous block's own tree:
   * lit_cost[b]      bits this stream spends on literal byte b
   * short_lcost[l]   bits for a length-l match's length code and extra bits
   * dmax_bits[b]     largest distance whose code and extra bits fit in b bits */
  uint8_t lit_cost[256];
  uint8_t short_lcost[16];
  uint32_t dmax_bits[GZ_DMAX_SLOTS];
  struct wreath_gzip_encoder_bw bw;
  uint8_t *out_end;
  const uint8_t *in;
  int failed;
  int stored_all; /* triage fired */
};

/* Recompute the short-match cost model from one block's frequencies and code
 * lengths, for the *next* block to parse against. */
void wreath_gzip_encoder_price_short(wreath_gzip_encoder_enc *e, const uint32_t *llf, const uint8_t *ll_len,
                    const uint8_t *d_len);
void wreath_gzip_encoder_defl_flush_block(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t end, int final);
/* Called from the parse loop when ntok reaches e->gate: takes a split decision,
 * or flushes when the token buffer is full, and re-arms the gate. */
void wreath_gzip_encoder_defl_gate(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t pos);

extern const uint8_t wreath_gzip_encoder_len_sym[GZ_MAX_MATCH + 1]; /* length -> 0..28 */
extern const uint8_t wreath_gzip_encoder_len_ext[29];
extern const uint16_t wreath_gzip_encoder_len_base[29];
extern const uint8_t wreath_gzip_encoder_dist_lo[257];
extern const uint8_t wreath_gzip_encoder_dist_hi[256];
extern const uint8_t wreath_gzip_encoder_dist_ext[30];
extern const uint16_t wreath_gzip_encoder_dist_base[30];

static inline uint32_t wreath_gzip_encoder_ld32(const uint8_t *p) {
  uint32_t v;
  memcpy(&v, p, 4);
  return v;
}
static inline uint16_t wreath_gzip_encoder_ld16(const uint8_t *p) {
  uint16_t v;
  memcpy(&v, p, 2);
  return v;
}
static inline uint64_t wreath_gzip_encoder_ld64(const uint8_t *p) {
  uint64_t v;
  memcpy(&v, p, 8);
  return v;
}

static inline uint32_t wreath_gzip_encoder_hash4(uint32_t v, unsigned bits) {
  return (v * 0x9E3779B1u) >> (32 - bits);
}
/* Three-byte hash: mask the fourth byte off the same load. */
static inline uint32_t wreath_gzip_encoder_hash3(uint32_t v) {
  return ((v & 0x00ffffffu) * 0x9E3779B1u) >> (32 - GZ_HASH3_BITS);
}

static inline unsigned wreath_gzip_encoder_dist_code(unsigned d) {
  return d <= 256 ? wreath_gzip_encoder_dist_lo[d] : wreath_gzip_encoder_dist_hi[(d - 1) >> 7];
}

/* Token append. The caller has already reserved space: GZ_TOKENS_MAX is the
 * hard ceiling and every appender checks it. */
static inline void wreath_gzip_encoder_push_lit(wreath_gzip_encoder_enc *e, unsigned b) {
#if !GZ_DEFER_HIST
  e->ll_freq[b]++;
#endif
#if GZ_SEQUENCE_IR
  (void)b;
  e->litrun++;
  e->ntok++;
#else
  e->tokens[e->ntok++] = GZ_TOK_LIT(b);
#endif
}
static inline void wreath_gzip_encoder_push_match(wreath_gzip_encoder_enc *e, unsigned len, unsigned dist) {
#if !GZ_DEFER_HIST
  e->ll_freq[257 + wreath_gzip_encoder_len_sym[len]]++;
  e->d_freq[wreath_gzip_encoder_dist_code(dist)]++;
#endif
#if GZ_SEQUENCE_IR
  struct wreath_gzip_encoder_seq *s = &e->tokens[e->nseq++];
  unsigned ds = wreath_gzip_encoder_dist_code(dist);
  s->run_and_len = e->litrun | (len << GZ_SEQ_LEN_SHIFT) |
                   (ds << GZ_SEQ_DIST_SHIFT);
  s->dist = (uint16_t)dist;
#if !GZ_SEQUENCE_PACKED
  s->reserved = 0;
#endif
  e->litrun = 0;
  e->ntok++;
#else
  e->tokens[e->ntok++] = GZ_TOK_MAKE(len, dist);
#endif
}

#if GZ_SEQUENCE_IR
static inline void wreath_gzip_encoder_seal_literals(wreath_gzip_encoder_enc *e) {
  if (!e->litrun) return;
  struct wreath_gzip_encoder_seq *s = &e->tokens[e->nseq++];
  s->run_and_len = e->litrun;
  s->dist = 0;
#if !GZ_SEQUENCE_PACKED
  s->reserved = 0;
#endif
  e->litrun = 0;
}
#endif

/* Parse entry points. `stop` bounds where a new token may start; `n` bounds
 * where a match may reach. Splitting the call at GZ_TRIAGE_SPAN is how triage
 * is priced without chunking the inner loop. */
typedef size_t (*wreath_gzip_encoder_parse_fn)(wreath_gzip_encoder_enc *, const uint8_t *, size_t, size_t, size_t,
                              const struct wreath_gzip_encoder_prof *);
#define GZ_PARSE_DECL(n) \
  size_t wreath_gzip_encoder_parse_##n(wreath_gzip_encoder_enc *e, const uint8_t *in, size_t pos, size_t stop, size_t n_, \
                      const struct wreath_gzip_encoder_prof *P)
GZ_PARSE_DECL(greedy1_noins);
GZ_PARSE_DECL(greedy1);
GZ_PARSE_DECL(sparse);
#if GZ_HASH_SPECIALIZE_PROFILES
GZ_PARSE_DECL(greedy1_noins18);
GZ_PARSE_DECL(greedy18);
#endif
GZ_PARSE_DECL(gchain);
GZ_PARSE_DECL(gchain3);
GZ_PARSE_DECL(light);
#if GZ_HASH_SPECIALIZE_PROFILES
GZ_PARSE_DECL(light18);
#endif
GZ_PARSE_DECL(deflt);
#if GZ_HASH_SPECIALIZE_SMALL
GZ_PARSE_DECL(deflt14);
GZ_PARSE_DECL(deflt15);
GZ_PARSE_DECL(deflt16);
GZ_PARSE_DECL(deflt17);
#endif
GZ_PARSE_DECL(deflt18);
GZ_PARSE_DECL(adapt18);
#define GZ_PARSE_FORMAT_DECL(n) \
  GZ_PARSE_DECL(n##14);          \
  GZ_PARSE_DECL(n##15);          \
  GZ_PARSE_DECL(n##16);          \
  GZ_PARSE_DECL(n##17);          \
  GZ_PARSE_DECL(n##18)
GZ_PARSE_FORMAT_DECL(json);
GZ_PARSE_FORMAT_DECL(graphql);
GZ_PARSE_FORMAT_DECL(text);
GZ_PARSE_FORMAT_DECL(textshallow);
GZ_PARSE_FORMAT_DECL(plaintext);
GZ_PARSE_FORMAT_DECL(log);
#undef GZ_PARSE_FORMAT_DECL
GZ_PARSE_DECL(high);
GZ_PARSE_DECL(max);
#if GZ_HASH_SPECIALIZE_PROFILES
GZ_PARSE_DECL(high18);
GZ_PARSE_DECL(max18);
#endif

#endif

/* One fast-lane function. Included twice per ISA arm, with GZ_FUSED 0 and 1.
 *
 * SPDX-License-Identifier: MPL-2.0
 */
int GZFN(GZ_LOOPNAME)(struct wreath_gzip_decoder_st *s) {
#if !GZ_FAST_LANE
  /* Ablation build: the fast lane never runs, so inflate.c's careful loop
   * decodes every symbol with every bound checked. This is the row that prices
   * the slack proof itself. */
  return 0;
#else
  if ((size_t)(s->in_end - s->in) < GZ_IN_SLACK ||
      (size_t)(s->out_end - s->out) < GZ_OUT_SLACK)
    return 0;

  const uint32_t *lt = s->d->lit, *dt = s->d->dist;
#if GZ_COMPACT_LITROOT && !GZ_FUSED
  const uint16_t *rt = s->d->litroot;
#endif
#if GZ_FUSED
  const uint64_t *fu = s->d->fused;
#endif
  const unsigned lroot = s->d->lbits, droot = s->d->dbits;
#if GZ_USE_BMI2
  const uint64_t lmask = 0, dmask = 0;
  (void)lmask;
  (void)dmask;
#else
  const uint64_t lmask = ((uint64_t)1 << lroot) - 1;
  const uint64_t dmask = ((uint64_t)1 << droot) - 1;
#endif
  uint64_t bb = s->bb;
  unsigned cnt = s->bc;
  const uint8_t *ip = s->in;
  const uint8_t *const ifast = s->in_end - GZ_IN_SLACK;
  uint8_t *op = s->out;
  uint8_t *const obase = s->win;
  uint8_t *const ofast = s->out_end - GZ_OUT_SLACK;
  int ret = 0;

  /* Deriving a budget once replaces two pointer compares per iteration with a
   * single decrement (arm 3's W5). The last budgeted iteration still starts
   * with ip <= ifast and op <= ofast, which is what makes the unchecked loads
   * and stores inside it legal. */
  for (;;) {
    if (ip > ifast || op > ofast) break;
    size_t bi = (size_t)(ifast - ip) / GZ_IN_STEP + 1;
    size_t bo = (size_t)(ofast - op) / GZ_OUT_STEP + 1;
    size_t budget = bi < bo ? bi : bo;
    if ((size_t)(op - obase) >= 32768u) {
#define GZ_CHECKDIST 0
#include "inflate_body.h"
#undef GZ_CHECKDIST
    } else {
#define GZ_CHECKDIST 1
#include "inflate_body.h"
#undef GZ_CHECKDIST
    }
  }

out:
  /* Hand back a reader the careful lane can use: bits above `cnt` must be zero
   * for its byte-at-a-time OR to stay consistent. */
  cnt = GZ_CNT;
  s->bb = cnt ? (bb & (~(uint64_t)0 >> (64 - cnt))) : 0;
  s->bc = cnt;
  s->in = ip;
  s->out = op;
  return ret;
#endif
}

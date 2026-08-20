/* The profile ladder's search constants, in one place.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * These are #defines rather than table fields because the parse loop is
 * instantiated once per profile and wants them as compile-time constants:
 * measured here at 60.2 -> 53.4 instructions/byte on json-0500k purely from
 * the register pressure the spilled loads created. Arm 2 reported the same
 * shape from the other direction. deflate.c's runtime table is built from the
 * same macros, so the two cannot drift.
 *
 * Nothing here is fitted to the measurement corpus: it is the conventional
 * doubling ladder of chain depth with a "good enough" cutoff.
 */
#ifndef GZ_PROFILES_H
#define GZ_PROFILES_H

/* Divisor applied to the chain budget for the speculative (lazy) probe.
 *
 * Re-swept jointly with max_lazy, because the two are one decision: how much of
 * the budget to spend on a match that only has to beat one already in hand.
 * The pair (max_lazy 16, budget chain/4) that this encoder shipped is dominated
 * by (max_lazy 48, budget chain/8) -- the same total lazy spend, spread over
 * more positions instead of more probes per position. Measured at 500 kB it is
 * smaller on the whole corpus (+0.452% vs +0.482% against libdeflate) *and*
 * cheaper on plaintext (-2.8%), html (-0.7%) and log (-1.1%), for +1.3% on
 * json. The first few chain steps of a speculative probe carry nearly all its
 * value; the tail of one is worth less than the head of the next. */
#ifndef GZ_LAZYDEP
#define GZ_LAZYDEP 8
#endif

/*                chain  nice  lazy  good */
#ifndef GZP_LIGHT_CHAIN
#define GZP_LIGHT_CHAIN 4
#endif
#ifndef GZP_LIGHT_NICE
#define GZP_LIGHT_NICE 32
#endif
#ifndef GZP_LIGHT_LAZY
#define GZP_LIGHT_LAZY 16
#endif
#ifndef GZP_LIGHT_GOOD
#define GZP_LIGHT_GOOD 0
#endif

#ifndef GZP_DEF_CHAIN
#define GZP_DEF_CHAIN 48
#endif
#ifndef GZP_DEF_NICE
#define GZP_DEF_NICE 258
#endif
#ifndef GZP_DEF_LAZY
#define GZP_DEF_LAZY 48
#endif
#ifndef GZP_DEF_GOOD
#define GZP_DEF_GOOD 0
#endif

#ifndef GZP_HIGH_CHAIN
#define GZP_HIGH_CHAIN 80
#endif
#ifndef GZP_HIGH_NICE
#define GZP_HIGH_NICE 258
#endif
#ifndef GZP_HIGH_LAZY
#define GZP_HIGH_LAZY 64
#endif
#ifndef GZP_HIGH_GOOD
#define GZP_HIGH_GOOD 0
#endif

#ifndef GZP_MAX_CHAIN
#define GZP_MAX_CHAIN 200
#endif
#ifndef GZP_MAX_NICE
#define GZP_MAX_NICE 258
#endif
#ifndef GZP_MAX_LAZY
#define GZP_MAX_LAZY 258
#endif
#ifndef GZP_MAX_GOOD
#define GZP_MAX_GOOD 0
#endif

#endif

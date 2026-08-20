/* The parse shapes. Each profile's chained loop is its own instantiation with
 * its search knobs as compile-time constants; the four generic shapes exist so
 * the ablation overrides have somewhere to run.
 *
 * SPDX-License-Identifier: MPL-2.0
 */
#include "profiles.h"
#ifndef GZ_SHORTPRICE
#define GZ_SHORTPRICE 1
#endif

#define GZP_NAME greedy1_noins
#define GZP_CHAINED 0
#define GZP_INSERT 0
#define GZP_LAZY 0
#define GZP_MIN3 0
#include "parse_core.h"

#if GZ_HASH_SPECIALIZE_PROFILES
#define GZP_NAME greedy1_noins18
#define GZP_HASH_BITS 18
#define GZP_CHAINED 0
#define GZP_INSERT 0
#define GZP_LAZY 0
#define GZP_MIN3 0
#include "parse_core.h"
#endif

#define GZP_NAME greedy1
#define GZP_CHAINED 0
#define GZP_INSERT 1
#define GZP_LAZY 0
#define GZP_MIN3 0
#include "parse_core.h"

/* Selected once for literal-heavy streams.  The 12-bit table occupies 16 KiB,
 * and one verified probe retains cheap structural repetition without
 * chain-link traffic. */
#define GZP_NAME sparse
#define GZP_HASH_BITS GZ_ADAPT_SPARSE_BITS
#define GZP_CHAINED 0
#define GZP_INSERT 1
#define GZP_LAZY 0
#define GZP_MIN3 0
#include "parse_core.h"

#if GZ_HASH_SPECIALIZE_PROFILES
#define GZP_NAME greedy18
#define GZP_HASH_BITS 18
#define GZP_CHAINED 0
#define GZP_INSERT 1
#define GZP_LAZY 0
#define GZP_MIN3 0
#include "parse_core.h"
#endif

#define GZP_NAME gchain
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#include "parse_core.h"

#define GZP_NAME gchain3
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 1
#include "parse_core.h"

#define GZP_NAME light
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_LIGHT_CHAIN
#define GZP_NICE_C GZP_LIGHT_NICE
#define GZP_LAZY_C GZP_LIGHT_LAZY
#define GZP_GOOD_C GZP_LIGHT_GOOD
#include "parse_core.h"

#if GZ_HASH_SPECIALIZE_PROFILES
#define GZP_NAME light18
#define GZP_HASH_BITS 18
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_LIGHT_CHAIN
#define GZP_NICE_C GZP_LIGHT_NICE
#define GZP_LAZY_C GZP_LIGHT_LAZY
#define GZP_GOOD_C GZP_LIGHT_GOOD
#include "parse_core.h"
#endif

#define GZP_NAME deflt
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_DEF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

/* The default profile gets one loop per hash-table size. This turns the
 * per-position variable shift into an immediate shift and releases the count
 * register throughout the hash-chain loop. Selection is once per encode call;
 * the other profiles retain the compact generic-width loop. */
#if GZ_HASH_SPECIALIZE_SMALL
#define GZP_NAME deflt14
#define GZP_HASH_BITS 14
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_DEF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME deflt15
#define GZP_HASH_BITS 15
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_DEF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME deflt16
#define GZP_HASH_BITS 16
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_DEF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME deflt17
#define GZP_HASH_BITS 17
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_DEF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"
#endif

#define GZP_NAME deflt18
#define GZP_HASH_BITS 18
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_DEF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

/* Intermediate rung selected only after the triage sample found dense, long
 * matches.  It preserves the default parser's match and lazy semantics while
 * spending fewer probes on an input that has already proved easy to search. */
#define GZP_NAME adapt18
#define GZP_HASH_BITS 18
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZ_ADAPT_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME high
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_HIGH_CHAIN
#define GZP_NICE_C GZP_HIGH_NICE
#define GZP_LAZY_C GZP_HIGH_LAZY
#define GZP_GOOD_C GZP_HIGH_GOOD
#include "parse_core.h"

#if GZ_HASH_SPECIALIZE_PROFILES
#define GZP_NAME high18
#define GZP_HASH_BITS 18
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_HIGH_CHAIN
#define GZP_NICE_C GZP_HIGH_NICE
#define GZP_LAZY_C GZP_HIGH_LAZY
#define GZP_GOOD_C GZP_HIGH_GOOD
#include "parse_core.h"
#endif

#define GZP_NAME max
#define GZP_CACHE_BEST 1
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_MAX_CHAIN
#define GZP_NICE_C GZP_MAX_NICE
#define GZP_LAZY_C GZP_MAX_LAZY
#define GZP_GOOD_C GZP_MAX_GOOD
#include "parse_core.h"

#if GZ_HASH_SPECIALIZE_PROFILES
#define GZP_NAME max18
#define GZP_CACHE_BEST 1
#define GZP_HASH_BITS 18
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZ_SHORTPRICE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZP_MAX_CHAIN
#define GZP_NICE_C GZP_MAX_NICE
#define GZP_LAZY_C GZP_MAX_LAZY
#define GZP_GOOD_C GZP_MAX_GOOD
#include "parse_core.h"
#endif

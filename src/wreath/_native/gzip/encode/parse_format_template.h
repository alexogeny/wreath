/* Instantiate one format-specialised default parser at hash widths 14..18.
 * Deliberately has no include guard: parse_formats.c includes it once per
 * policy after defining GZPF_NAME, GZPF_CHAIN, GZPF_SHORTPRICE and
 * GZPF_SHORTMODE. */

#define GZPF_CAT2_(a, b) a##b
#define GZPF_CAT_(a, b) GZPF_CAT2_(a, b)

#define GZP_NAME GZPF_CAT_(GZPF_NAME, 14)
#define GZP_HASH_BITS 14
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZPF_SHORTPRICE
#define GZP_SHORTMODE GZPF_SHORTMODE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZPF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME GZPF_CAT_(GZPF_NAME, 15)
#define GZP_HASH_BITS 15
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZPF_SHORTPRICE
#define GZP_SHORTMODE GZPF_SHORTMODE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZPF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME GZPF_CAT_(GZPF_NAME, 16)
#define GZP_HASH_BITS 16
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZPF_SHORTPRICE
#define GZP_SHORTMODE GZPF_SHORTMODE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZPF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME GZPF_CAT_(GZPF_NAME, 17)
#define GZP_HASH_BITS 17
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZPF_SHORTPRICE
#define GZP_SHORTMODE GZPF_SHORTMODE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZPF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#define GZP_NAME GZPF_CAT_(GZPF_NAME, 18)
#define GZP_HASH_BITS 18
#define GZP_LAZYDEPTH GZ_LAZYDEP
#define GZP_SHORTPRICE GZPF_SHORTPRICE
#define GZP_SHORTMODE GZPF_SHORTMODE
#define GZP_CHAINED 1
#define GZP_INSERT 1
#define GZP_LAZY 1
#define GZP_MIN3 0
#define GZP_CHAIN_C GZPF_CHAIN
#define GZP_NICE_C GZP_DEF_NICE
#define GZP_LAZY_C GZP_DEF_LAZY
#define GZP_GOOD_C GZP_DEF_GOOD
#include "parse_core.h"

#undef GZPF_CAT_
#undef GZPF_CAT2_
#undef GZPF_NAME
#undef GZPF_CHAIN
#undef GZPF_SHORTPRICE
#undef GZPF_SHORTMODE

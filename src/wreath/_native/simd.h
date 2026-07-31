/* Runtime-dispatched byte scanners for the native hot paths.
 *
 * Every kernel here answers the same shape of question -- "how many bytes may
 * I pass over before one needs handling?" -- and every kernel ships four arms
 * that must return the identical answer:
 *
 *   scalar  one byte at a time; the definition the others are checked against
 *   swar    eight bytes per step, portable to every compiler and target
 *   sse2    sixteen bytes per step, baseline on every x86-64, no dispatch
 *   avx2    thirty-two bytes per step, selected per call
 *
 * **No cached feature flag.** ADR 0007 forbids process-global mutable state,
 * and a `static int have_avx2` is exactly that: a write shared by every thread
 * on the free-threaded build, for a value that never changes.
 * `__builtin_cpu_supports` needs no cache -- it is a load and a bit test
 * against a table libgcc's constructor fills before `main`, which is far below
 * the cost of the loop it guards and perfectly predicted after the first call.
 *
 * The arms stay individually callable rather than collapsing into the
 * dispatcher, because the only way to know an arm is correct is to run it
 * against the scalar one on the same bytes: `_core.simd_probe()` exposes them
 * to `tests/test_native_simd.py` for exactly that.
 *
 * `ptrdiff_t` rather than `Py_ssize_t` keeps this header free of Python.h, so
 * the arms can be compiled and timed on their own. The two are the same width
 * on every platform CPython supports.
 */
#ifndef WREATH_SIMD_H
#define WREATH_SIMD_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* Arm identifiers, ascending by register width. */
#define WREATH_ARM_SCALAR 0
#define WREATH_ARM_SWAR 1
#define WREATH_ARM_SSE2 2
#define WREATH_ARM_AVX2 3
#define WREATH_ARM_NEON 4

#if defined(__SSE2__)
#include <emmintrin.h>
#define WREATH_HAVE_SSE2 1
#endif

/* The AVX2 arm needs both the intrinsics and per-function target selection.
 * MSVC has the first and not the second, so it stays on SSE2. */
#if defined(WREATH_HAVE_SSE2) && (defined(__GNUC__) || defined(__clang__))
#include <immintrin.h>
#define WREATH_HAVE_AVX2 1
#define WREATH_TARGET_AVX2 __attribute__((target("avx2")))
#else
#define WREATH_TARGET_AVX2
#endif

/* NEON is part of the ARMv8 baseline, so aarch64 needs no dispatch at all:
 * the arm is either compiled in and always usable, or the target is not
 * aarch64. That makes it simpler than the x86 side, not more delicate. */
#if defined(__aarch64__)
#include <arm_neon.h>
#define WREATH_HAVE_NEON 1
#endif

static inline int
wreath_simd_has_avx2(void)
{
#if defined(WREATH_HAVE_AVX2)
    return __builtin_cpu_supports("avx2");
#else
    return 0;
#endif
}

/* --- SWAR primitives ----------------------------------------------------- */

#define WREATH_SWAR_ONES 0x0101010101010101ULL
#define WREATH_SWAR_HIGH 0x8080808080808080ULL

static inline uint64_t
wreath_swar_has_zero(uint64_t x)
{
    return (x - WREATH_SWAR_ONES) & ~x & WREATH_SWAR_HIGH;
}

/* Bytes strictly below `value`. Only valid while every byte is < 0x80, which
 * every caller here guarantees by testing the high bits separately. */
static inline uint64_t
wreath_swar_lt(uint64_t word, uint64_t value)
{
    return (word - WREATH_SWAR_ONES * value) & ~word & WREATH_SWAR_HIGH;
}

static inline uint64_t
wreath_swar_eq(uint64_t word, uint64_t value)
{
    return wreath_swar_has_zero(word ^ (WREATH_SWAR_ONES * value));
}

/* ======================================================================== */
/* JSON string bodies: the first byte that cannot be copied through as-is.    */
/*                                                                           */
/* `seen_high` is raised (never lowered) when any byte *passed over* had its  */
/* high bit set. The decoder uses it to choose between a one-byte str and a   */
/* UTF-8 decode, so a byte at or after the stopping point must not count.     */
/* ======================================================================== */

static inline ptrdiff_t
wreath_json_run_scalar(const char *data, ptrdiff_t len, unsigned *seen_high)
{
    unsigned high = 0;
    ptrdiff_t i = 0;
    for (; i < len; i++) {
        uint8_t c = (uint8_t)data[i];
        if (c < 0x20 || c == '"' || c == '\\') {
            break;
        }
        high |= c;
    }
    if (high & 0x80u) {
        *seen_high |= 1u;
    }
    return i;
}

/* The borrow-based `lt` test over-reports: subtracting 0x20 from a byte below
 * it borrows into the next, which can flag a byte that is in range. It never
 * *under*-reports, so a flagged word is a candidate to be re-checked byte by
 * byte rather than an answer. A false positive costs one scalar word and the
 * scan continues in words; only a real stop returns. */
static inline ptrdiff_t
wreath_json_run_swar(const char *data, ptrdiff_t len, unsigned *seen_high)
{
    ptrdiff_t i = 0;
    while (len - i >= 8) {
        uint64_t word;
        memcpy(&word, data + i, 8);
        if (wreath_swar_lt(word, 0x20) | wreath_swar_eq(word, '"') |
            wreath_swar_eq(word, '\\')) {
            ptrdiff_t stop = wreath_json_run_scalar(data + i, 8, seen_high);
            if (stop < 8) {
                return i + stop;
            }
        }
        /* Folded per word rather than accumulated and folded once at the end:
         * a later word can return from inside this loop, and an accumulator
         * that is only read after it would drop every high bit seen so far.
         * The differential probe caught exactly that. */
        else if (word & WREATH_SWAR_HIGH) {
            *seen_high |= 1u;
        }
        i += 8;
    }
    return i + wreath_json_run_scalar(data + i, len - i, seen_high);
}

#if defined(WREATH_HAVE_SSE2)
static inline ptrdiff_t
wreath_json_run_sse2(const char *data, ptrdiff_t len, unsigned *seen_high)
{
    const __m128i quote = _mm_set1_epi8('"');
    const __m128i backslash = _mm_set1_epi8('\\');
    const __m128i ctrl_max = _mm_set1_epi8(0x1F);
    ptrdiff_t i = 0;
    while (len - i >= 16) {
        __m128i v = _mm_loadu_si128((const __m128i *)(const void *)(data + i));
        __m128i special = _mm_or_si128(
            _mm_or_si128(_mm_cmpeq_epi8(v, quote), _mm_cmpeq_epi8(v, backslash)),
            _mm_cmpeq_epi8(_mm_min_epu8(v, ctrl_max), v));
        unsigned mask = (unsigned)_mm_movemask_epi8(special);
        unsigned highs = (unsigned)_mm_movemask_epi8(v);
        if (mask != 0) {
            /* Bits strictly below the lowest set bit: the bytes actually
             * passed over, so a non-ASCII byte at or past the stop does not
             * count as seen. */
            if (highs & ((mask ^ (mask - 1)) >> 1)) {
                *seen_high |= 1u;
            }
            return i + (ptrdiff_t)__builtin_ctz(mask);
        }
        if (highs) {
            *seen_high |= 1u;
        }
        i += 16;
    }
    return i + wreath_json_run_swar(data + i, len - i, seen_high);
}
#endif

#if defined(WREATH_HAVE_AVX2)
WREATH_TARGET_AVX2 static inline ptrdiff_t
wreath_json_run_avx2(const char *data, ptrdiff_t len, unsigned *seen_high)
{
    const __m256i quote = _mm256_set1_epi8('"');
    const __m256i backslash = _mm256_set1_epi8('\\');
    const __m256i ctrl_max = _mm256_set1_epi8(0x1F);
    ptrdiff_t i = 0;
    while (len - i >= 32) {
        __m256i v = _mm256_loadu_si256((const __m256i *)(const void *)(data + i));
        __m256i special = _mm256_or_si256(
            _mm256_or_si256(_mm256_cmpeq_epi8(v, quote), _mm256_cmpeq_epi8(v, backslash)),
            _mm256_cmpeq_epi8(_mm256_min_epu8(v, ctrl_max), v));
        unsigned mask = (unsigned)_mm256_movemask_epi8(special);
        unsigned highs = (unsigned)_mm256_movemask_epi8(v);
        if (mask != 0) {
            if (highs & ((mask ^ (mask - 1)) >> 1)) {
                *seen_high |= 1u;
            }
            return i + (ptrdiff_t)__builtin_ctz(mask);
        }
        if (highs) {
            *seen_high |= 1u;
        }
        i += 32;
    }
    return i + wreath_json_run_sse2(data + i, len - i, seen_high);
}
#endif

/* Below one vector register there is nothing to vectorise, and the walk down
 * the arms -- dispatcher, avx2, sse2, swar -- costs four calls to discover
 * that. Short runs are the common case wherever the interesting bytes are
 * dense (escape-heavy JSON, HTML full of tags), so they take the scalar loop
 * directly. Measured: without this, escape-heavy decoding and template
 * escaping were 10-15% *slower* than the byte loop they replaced. */

#if defined(WREATH_HAVE_NEON)
/* Index of the first 0xFF lane in a NEON comparison result.
 *
 * NEON has no `movemask`. The idiom is to narrow each 16-bit lane down to its
 * low nibble, which leaves four bits per input byte in a single 64-bit word;
 * the first set bit divided by four is the byte. */
static inline int
wreath_neon_first_lane(uint8x16_t cmp)
{
    uint64_t packed = vget_lane_u64(
        vreinterpret_u64_u8(vshrn_n_u16(vreinterpretq_u16_u8(cmp), 4)), 0);
    return (int)(__builtin_ctzll(packed) >> 2);
}

static inline ptrdiff_t
wreath_json_run_neon(const char *data, ptrdiff_t len, unsigned *seen_high)
{
    const uint8x16_t quote = vdupq_n_u8('"');
    const uint8x16_t backslash = vdupq_n_u8('\\');
    const uint8x16_t ctrl = vdupq_n_u8(0x20);
    const uint8x16_t top = vdupq_n_u8(0x80);
    ptrdiff_t i = 0;
    while (len - i >= 16) {
        uint8x16_t v = vld1q_u8((const uint8_t *)(data + i));
        uint8x16_t special = vorrq_u8(vorrq_u8(vceqq_u8(v, quote), vceqq_u8(v, backslash)),
                                      vcltq_u8(v, ctrl));
        if (vmaxvq_u8(special) != 0) {
            /* Fold the high bits of exactly the bytes passed over by running
             * the scalar definition across them, rather than deriving a second
             * mask: the prefix is at most fifteen bytes and the definition
             * cannot disagree with itself. */
            int stop = wreath_neon_first_lane(special);
            return i + wreath_json_run_scalar(data + i, stop, seen_high);
        }
        if (vmaxvq_u8(vandq_u8(v, top)) != 0) {
            *seen_high |= 1u;
        }
        i += 16;
    }
    return i + wreath_json_run_swar(data + i, len - i, seen_high);
}

static inline ptrdiff_t
wreath_html_run_neon(const char *data, ptrdiff_t len)
{
    const uint8x16_t amp = vdupq_n_u8('&');
    const uint8x16_t lt = vdupq_n_u8('<');
    const uint8x16_t gt = vdupq_n_u8('>');
    const uint8x16_t quote = vdupq_n_u8('"');
    const uint8x16_t apos = vdupq_n_u8('\'');
    ptrdiff_t i = 0;
    while (len - i >= 16) {
        uint8x16_t v = vld1q_u8((const uint8_t *)(data + i));
        uint8x16_t special = vorrq_u8(
            vorrq_u8(vorrq_u8(vceqq_u8(v, amp), vceqq_u8(v, lt)),
                     vorrq_u8(vceqq_u8(v, gt), vceqq_u8(v, quote))),
            vceqq_u8(v, apos));
        if (vmaxvq_u8(special) != 0) {
            return i + wreath_neon_first_lane(special);
        }
        i += 16;
    }
    return i + wreath_html_run_swar(data + i, len - i);
}

static inline ptrdiff_t
wreath_value_run_neon(const char *data, ptrdiff_t len)
{
    const uint8x16_t tab = vdupq_n_u8('\t');
    const uint8x16_t del = vdupq_n_u8(0x7f);
    const uint8x16_t ctrl = vdupq_n_u8(0x20);
    ptrdiff_t i = 0;
    while (len - i >= 16) {
        uint8x16_t v = vld1q_u8((const uint8_t *)(data + i));
        /* `vcltq_u8` is an unsigned compare, so obs-text (>= 0x80) is not a
         * control byte here and needs no separate guard. */
        uint8x16_t stops = vorrq_u8(vbicq_u8(vcltq_u8(v, ctrl), vceqq_u8(v, tab)),
                                    vceqq_u8(v, del));
        if (vmaxvq_u8(stops) != 0) {
            return i + wreath_neon_first_lane(stops);
        }
        i += 16;
    }
    return i + wreath_value_run_swar(data + i, len - i);
}

static inline void
wreath_xor_mask_neon(uint8_t *dst, const uint8_t *src, ptrdiff_t len, const uint8_t *key)
{
    uint32_t key32;
    uint8x16_t pattern;
    ptrdiff_t i = 0;
    memcpy(&key32, key, 4);
    pattern = vreinterpretq_u8_u32(vdupq_n_u32(key32));
    for (; i + 16 <= len; i += 16) {
        vst1q_u8(dst + i, veorq_u8(vld1q_u8(src + i), pattern));
    }
    wreath_xor_mask_swar(dst + i, src + i, len - i, key);
}
#endif

/* Straight to the widest arm, with no cheap pre-test for a nearby stop.
 * That pre-test seemed obviously right and measured wrong: on 4 KiB with a
 * quote every four bytes -- denser than any real payload -- AVX2 answers in
 * 2.1ns and the SWAR word it would have diverted to takes 5.5ns. Loading a
 * vector register and finding the answer in the first lane still beats
 * deciding in scalar code whether it was worth loading. */
static inline ptrdiff_t
wreath_json_run(const char *data, ptrdiff_t len, unsigned *seen_high)
{
    if (len < 16) {
        return wreath_json_run_scalar(data, len, seen_high);
    }
    /* Answer from the first word when it holds the stop, without leaving this
     * function. The wide arms are real calls -- a `target("avx2")` function
     * cannot be inlined into a caller that is not itself AVX2 -- and escaped
     * text asks for a run of four or five bytes hundreds of times per string,
     * where that call is the whole cost. This is not the pre-test removed
     * above: that one diverted the entire scan to SWAR and lost the wide arm
     * on long runs. This returns only on a *confirmed* stop and otherwise
     * falls through to the vector path unchanged. */
    {
        uint64_t first;
        memcpy(&first, data, 8);
        if (wreath_swar_lt(first, 0x20) | wreath_swar_eq(first, '"') |
            wreath_swar_eq(first, '\\')) {
            ptrdiff_t stop = wreath_json_run_scalar(data, 8, seen_high);
            if (stop < 8) {
                return stop;
            }
        }
    }
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        return wreath_json_run_avx2(data, len, seen_high);
    }
#endif
#if defined(WREATH_HAVE_NEON)
    return wreath_json_run_neon(data, len, seen_high);
#elif defined(WREATH_HAVE_SSE2)
    return wreath_json_run_sse2(data, len, seen_high);
#else
    return wreath_json_run_swar(data, len, seen_high);
#endif
}

/* ======================================================================== */
/* HTML text: the first byte the template engine must replace with an entity. */
/* ======================================================================== */

static inline int
wreath_is_html_special(uint8_t c)
{
    return c == '&' || c == '<' || c == '>' || c == '"' || c == '\'';
}

static inline ptrdiff_t
wreath_html_run_scalar(const char *data, ptrdiff_t len)
{
    ptrdiff_t i = 0;
    for (; i < len; i++) {
        if (wreath_is_html_special((uint8_t)data[i])) {
            break;
        }
    }
    return i;
}

static inline ptrdiff_t
wreath_html_run_swar(const char *data, ptrdiff_t len)
{
    ptrdiff_t i = 0;
    while (len - i >= 8) {
        uint64_t word;
        memcpy(&word, data + i, 8);
        if (wreath_swar_eq(word, '&') | wreath_swar_eq(word, '<') |
            wreath_swar_eq(word, '>') | wreath_swar_eq(word, '"') |
            wreath_swar_eq(word, '\'')) {
            ptrdiff_t stop = wreath_html_run_scalar(data + i, 8);
            if (stop < 8) {
                return i + stop;
            }
        }
        i += 8;
    }
    return i + wreath_html_run_scalar(data + i, len - i);
}

#if defined(WREATH_HAVE_SSE2)
static inline ptrdiff_t
wreath_html_run_sse2(const char *data, ptrdiff_t len)
{
    const __m128i amp = _mm_set1_epi8('&');
    const __m128i lt = _mm_set1_epi8('<');
    const __m128i gt = _mm_set1_epi8('>');
    const __m128i quote = _mm_set1_epi8('"');
    const __m128i apos = _mm_set1_epi8('\'');
    ptrdiff_t i = 0;
    while (len - i >= 16) {
        __m128i v = _mm_loadu_si128((const __m128i *)(const void *)(data + i));
        __m128i special = _mm_or_si128(
            _mm_or_si128(_mm_or_si128(_mm_cmpeq_epi8(v, amp), _mm_cmpeq_epi8(v, lt)),
                         _mm_or_si128(_mm_cmpeq_epi8(v, gt), _mm_cmpeq_epi8(v, quote))),
            _mm_cmpeq_epi8(v, apos));
        unsigned mask = (unsigned)_mm_movemask_epi8(special);
        if (mask != 0) {
            return i + (ptrdiff_t)__builtin_ctz(mask);
        }
        i += 16;
    }
    return i + wreath_html_run_swar(data + i, len - i);
}
#endif

#if defined(WREATH_HAVE_AVX2)
WREATH_TARGET_AVX2 static inline ptrdiff_t
wreath_html_run_avx2(const char *data, ptrdiff_t len)
{
    const __m256i amp = _mm256_set1_epi8('&');
    const __m256i lt = _mm256_set1_epi8('<');
    const __m256i gt = _mm256_set1_epi8('>');
    const __m256i quote = _mm256_set1_epi8('"');
    const __m256i apos = _mm256_set1_epi8('\'');
    ptrdiff_t i = 0;
    while (len - i >= 32) {
        __m256i v = _mm256_loadu_si256((const __m256i *)(const void *)(data + i));
        __m256i special = _mm256_or_si256(
            _mm256_or_si256(
                _mm256_or_si256(_mm256_cmpeq_epi8(v, amp), _mm256_cmpeq_epi8(v, lt)),
                _mm256_or_si256(_mm256_cmpeq_epi8(v, gt), _mm256_cmpeq_epi8(v, quote))),
            _mm256_cmpeq_epi8(v, apos));
        unsigned mask = (unsigned)_mm256_movemask_epi8(special);
        if (mask != 0) {
            return i + (ptrdiff_t)__builtin_ctz(mask);
        }
        i += 32;
    }
    return i + wreath_html_run_sse2(data + i, len - i);
}
#endif

static inline ptrdiff_t
wreath_html_run(const char *data, ptrdiff_t len)
{
    if (len < 16) {
        return wreath_html_run_scalar(data, len);
    }
    {
        uint64_t first;
        memcpy(&first, data, 8);
        if (wreath_swar_eq(first, '&') | wreath_swar_eq(first, '<') |
            wreath_swar_eq(first, '>') | wreath_swar_eq(first, '"') |
            wreath_swar_eq(first, '\'')) {
            ptrdiff_t stop = wreath_html_run_scalar(data, 8);
            if (stop < 8) {
                return stop;
            }
        }
    }
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        return wreath_html_run_avx2(data, len);
    }
#endif
#if defined(WREATH_HAVE_NEON)
    return wreath_html_run_neon(data, len);
#elif defined(WREATH_HAVE_SSE2)
    return wreath_html_run_sse2(data, len);
#else
    return wreath_html_run_swar(data, len);
#endif
}

/* ======================================================================== */
/* Header field values: the first byte that ends the value or forbids it.     */
/*                                                                           */
/* Stops on CR (the line ending), on any control byte other than HTAB, and on */
/* DEL. The caller decides which it found by looking at the byte, exactly as  */
/* the scalar loop it replaces did -- a run that ends on CR is a well-formed  */
/* value, anything else is malformed.                                        */
/* ======================================================================== */

static inline int
wreath_is_value_stop(uint8_t c)
{
    return (c < 0x20 && c != '\t') || c == 0x7f;
}

static inline ptrdiff_t
wreath_value_run_scalar(const char *data, ptrdiff_t len)
{
    ptrdiff_t i = 0;
    for (; i < len; i++) {
        if (wreath_is_value_stop((uint8_t)data[i])) {
            break;
        }
    }
    return i;
}

static inline ptrdiff_t
wreath_value_run_swar(const char *data, ptrdiff_t len)
{
    ptrdiff_t i = 0;
    while (len - i >= 8) {
        uint64_t word;
        memcpy(&word, data + i, 8);
        /* Presence, never position. A SWAR test says only *that* the word
         * holds a byte of interest; which byte is not reliable, because the
         * subtraction borrows across lanes. This originally excluded HTAB by
         * masking with `& ~eq('\t')`, and the mask cleared the flag for a
         * neighbouring 0x08 as well -- a control byte that must be refused
         * walked straight through. The differential probe caught it on the
         * second seed. So: over-report here, and let the scalar re-check
         * below decide. A tab costs one eight-byte rescan. */
        if (wreath_swar_lt(word, 0x20) | wreath_swar_eq(word, 0x7f)) {
            ptrdiff_t stop = wreath_value_run_scalar(data + i, 8);
            if (stop < 8) {
                return i + stop;
            }
        }
        i += 8;
    }
    return i + wreath_value_run_scalar(data + i, len - i);
}

#if defined(WREATH_HAVE_SSE2)
static inline ptrdiff_t
wreath_value_run_sse2(const char *data, ptrdiff_t len)
{
    const __m128i tab = _mm_set1_epi8('\t');
    const __m128i del = _mm_set1_epi8(0x7f);
    const __m128i ctrl_max = _mm_set1_epi8(0x1F);
    ptrdiff_t i = 0;
    while (len - i >= 16) {
        __m128i v = _mm_loadu_si128((const __m128i *)(const void *)(data + i));
        __m128i ctrl = _mm_andnot_si128(_mm_cmpeq_epi8(v, tab),
                                        _mm_cmpeq_epi8(_mm_min_epu8(v, ctrl_max), v));
        unsigned mask = (unsigned)_mm_movemask_epi8(
            _mm_or_si128(ctrl, _mm_cmpeq_epi8(v, del)));
        if (mask != 0) {
            return i + (ptrdiff_t)__builtin_ctz(mask);
        }
        i += 16;
    }
    return i + wreath_value_run_swar(data + i, len - i);
}
#endif

#if defined(WREATH_HAVE_AVX2)
WREATH_TARGET_AVX2 static inline ptrdiff_t
wreath_value_run_avx2(const char *data, ptrdiff_t len)
{
    const __m256i tab = _mm256_set1_epi8('\t');
    const __m256i del = _mm256_set1_epi8(0x7f);
    const __m256i ctrl_max = _mm256_set1_epi8(0x1F);
    ptrdiff_t i = 0;
    while (len - i >= 32) {
        __m256i v = _mm256_loadu_si256((const __m256i *)(const void *)(data + i));
        __m256i ctrl = _mm256_andnot_si256(
            _mm256_cmpeq_epi8(v, tab), _mm256_cmpeq_epi8(_mm256_min_epu8(v, ctrl_max), v));
        unsigned mask = (unsigned)_mm256_movemask_epi8(
            _mm256_or_si256(ctrl, _mm256_cmpeq_epi8(v, del)));
        if (mask != 0) {
            return i + (ptrdiff_t)__builtin_ctz(mask);
        }
        i += 32;
    }
    return i + wreath_value_run_sse2(data + i, len - i);
}
#endif

static inline ptrdiff_t
wreath_value_run(const char *data, ptrdiff_t len)
{
    if (len < 16) {
        return wreath_value_run_scalar(data, len);
    }
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        return wreath_value_run_avx2(data, len);
    }
#endif
#if defined(WREATH_HAVE_NEON)
    return wreath_value_run_neon(data, len);
#elif defined(WREATH_HAVE_SSE2)
    return wreath_value_run_sse2(data, len);
#else
    return wreath_value_run_swar(data, len);
#endif
}

/* ======================================================================== */
/* WebSocket payload masking: XOR against a repeating four-byte key.          */
/* ======================================================================== */

static inline void
wreath_xor_mask_scalar(uint8_t *dst, const uint8_t *src, ptrdiff_t len, const uint8_t *key)
{
    for (ptrdiff_t i = 0; i < len; i++) {
        dst[i] = src[i] ^ key[i & 3];
    }
}

static inline void
wreath_xor_mask_swar(uint8_t *dst, const uint8_t *src, ptrdiff_t len, const uint8_t *key)
{
    uint32_t key32;
    uint64_t pattern;
    ptrdiff_t i = 0;
    memcpy(&key32, key, 4);
    pattern = ((uint64_t)key32 << 32) | key32;
    for (; i + 8 <= len; i += 8) {
        uint64_t word;
        memcpy(&word, src + i, 8);
        word ^= pattern;
        memcpy(dst + i, &word, 8);
    }
    wreath_xor_mask_scalar(dst + i, src + i, len - i, key);
}

#if defined(WREATH_HAVE_SSE2)
static inline void
wreath_xor_mask_sse2(uint8_t *dst, const uint8_t *src, ptrdiff_t len, const uint8_t *key)
{
    uint32_t key32;
    __m128i pattern;
    ptrdiff_t i = 0;
    memcpy(&key32, key, 4);
    pattern = _mm_set1_epi32((int)key32);
    for (; i + 16 <= len; i += 16) {
        __m128i v = _mm_loadu_si128((const __m128i *)(const void *)(src + i));
        _mm_storeu_si128((__m128i *)(void *)(dst + i), _mm_xor_si128(v, pattern));
    }
    wreath_xor_mask_swar(dst + i, src + i, len - i, key);
}
#endif

#if defined(WREATH_HAVE_AVX2)
WREATH_TARGET_AVX2 static inline void
wreath_xor_mask_avx2(uint8_t *dst, const uint8_t *src, ptrdiff_t len, const uint8_t *key)
{
    uint32_t key32;
    __m256i pattern;
    ptrdiff_t i = 0;
    memcpy(&key32, key, 4);
    pattern = _mm256_set1_epi32((int)key32);
    for (; i + 32 <= len; i += 32) {
        __m256i v = _mm256_loadu_si256((const __m256i *)(const void *)(src + i));
        _mm256_storeu_si256((__m256i *)(void *)(dst + i), _mm256_xor_si256(v, pattern));
    }
    wreath_xor_mask_sse2(dst + i, src + i, len - i, key);
}
#endif

static inline void
wreath_xor_mask(uint8_t *dst, const uint8_t *src, ptrdiff_t len, const uint8_t *key)
{
    if (len < 16) {
        wreath_xor_mask_swar(dst, src, len, key);
        return;
    }
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        wreath_xor_mask_avx2(dst, src, len, key);
        return;
    }
#endif
#if defined(WREATH_HAVE_NEON)
    wreath_xor_mask_neon(dst, src, len, key);
#elif defined(WREATH_HAVE_SSE2)
    wreath_xor_mask_sse2(dst, src, len, key);
#else
    wreath_xor_mask_swar(dst, src, len, key);
#endif
}

/* ======================================================================== */
/* base64url decoding: A-Z a-z 0-9 - _ , unpadded. Used per JWT segment.      */
/* ======================================================================== */

/* Six-bit value of each byte; 0xFF for everything outside the alphabet. Any
 * legal value is <= 63, so four characters are validated with one OR. */
static const unsigned char wreath_b64url_value[256] = {
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x3E, 0xFF, 0xFF,
    0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E,
    0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0xFF, 0xFF, 0xFF, 0xFF, 0x3F,
    0xFF, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
};

static inline ptrdiff_t
wreath_b64url_decode_scalar(const char *in, ptrdiff_t len, unsigned char *out)
{
    ptrdiff_t o = 0;
    ptrdiff_t i = 0;
    if (len % 4 == 1) {
        return -1;  /* no whole number of characters encodes to this */
    }
    for (; i + 4 <= len; i += 4) {
        unsigned v0 = wreath_b64url_value[(unsigned char)in[i]];
        unsigned v1 = wreath_b64url_value[(unsigned char)in[i + 1]];
        unsigned v2 = wreath_b64url_value[(unsigned char)in[i + 2]];
        unsigned v3 = wreath_b64url_value[(unsigned char)in[i + 3]];
        if ((v0 | v1 | v2 | v3) > 63) {
            return -1;
        }
        unsigned q = (v0 << 18) | (v1 << 12) | (v2 << 6) | v3;
        out[o++] = (unsigned char)((q >> 16) & 0xFF);
        out[o++] = (unsigned char)((q >> 8) & 0xFF);
        out[o++] = (unsigned char)(q & 0xFF);
    }
    ptrdiff_t rem = len - i;
    if (rem == 2) {
        unsigned v0 = wreath_b64url_value[(unsigned char)in[i]];
        unsigned v1 = wreath_b64url_value[(unsigned char)in[i + 1]];
        if ((v0 | v1) > 63) {
            return -1;
        }
        out[o++] = (unsigned char)((((v0 << 18) | (v1 << 12)) >> 16) & 0xFF);
    }
    else if (rem == 3) {
        unsigned v0 = wreath_b64url_value[(unsigned char)in[i]];
        unsigned v1 = wreath_b64url_value[(unsigned char)in[i + 1]];
        unsigned v2 = wreath_b64url_value[(unsigned char)in[i + 2]];
        if ((v0 | v1 | v2) > 63) {
            return -1;
        }
        unsigned q = (v0 << 18) | (v1 << 12) | (v2 << 6);
        out[o++] = (unsigned char)((q >> 16) & 0xFF);
        out[o++] = (unsigned char)((q >> 8) & 0xFF);
    }
    return o;
}

#if defined(WREATH_HAVE_AVX2)
/* Thirty-two characters to twenty-four bytes per step.
 *
 * Validation and mapping both key off the two nibbles of each byte, looked up
 * with `vpshufb`. `mask[lo] & bitpos[hi]` is zero exactly for bytes outside
 * the alphabet -- the tables are derived from the alphabet itself, not written
 * by hand, and `tests/test_native_simd.py` crosses this arm against the scalar
 * one on every byte value. Bytes >= 0x80 land on a zero entry in `bitpos` and
 * so are rejected with everything else.
 *
 * The value of a character is itself plus a shift chosen by its high nibble:
 * one shift per nibble, except that 'P'-'Z' and '_' share nibble 5 and need
 * different ones, so '_' is blended in separately.
 *
 * A block containing anything invalid stops the vector loop and hands the
 * remainder to the scalar decoder, which reports precisely where it failed.
 */
WREATH_TARGET_AVX2 static inline ptrdiff_t
wreath_b64url_decode_avx2(const char *in, ptrdiff_t len, unsigned char *out)
{
    const __m256i nibble = _mm256_set1_epi8(0x0F);
    const __m256i bitpos = _mm256_setr_epi8(
        1, 2, 4, 8, 16, 32, 64, -128, 0, 0, 0, 0, 0, 0, 0, 0,
        1, 2, 4, 8, 16, 32, 64, -128, 0, 0, 0, 0, 0, 0, 0, 0);
    const __m256i mask_lut = _mm256_setr_epi8(
        0xA8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF0, 0x50, 0x50, 0x54, 0x50, 0x70, 0xA8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF8, 0xF0, 0x50, 0x50, 0x54, 0x50, 0x70);
    const __m256i shift_lut = _mm256_setr_epi8(
        0, 0, 17, 4, -65, -65, -71, -71, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 17, 4, -65, -65, -71, -71, 0, 0, 0, 0, 0, 0, 0, 0);
    const __m256i underscore = _mm256_set1_epi8('_');
    const __m256i pack = _mm256_setr_epi8(
        2, 1, 0, 6, 5, 4, 10, 9, 8, 14, 13, 12, -1, -1, -1, -1,
        2, 1, 0, 6, 5, 4, 10, 9, 8, 14, 13, 12, -1, -1, -1, -1);
    const __m256i gather = _mm256_setr_epi32(0, 1, 2, 4, 5, 6, 7, 7);

    ptrdiff_t i = 0;
    ptrdiff_t o = 0;
    if (len % 4 == 1) {
        return -1;
    }
    while (len - i >= 32) {
        __m256i v = _mm256_loadu_si256((const __m256i *)(const void *)(in + i));
        __m256i hi = _mm256_and_si256(_mm256_srli_epi32(v, 4), nibble);
        __m256i lo = _mm256_and_si256(v, nibble);
        __m256i allowed = _mm256_and_si256(_mm256_shuffle_epi8(mask_lut, lo),
                                           _mm256_shuffle_epi8(bitpos, hi));
        if (_mm256_movemask_epi8(
                _mm256_cmpeq_epi8(allowed, _mm256_setzero_si256())) != 0) {
            break;  /* something outside the alphabet: let scalar place the blame */
        }
        __m256i shift = _mm256_blendv_epi8(_mm256_shuffle_epi8(shift_lut, hi),
                                           _mm256_set1_epi8(-32),
                                           _mm256_cmpeq_epi8(v, underscore));
        __m256i values = _mm256_add_epi8(v, shift);
        /* 4x6 bits -> 3 bytes: pair the sextets into twelve-bit halves, then
         * the halves into one 24-bit value per dword. */
        __m256i merged = _mm256_madd_epi16(
            _mm256_maddubs_epi16(values, _mm256_set1_epi32(0x01400140)),
            _mm256_set1_epi32(0x00011000));
        __m256i packed = _mm256_permutevar8x32_epi32(
            _mm256_shuffle_epi8(merged, pack), gather);
        /* Twenty-four bytes exactly: the caller sizes `out` from the input
         * length and a 32-byte store would run past it. */
        _mm_storeu_si128((__m128i *)(void *)(out + o), _mm256_castsi256_si128(packed));
        _mm_storel_epi64((__m128i *)(void *)(out + o + 16),
                         _mm256_extracti128_si256(packed, 1));
        i += 32;
        o += 24;
    }
    ptrdiff_t rest = wreath_b64url_decode_scalar(in + i, len - i, out + o);
    if (rest < 0) {
        return -1;
    }
    return o + rest;
}
#endif

static inline ptrdiff_t
wreath_b64url_decode(const char *in, ptrdiff_t len, unsigned char *out)
{
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        return wreath_b64url_decode_avx2(in, len, out);
    }
#endif
    return wreath_b64url_decode_scalar(in, len, out);
}

/* --- base64 encoding ------------------------------------------------------
 *
 * Encoding is not on the CSRF path in any meaningful way: `b64url_32` costs
 * 28.6ns for its 32 bytes, twice per token, against a token that costs 2433ns.
 * This exists for the *large* payloads -- a WebSocket room broadcast, a bytes
 * field in a response body -- where CPython's `base64.b64encode` runs a scalar
 * table loop at roughly 0.5 bytes/ns.
 */

/* Trailing two characters are all that separates the two alphabets. */
#define WREATH_B64_STD 0
#define WREATH_B64_URL 1

static inline const char *
wreath_b64_alphabet(int urlsafe)
{
    return urlsafe
        ? "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        : "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
}

/* Encodes `len` bytes as `pad ? padded : unpadded` base64. Returns the number
 * of characters written; `out` must hold at least ((len + 2) / 3) * 4. */
static inline ptrdiff_t
wreath_b64_encode_scalar(const unsigned char *in, ptrdiff_t len, char *out,
                         int urlsafe, int pad)
{
    const char *alpha = wreath_b64_alphabet(urlsafe);
    ptrdiff_t i = 0;
    ptrdiff_t o = 0;
    for (; i + 3 <= len; i += 3) {
        unsigned t = ((unsigned)in[i] << 16) | ((unsigned)in[i + 1] << 8) | in[i + 2];
        out[o++] = alpha[(t >> 18) & 0x3F];
        out[o++] = alpha[(t >> 12) & 0x3F];
        out[o++] = alpha[(t >> 6) & 0x3F];
        out[o++] = alpha[t & 0x3F];
    }
    ptrdiff_t rem = len - i;
    if (rem == 1) {
        unsigned t = (unsigned)in[i] << 16;
        out[o++] = alpha[(t >> 18) & 0x3F];
        out[o++] = alpha[(t >> 12) & 0x3F];
        if (pad) {
            out[o++] = '=';
            out[o++] = '=';
        }
    }
    else if (rem == 2) {
        unsigned t = ((unsigned)in[i] << 16) | ((unsigned)in[i + 1] << 8);
        out[o++] = alpha[(t >> 18) & 0x3F];
        out[o++] = alpha[(t >> 12) & 0x3F];
        out[o++] = alpha[(t >> 6) & 0x3F];
        if (pad) {
            out[o++] = '=';
        }
    }
    return o;
}

#if defined(WREATH_HAVE_AVX2)
/* Twenty-four bytes to thirty-two characters per step.
 *
 * Each three bytes become four six-bit fields, extracted with one `and` and a
 * multiply per pair rather than by shifting each field out; the six-bit values
 * are then turned into characters by adding an offset chosen from a five-entry
 * table, which is the only place the two alphabets differ.
 *
 * The loop runs while 32 input bytes remain, not 24: the load reads a little
 * past the group it consumes, and stopping early keeps that read inside the
 * caller's buffer. The tail goes to the scalar encoder, which also owns
 * padding.
 */
WREATH_TARGET_AVX2 static inline ptrdiff_t
wreath_b64_encode_avx2(const unsigned char *in, ptrdiff_t len, char *out,
                       int urlsafe, int pad)
{
    const __m256i shuf = _mm256_setr_epi8(
        1, 0, 2, 1, 4, 3, 5, 4, 7, 6, 8, 7, 10, 9, 11, 10,
        1, 0, 2, 1, 4, 3, 5, 4, 7, 6, 8, 7, 10, 9, 11, 10);
    /* Indexed by the bucket computed below, not by the sextet: bucket 0 is
     * 26-51, buckets 1-10 are 52-61, 11 and 12 are the two alphabet-specific
     * characters, and 13 is 0-25. Transcribing this table in sextet order
     * instead produced 736k differential failures on the first run. */
    const __m256i offsets = urlsafe
        ? _mm256_setr_epi8(71, -4, -4, -4, -4, -4, -4, -4, -4, -4, -4, -17, 32, 65, 0, 0,
                           71, -4, -4, -4, -4, -4, -4, -4, -4, -4, -4, -17, 32, 65, 0, 0)
        : _mm256_setr_epi8(71, -4, -4, -4, -4, -4, -4, -4, -4, -4, -4, -19, -16, 65, 0, 0,
                           71, -4, -4, -4, -4, -4, -4, -4, -4, -4, -4, -19, -16, 65, 0, 0);
    ptrdiff_t i = 0;
    ptrdiff_t o = 0;
    while (len - i >= 32) {
        __m256i v = _mm256_set_m128i(
            _mm_loadu_si128((const __m128i *)(const void *)(in + i + 12)),
            _mm_loadu_si128((const __m128i *)(const void *)(in + i)));
        v = _mm256_shuffle_epi8(v, shuf);
        __m256i hi = _mm256_mulhi_epu16(_mm256_and_si256(v, _mm256_set1_epi32(0x0fc0fc00)),
                                        _mm256_set1_epi32(0x04000040));
        __m256i lo = _mm256_mullo_epi16(_mm256_and_si256(v, _mm256_set1_epi32(0x003f03f0)),
                                        _mm256_set1_epi32(0x01000010));
        __m256i sextets = _mm256_or_si256(hi, lo);
        /* Bucket each sextet into one of five ranges, then add that range's
         * offset. `subs_epu8` saturates everything below 51 to zero, and the
         * compare separates 0-25 from 26-51. */
        __m256i bucket = _mm256_subs_epu8(sextets, _mm256_set1_epi8(51));
        __m256i under26 = _mm256_cmpgt_epi8(_mm256_set1_epi8(26), sextets);
        bucket = _mm256_or_si256(bucket, _mm256_and_si256(under26, _mm256_set1_epi8(13)));
        _mm256_storeu_si256((__m256i *)(void *)(out + o),
                            _mm256_add_epi8(sextets, _mm256_shuffle_epi8(offsets, bucket)));
        i += 24;
        o += 32;
    }
    return o + wreath_b64_encode_scalar(in + i, len - i, out + o, urlsafe, pad);
}
#endif

static inline ptrdiff_t
wreath_b64_encode(const unsigned char *in, ptrdiff_t len, char *out,
                  int urlsafe, int pad)
{
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        return wreath_b64_encode_avx2(in, len, out, urlsafe, pad);
    }
#endif
    return wreath_b64_encode_scalar(in, len, out, urlsafe, pad);
}

/* ======================================================================== */
/* Hex decoding: PostgreSQL sends every `bytea` in text format as hex.        */
/* ======================================================================== */

/* Nibble value per byte, 0xFF for anything that is not a hex digit. Both
 * cases are accepted; PostgreSQL emits lower. One table rather than a value
 * table beside a validity table, for the same reason base64 needed only one:
 * with invalid marked 0xFF, a pair of digits is checked with a single OR. */
static const unsigned char wreath_hex_value[256] = {
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
};

/* Decodes `len` hex characters into `len / 2` bytes. Returns the byte count,
 * or -1 for an odd length or any non-hex byte. */
static inline ptrdiff_t
wreath_hex_decode_scalar(const char *in, ptrdiff_t len, unsigned char *out)
{
    if ((len & 1) != 0) {
        return -1;
    }
    for (ptrdiff_t i = 0; i < len; i += 2) {
        unsigned hi = wreath_hex_value[(unsigned char)in[i]];
        unsigned lo = wreath_hex_value[(unsigned char)in[i + 1]];
        if ((hi | lo) > 15) {
            return -1;
        }
        out[i >> 1] = (unsigned char)((hi << 4) | lo);
    }
    return len >> 1;
}

#if defined(WREATH_HAVE_AVX2)
/* Thirty-two characters to sixteen bytes per step.
 *
 * A digit and a letter are separated arithmetically rather than by table:
 * `c - '0'` is at most 9 for a digit, and `(c | 0x20) - 'a'` is at most 5 for
 * a letter, both tested as unsigned with `min`. Anything that is neither
 * fails both and stops the vector loop, leaving the scalar decoder to report
 * where. `maddubs` then folds each high/low nibble pair into one byte.
 */
WREATH_TARGET_AVX2 static inline ptrdiff_t
wreath_hex_decode_avx2(const char *in, ptrdiff_t len, unsigned char *out)
{
    const __m256i zero_digit = _mm256_set1_epi8('0');
    const __m256i nine = _mm256_set1_epi8(9);
    const __m256i five = _mm256_set1_epi8(5);
    const __m256i lower = _mm256_set1_epi8(0x20);
    const __m256i letter_a = _mm256_set1_epi8('a');
    const __m256i ten = _mm256_set1_epi8(10);
    const __m256i gather = _mm256_setr_epi8(
        0, 2, 4, 6, 8, 10, 12, 14, -1, -1, -1, -1, -1, -1, -1, -1,
        0, 2, 4, 6, 8, 10, 12, 14, -1, -1, -1, -1, -1, -1, -1, -1);
    if ((len & 1) != 0) {
        return -1;
    }
    ptrdiff_t i = 0;
    ptrdiff_t o = 0;
    while (len - i >= 32) {
        __m256i v = _mm256_loadu_si256((const __m256i *)(const void *)(in + i));
        __m256i digit = _mm256_sub_epi8(v, zero_digit);
        __m256i is_digit = _mm256_cmpeq_epi8(_mm256_min_epu8(digit, nine), digit);
        __m256i alpha = _mm256_sub_epi8(_mm256_or_si256(v, lower), letter_a);
        __m256i is_alpha = _mm256_cmpeq_epi8(_mm256_min_epu8(alpha, five), alpha);
        if (_mm256_movemask_epi8(_mm256_or_si256(is_digit, is_alpha)) != -1) {
            break;  /* not a hex digit somewhere in this block */
        }
        __m256i nibbles = _mm256_blendv_epi8(_mm256_add_epi8(alpha, ten), digit, is_digit);
        /* (high << 4) | low for each adjacent pair, into 16-bit lanes. */
        __m256i bytes = _mm256_maddubs_epi16(nibbles, _mm256_set1_epi16(0x0110));
        __m256i packed = _mm256_permutevar8x32_epi32(
            _mm256_shuffle_epi8(bytes, gather),
            _mm256_setr_epi32(0, 1, 4, 5, 2, 3, 6, 7));
        _mm_storeu_si128((__m128i *)(void *)(out + o), _mm256_castsi256_si128(packed));
        i += 32;
        o += 16;
    }
    ptrdiff_t rest = wreath_hex_decode_scalar(in + i, len - i, out + o);
    if (rest < 0) {
        return -1;
    }
    return o + rest;
}
#endif

static inline ptrdiff_t
wreath_hex_decode(const char *in, ptrdiff_t len, unsigned char *out)
{
#if defined(WREATH_HAVE_AVX2)
    if (len >= 32 && wreath_simd_has_avx2()) {
        return wreath_hex_decode_avx2(in, len, out);
    }
#endif
    return wreath_hex_decode_scalar(in, len, out);
}

#endif /* WREATH_SIMD_H */

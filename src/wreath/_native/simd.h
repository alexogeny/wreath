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

#endif /* WREATH_SIMD_H */

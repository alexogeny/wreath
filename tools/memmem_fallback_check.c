/* Diagnostic build: validate and time the portable wreath_memmem fallback.
 *
 * The release build on Linux/macOS/BSD binds wreath_memmem to libc memmem, so the
 * portable two-way path is never executed there and no test running against a
 * normal build can validate it. This harness forces the fallback with
 * WREATH_FORCE_PORTABLE_MEMMEM and exercises it directly.
 *
 * Build and run (needs only CPython headers; nothing from libpython is called):
 *
 *   cc -O2 $(python3-config --includes) tools/memmem_fallback_check.c -o /tmp/memmem
 *   /tmp/memmem            # exhaustive + repetitive correctness, then scaling
 *
 * Exit status is 0 only when every case matches the reference.
 */
#define WREATH_FORCE_PORTABLE_MEMMEM 1
#include "../src/wreath/_native/wreathcore.h"

#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#if WREATH_MEMMEM_USES_LIBC
#error "the fallback was not selected; WREATH_FORCE_PORTABLE_MEMMEM had no effect"
#endif

static long failures = 0;
static long checks = 0;

/* Obviously-correct reference: the definition of substring search. */
static const uint8_t *
reference_search(const uint8_t *hay, Py_ssize_t hay_len, const uint8_t *needle,
                 Py_ssize_t needle_len)
{
    if (needle_len <= 0 || hay_len < needle_len) {
        return NULL;
    }
    for (Py_ssize_t i = 0; i + needle_len <= hay_len; i++) {
        Py_ssize_t j = 0;
        while (j < needle_len && hay[i + j] == needle[j]) {
            j++;
        }
        if (j == needle_len) {
            return hay + i;
        }
    }
    return NULL;
}

static void
check(const uint8_t *hay, Py_ssize_t hay_len, const uint8_t *needle, Py_ssize_t needle_len)
{
    const uint8_t *got = wreath_memmem(hay, hay_len, needle, needle_len);
    const uint8_t *want = reference_search(hay, hay_len, needle, needle_len);
    checks++;
    if (got != want) {
        failures++;
        if (failures <= 10) {
            fprintf(stderr, "MISMATCH hay=\"%.*s\" needle=\"%.*s\" got=%ld want=%ld\n",
                    (int)hay_len, (const char *)hay, (int)needle_len, (const char *)needle,
                    got ? (long)(got - hay) : -1L, want ? (long)(want - hay) : -1L);
        }
    }
}

/* Every string over a k-letter alphabet of the given length, as a counter. */
static int
next_string(uint8_t *buf, Py_ssize_t len, int alphabet)
{
    for (Py_ssize_t i = len - 1; i >= 0; i--) {
        if (buf[i] - 'a' + 1 < alphabet) {
            buf[i]++;
            return 1;
        }
        buf[i] = 'a';
    }
    return 0;
}

/* Exhaustive over a tiny alphabet: this is where periodicity, overlapping
 * prefixes, and the critical-factorization edge cases all live. */
static void
exhaustive(int alphabet, Py_ssize_t max_hay, Py_ssize_t max_needle)
{
    uint8_t hay[16], needle[16];
    for (Py_ssize_t hl = 1; hl <= max_hay; hl++) {
        for (Py_ssize_t i = 0; i < hl; i++) {
            hay[i] = 'a';
        }
        do {
            for (Py_ssize_t nl = 1; nl <= max_needle && nl <= hl; nl++) {
                for (Py_ssize_t i = 0; i < nl; i++) {
                    needle[i] = 'a';
                }
                do {
                    check(hay, hl, needle, nl);
                } while (next_string(needle, nl, alphabet));
            }
        } while (next_string(hay, hl, alphabet));
    }
}

static void
repetitive(void)
{
    /* Highly repetitive haystack with overlapping needle prefixes: the shape
     * that makes a naive scan quadratic. */
    static uint8_t hay[4096];
    static uint8_t needle[64];
    for (int period = 1; period <= 5; period++) {
        for (size_t i = 0; i < sizeof(hay); i++) {
            hay[i] = (uint8_t)('a' + (i % period));
        }
        for (Py_ssize_t nl = 1; nl <= 40; nl++) {
            for (Py_ssize_t i = 0; i < nl; i++) {
                needle[i] = (uint8_t)('a' + (i % period));
            }
            check(hay, (Py_ssize_t)sizeof(hay), needle, nl);
            /* Same needle, last byte broken: never matches, worst case. */
            needle[nl - 1] = 'z';
            check(hay, (Py_ssize_t)sizeof(hay), needle, nl);
            /* A match placed only at the very end. */
            for (Py_ssize_t i = 0; i < nl; i++) {
                needle[i] = (uint8_t)('a' + (i % period));
            }
            memcpy(hay + sizeof(hay) - nl, needle, (size_t)nl);
            check(hay, (Py_ssize_t)sizeof(hay), needle, nl);
            for (size_t i = 0; i < sizeof(hay); i++) {
                hay[i] = (uint8_t)('a' + (i % period));
            }
        }
    }

    /* A realistic multipart delimiter: 74 bytes, absent from the haystack. */
    static uint8_t big[1 << 16];
    uint8_t delim[74];
    memset(big, 'a', sizeof(big));
    memset(delim, 'a', sizeof(delim));
    delim[73] = 'b';
    check(big, (Py_ssize_t)sizeof(big), delim, 74);
}

static double
elapsed_ns_per_byte(Py_ssize_t hay_len, Py_ssize_t needle_len, int reps)
{
    uint8_t *hay = malloc((size_t)hay_len);
    uint8_t *needle = malloc((size_t)needle_len);
    memset(hay, 'a', (size_t)hay_len);
    memset(needle, 'a', (size_t)needle_len);
    needle[needle_len - 1] = 'b';  /* forces the full scan, never matches */
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (int i = 0; i < reps; i++) {
        const uint8_t *r = wreath_memmem(hay, hay_len, needle, needle_len);
        if (r != NULL) {
            abort();
        }
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    double ns = (double)(t1.tv_sec - t0.tv_sec) * 1e9 + (double)(t1.tv_nsec - t0.tv_nsec);
    free(hay);
    free(needle);
    return ns / (double)reps / (double)hay_len;
}

int
main(void)
{
    exhaustive(2, 12, 12);   /* binary alphabet: maximal periodicity */
    exhaustive(3, 8, 8);
    repetitive();
    printf("correctness: %ld checks, %ld failures\n", checks, failures);
    if (failures != 0) {
        return 1;
    }

    /* Worst case for the naive scan: every haystack byte matches needle[0].
     * A linear search holds ns/byte roughly flat as the haystack grows. Measure
     * each needle independently: lengths through WREATH_SIMD_NEEDLE_MAX use
     * the vector path while longer needles use two-way, and those algorithms
     * intentionally have different constant factors. */
    printf("scaling (repetitive haystack, non-matching needle):\n");
    const Py_ssize_t needles[] = {4, 32, 64};
    for (size_t ni = 0; ni < sizeof(needles) / sizeof(needles[0]); ni++) {
        for (Py_ssize_t hl = 1 << 16; hl <= 1 << 20; hl *= 4) {
            int reps = (int)(20 * ((1 << 20) / hl));
            double per_byte = elapsed_ns_per_byte(hl, needles[ni], reps);
            printf("  needle=%ld haystack=%ld ns_per_haystack_byte=%.4f\n",
                   (long)needles[ni], (long)hl, per_byte);
        }
    }
    return 0;
}

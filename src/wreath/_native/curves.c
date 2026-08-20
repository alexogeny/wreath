/* Fixed-width arithmetic for edwards25519 and NIST P-256. */
#include "wreathcore.h"

#include <stdint.h>
#include <string.h>

#define CURVE_BYTES 32
#define PY_U256_FLAGS (Py_ASNATIVEBYTES_LITTLE_ENDIAN | \
                       Py_ASNATIVEBYTES_UNSIGNED_BUFFER | \
                       Py_ASNATIVEBYTES_REJECT_NEGATIVE)

#if defined(__SIZEOF_INT128__) && !defined(WREATH_CURVE_FORCE_32)
#define WREATH_CURVE_HAVE_INT128 1
typedef uint64_t CurveWord;
typedef __uint128_t CurveWide;
#define CURVE_LIMBS 4
#define CURVE_WORD_BITS 64
#define CURVE_WORD_BYTES 8
#define CURVE_WIDE_SHIFT 64
#else
typedef uint32_t CurveWord;
typedef uint64_t CurveWide;
#define CURVE_LIMBS 8
#define CURVE_WORD_BITS 32
#define CURVE_WORD_BYTES 4
#define CURVE_WIDE_SHIFT 32
#endif

typedef struct {
    uint64_t state[8];
    uint64_t total;
    size_t used;
    unsigned char block[128];
} CurveSha512;

static const uint64_t SHA512_CONSTANTS[80] = {
    UINT64_C(0x428a2f98d728ae22), UINT64_C(0x7137449123ef65cd),
    UINT64_C(0xb5c0fbcfec4d3b2f), UINT64_C(0xe9b5dba58189dbbc),
    UINT64_C(0x3956c25bf348b538), UINT64_C(0x59f111f1b605d019),
    UINT64_C(0x923f82a4af194f9b), UINT64_C(0xab1c5ed5da6d8118),
    UINT64_C(0xd807aa98a3030242), UINT64_C(0x12835b0145706fbe),
    UINT64_C(0x243185be4ee4b28c), UINT64_C(0x550c7dc3d5ffb4e2),
    UINT64_C(0x72be5d74f27b896f), UINT64_C(0x80deb1fe3b1696b1),
    UINT64_C(0x9bdc06a725c71235), UINT64_C(0xc19bf174cf692694),
    UINT64_C(0xe49b69c19ef14ad2), UINT64_C(0xefbe4786384f25e3),
    UINT64_C(0x0fc19dc68b8cd5b5), UINT64_C(0x240ca1cc77ac9c65),
    UINT64_C(0x2de92c6f592b0275), UINT64_C(0x4a7484aa6ea6e483),
    UINT64_C(0x5cb0a9dcbd41fbd4), UINT64_C(0x76f988da831153b5),
    UINT64_C(0x983e5152ee66dfab), UINT64_C(0xa831c66d2db43210),
    UINT64_C(0xb00327c898fb213f), UINT64_C(0xbf597fc7beef0ee4),
    UINT64_C(0xc6e00bf33da88fc2), UINT64_C(0xd5a79147930aa725),
    UINT64_C(0x06ca6351e003826f), UINT64_C(0x142929670a0e6e70),
    UINT64_C(0x27b70a8546d22ffc), UINT64_C(0x2e1b21385c26c926),
    UINT64_C(0x4d2c6dfc5ac42aed), UINT64_C(0x53380d139d95b3df),
    UINT64_C(0x650a73548baf63de), UINT64_C(0x766a0abb3c77b2a8),
    UINT64_C(0x81c2c92e47edaee6), UINT64_C(0x92722c851482353b),
    UINT64_C(0xa2bfe8a14cf10364), UINT64_C(0xa81a664bbc423001),
    UINT64_C(0xc24b8b70d0f89791), UINT64_C(0xc76c51a30654be30),
    UINT64_C(0xd192e819d6ef5218), UINT64_C(0xd69906245565a910),
    UINT64_C(0xf40e35855771202a), UINT64_C(0x106aa07032bbd1b8),
    UINT64_C(0x19a4c116b8d2d0c8), UINT64_C(0x1e376c085141ab53),
    UINT64_C(0x2748774cdf8eeb99), UINT64_C(0x34b0bcb5e19b48a8),
    UINT64_C(0x391c0cb3c5c95a63), UINT64_C(0x4ed8aa4ae3418acb),
    UINT64_C(0x5b9cca4f7763e373), UINT64_C(0x682e6ff3d6b2b8a3),
    UINT64_C(0x748f82ee5defb2fc), UINT64_C(0x78a5636f43172f60),
    UINT64_C(0x84c87814a1f0ab72), UINT64_C(0x8cc702081a6439ec),
    UINT64_C(0x90befffa23631e28), UINT64_C(0xa4506cebde82bde9),
    UINT64_C(0xbef9a3f7b2c67915), UINT64_C(0xc67178f2e372532b),
    UINT64_C(0xca273eceea26619c), UINT64_C(0xd186b8c721c0c207),
    UINT64_C(0xeada7dd6cde0eb1e), UINT64_C(0xf57d4f7fee6ed178),
    UINT64_C(0x06f067aa72176fba), UINT64_C(0x0a637dc5a2c898a6),
    UINT64_C(0x113f9804bef90dae), UINT64_C(0x1b710b35131c471b),
    UINT64_C(0x28db77f523047d84), UINT64_C(0x32caab7b40c72493),
    UINT64_C(0x3c9ebe0a15c9bebc), UINT64_C(0x431d67c49c100d4c),
    UINT64_C(0x4cc5d4becb3e42b6), UINT64_C(0x597f299cfc657e2a),
    UINT64_C(0x5fcb6fab3ad6faec), UINT64_C(0x6c44198c4a475817)
};

static uint64_t
sha512_rotate(uint64_t value, unsigned shift)
{
    return (value >> shift) | (value << (64 - shift));
}

static uint64_t
sha512_load(const unsigned char *input)
{
    uint64_t value = 0;
    for (int index = 0; index < 8; index++)
        value = (value << 8) | input[index];
    return value;
}

static void
sha512_store(unsigned char *output, uint64_t value)
{
    for (int index = 7; index >= 0; index--) {
        output[index] = (unsigned char)value;
        value >>= 8;
    }
}

static void
sha512_transform(CurveSha512 *context, const unsigned char block[128])
{
    uint64_t schedule[80];
    uint64_t a, b, c, d, e, f, g, h;
    for (int index = 0; index < 16; index++)
        schedule[index] = sha512_load(block + index * 8);
    for (int index = 16; index < 80; index++) {
        uint64_t first = sha512_rotate(schedule[index - 15], 1) ^
                         sha512_rotate(schedule[index - 15], 8) ^
                         (schedule[index - 15] >> 7);
        uint64_t second = sha512_rotate(schedule[index - 2], 19) ^
                          sha512_rotate(schedule[index - 2], 61) ^
                          (schedule[index - 2] >> 6);
        schedule[index] = schedule[index - 16] + first +
                          schedule[index - 7] + second;
    }
    a = context->state[0]; b = context->state[1];
    c = context->state[2]; d = context->state[3];
    e = context->state[4]; f = context->state[5];
    g = context->state[6]; h = context->state[7];
    for (int index = 0; index < 80; index++) {
        uint64_t sigma1 = sha512_rotate(e, 14) ^ sha512_rotate(e, 18) ^
                          sha512_rotate(e, 41);
        uint64_t choice = (e & f) ^ ((~e) & g);
        uint64_t first = h + sigma1 + choice + SHA512_CONSTANTS[index] +
                         schedule[index];
        uint64_t sigma0 = sha512_rotate(a, 28) ^ sha512_rotate(a, 34) ^
                          sha512_rotate(a, 39);
        uint64_t majority = (a & b) ^ (a & c) ^ (b & c);
        uint64_t second = sigma0 + majority;
        h = g; g = f; f = e; e = d + first;
        d = c; c = b; b = a; a = first + second;
    }
    context->state[0] += a; context->state[1] += b;
    context->state[2] += c; context->state[3] += d;
    context->state[4] += e; context->state[5] += f;
    context->state[6] += g; context->state[7] += h;
}

static void
sha512_init(CurveSha512 *context)
{
    static const uint64_t initial[8] = {
        UINT64_C(0x6a09e667f3bcc908), UINT64_C(0xbb67ae8584caa73b),
        UINT64_C(0x3c6ef372fe94f82b), UINT64_C(0xa54ff53a5f1d36f1),
        UINT64_C(0x510e527fade682d1), UINT64_C(0x9b05688c2b3e6c1f),
        UINT64_C(0x1f83d9abfb41bd6b), UINT64_C(0x5be0cd19137e2179)
    };
    memcpy(context->state, initial, sizeof(initial));
    context->total = 0;
    context->used = 0;
}

static void
sha512_update(CurveSha512 *context, const unsigned char *data, size_t length)
{
    context->total += (uint64_t)length;
    if (context->used != 0) {
        size_t take = 128 - context->used;
        if (take > length) take = length;
        memcpy(context->block + context->used, data, take);
        context->used += take;
        data += take;
        length -= take;
        if (context->used == 128) {
            sha512_transform(context, context->block);
            context->used = 0;
        }
    }
    while (length >= 128) {
        sha512_transform(context, data);
        data += 128;
        length -= 128;
    }
    if (length != 0) {
        memcpy(context->block, data, length);
        context->used = length;
    }
}

static void
sha512_final(CurveSha512 *context, unsigned char digest[64])
{
    uint64_t high_bits = context->total >> 61;
    uint64_t low_bits = context->total << 3;
    context->block[context->used++] = 0x80;
    if (context->used > 112) {
        memset(context->block + context->used, 0, 128 - context->used);
        sha512_transform(context, context->block);
        context->used = 0;
    }
    memset(context->block + context->used, 0, 112 - context->used);
    sha512_store(context->block + 112, high_bits);
    sha512_store(context->block + 120, low_bits);
    sha512_transform(context, context->block);
    for (int index = 0; index < 8; index++)
        sha512_store(digest + index * 8, context->state[index]);
}

static void
curve_secure_zero(void *memory, size_t length)
{
    volatile unsigned char *bytes = memory;
    while (length-- != 0) *bytes++ = 0;
}

typedef struct {
    CurveWord limb[CURVE_LIMBS];
} U256;

#if defined(WREATH_CURVE_HAVE_INT128)
typedef struct {
    uint64_t limb[5];
} EdFe;
#else
typedef U256 EdFe;
#endif

typedef struct {
    U256 n;
    U256 r2;
    U256 one;
    CurveWord n0;
} Modulus;

typedef struct {
    EdFe x;
    EdFe y;
    EdFe z;
    EdFe t;
} EdPoint;

typedef struct {
    U256 x;
    U256 y;
    U256 z;
} P256Point;

static const U256 U256_ZERO = {{0}};
static const U256 U256_ONE = {{1}};

#if defined(WREATH_CURVE_HAVE_INT128)

static const Modulus ED_FIELD = {
    {{UINT64_C(0xffffffffffffffed), UINT64_C(0xffffffffffffffff),
      UINT64_C(0xffffffffffffffff), UINT64_C(0x7fffffffffffffff)}},
    {{UINT64_C(0x00000000000005a4), 0, 0, 0}},
    {{UINT64_C(0x0000000000000026), 0, 0, 0}},
    UINT64_C(0x86bca1af286bca1b)
};

static const U256 ED_ORDER = {{
    UINT64_C(0x5812631a5cf5d3ed), UINT64_C(0x14def9dea2f79cd6),
    UINT64_C(0x0000000000000000), UINT64_C(0x1000000000000000)
}};
static const EdFe ED_D = {{
    UINT64_C(0x34dca135978a3), UINT64_C(0x1a8283b156ebd),
    UINT64_C(0x5e7a26001c029), UINT64_C(0x739c663a03cbb),
    UINT64_C(0x52036cee2b6ff)
}};
static const EdFe ED_SQRT_M1 = {{
    UINT64_C(0x61b274a0ea0b0), UINT64_C(0x0d5a5fc8f189d),
    UINT64_C(0x7ef5e9cbd0c60), UINT64_C(0x78595a6804c9e),
    UINT64_C(0x2b8324804fc1d)
}};
static const EdPoint ED_BASE_POINT = {
    {{UINT64_C(0x62d608f25d51a), UINT64_C(0x412a4b4f6592a),
      UINT64_C(0x75b7171a4b31d), UINT64_C(0x1ff60527118fe),
      UINT64_C(0x216936d3cd6e5)}},
    {{UINT64_C(0x6666666666658), UINT64_C(0x4cccccccccccc),
      UINT64_C(0x1999999999999), UINT64_C(0x3333333333333),
      UINT64_C(0x6666666666666)}},
    {{1, 0, 0, 0, 0}},
    {{UINT64_C(0x68ab3a5b7dda3), UINT64_C(0x0eea2a5eadbb),
      UINT64_C(0x2af8df483c27e), UINT64_C(0x332b375274732),
      UINT64_C(0x67875f0fd78b7)}}
};

static const Modulus P256_FIELD = {
    {{UINT64_C(0xffffffffffffffff), UINT64_C(0x00000000ffffffff),
      UINT64_C(0x0000000000000000), UINT64_C(0xffffffff00000001)}},
    {{UINT64_C(0x0000000000000003), UINT64_C(0xfffffffbffffffff),
      UINT64_C(0xfffffffffffffffe), UINT64_C(0x00000004fffffffd)}},
    {{UINT64_C(0x0000000000000001), UINT64_C(0xffffffff00000000),
      UINT64_C(0xffffffffffffffff), UINT64_C(0x00000000fffffffe)}},
    UINT64_C(0x0000000000000001)
};
static const U256 P256_ORDER = {{
    UINT64_C(0xf3b9cac2fc632551), UINT64_C(0xbce6faada7179e84),
    UINT64_C(0xffffffffffffffff), UINT64_C(0xffffffff00000000)
}};
static const Modulus P256_SCALAR_FIELD = {
    {{UINT64_C(0xf3b9cac2fc632551), UINT64_C(0xbce6faada7179e84),
      UINT64_C(0xffffffffffffffff), UINT64_C(0xffffffff00000000)}},
    {{UINT64_C(0x83244c95be79eea2), UINT64_C(0x4699799c49bd6fa6),
      UINT64_C(0x2845b2392b6bec59), UINT64_C(0x66e12d94f3d95620)}},
    {{UINT64_C(0x0c46353d039cdaaf), UINT64_C(0x4319055258e8617b),
      UINT64_C(0), UINT64_C(0xffffffff)}},
    UINT64_C(0xccd1c8aaee00bc4f)
};
static const U256 P256_SCALAR_INVERSE_EXPONENT = {{
    UINT64_C(0xf3b9cac2fc63254f), UINT64_C(0xbce6faada7179e84),
    UINT64_C(0xffffffffffffffff), UINT64_C(0xffffffff00000000)
}};
static const U256 P256_INVERSE_EXPONENT = {{
    UINT64_C(0xfffffffffffffffd), UINT64_C(0x00000000ffffffff),
    UINT64_C(0x0000000000000000), UINT64_C(0xffffffff00000001)
}};
static const U256 P256_B = {{
    UINT64_C(0xd89cdf6229c4bddf), UINT64_C(0xacf005cd78843090),
    UINT64_C(0xe5a220abf7212ed6), UINT64_C(0xdc30061d04874834)
}};
static const U256 P256_GX = {{
    UINT64_C(0x79e730d418a9143c), UINT64_C(0x75ba95fc5fedb601),
    UINT64_C(0x79fb732b77622510), UINT64_C(0x18905f76a53755c6)
}};
static const U256 P256_GY = {{
    UINT64_C(0xddf25357ce95560a), UINT64_C(0x8b4ab8e4ba19e45c),
    UINT64_C(0xd2e88688dd21f325), UINT64_C(0x8571ff1825885d85)
}};

#else

static const Modulus ED_FIELD = {
    {{0xffffffedu, 0xffffffffu, 0xffffffffu, 0xffffffffu,
      0xffffffffu, 0xffffffffu, 0xffffffffu, 0x7fffffffu}},
    {{0x000005a4u, 0, 0, 0, 0, 0, 0, 0}},
    {{0x00000026u, 0, 0, 0, 0, 0, 0, 0}},
    0x286bca1bu
};

static const U256 ED_ORDER = {{
    0x5cf5d3edu, 0x5812631au, 0xa2f79cd6u, 0x14def9deu,
    0, 0, 0, 0x10000000u
}};

static const U256 ED_INVERSE_EXPONENT = {{
    0xffffffebu, 0xffffffffu, 0xffffffffu, 0xffffffffu,
    0xffffffffu, 0xffffffffu, 0xffffffffu, 0x7fffffffu
}};

static const U256 ED_POW22523_EXPONENT = {{
    0xfffffffdu, 0xffffffffu, 0xffffffffu, 0xffffffffu,
    0xffffffffu, 0xffffffffu, 0xffffffffu, 0x0fffffffu
}};

/* Montgomery encodings of d and sqrt(-1), derived from RFC 8032 values. */
static const U256 ED_D = {{
    0xdf47e9fau, 0x80ed8bfeu, 0xafc62973u, 0x10a18777u,
    0xbc188690u, 0xe5939207u, 0x729fc526u, 0x2c822b5au
}};
static const U256 ED_SQRT_M1 = {{
    0xfe2bdb04u, 0x3b5807d4u, 0xb51be9edu, 0x03f590fdu,
    0x336202d1u, 0x6d6e16bfu, 0xd6c71ba8u, 0x75776b0bu
}};
static const EdPoint ED_BASE_POINT = {
    {{0x3f9da287u, 0xe2cabc55u, 0x2396e489u, 0x9ca59856u,
      0xade4b5b7u, 0x9879936bu, 0x7e6077d0u, 0x759e2370u}},
    {{0x3333334au, 0x33333333u, 0x33333333u, 0x33333333u,
      0x33333333u, 0x33333333u, 0x33333333u, 0x33333333u}},
    {{0x00000026u, 0, 0, 0, 0, 0, 0, 0}},
    {{0x994ae86cu, 0x4f0896aau, 0xb612506eu, 0xe3b7ad11u,
      0xf183c492u, 0x46c7a922u, 0xfeb3930du, 0x5e181c59u}}
};

static const Modulus P256_FIELD = {
    {{0xffffffffu, 0xffffffffu, 0xffffffffu, 0x00000000u,
      0x00000000u, 0x00000000u, 0x00000001u, 0xffffffffu}},
    {{0x00000003u, 0x00000000u, 0xffffffffu, 0xfffffffbu,
      0xfffffffeu, 0xffffffffu, 0xfffffffdu, 0x00000004u}},
    {{0x00000001u, 0x00000000u, 0x00000000u, 0xffffffffu,
      0xffffffffu, 0xffffffffu, 0xfffffffeu, 0x00000000u}},
    0x00000001u
};

static const U256 P256_ORDER = {{
    0xfc632551u, 0xf3b9cac2u, 0xa7179e84u, 0xbce6faadu,
    0xffffffffu, 0xffffffffu, 0x00000000u, 0xffffffffu
}};
static const Modulus P256_SCALAR_FIELD = {
    {{0xfc632551u, 0xf3b9cac2u, 0xa7179e84u, 0xbce6faadu,
      0xffffffffu, 0xffffffffu, 0x00000000u, 0xffffffffu}},
    {{0xbe79eea2u, 0x83244c95u, 0x49bd6fa6u, 0x4699799cu,
      0x2b6bec59u, 0x2845b239u, 0xf3d95620u, 0x66e12d94u}},
    {{0x039cdaafu, 0x0c46353du, 0x58e8617bu, 0x43190552u,
      0, 0, 0xffffffffu, 0}},
    0xee00bc4fu
};
static const U256 P256_SCALAR_INVERSE_EXPONENT = {{
    0xfc63254fu, 0xf3b9cac2u, 0xa7179e84u, 0xbce6faadu,
    0xffffffffu, 0xffffffffu, 0x00000000u, 0xffffffffu
}};

static const U256 P256_INVERSE_EXPONENT = {{
    0xfffffffdu, 0xffffffffu, 0xffffffffu, 0x00000000u,
    0x00000000u, 0x00000000u, 0x00000001u, 0xffffffffu
}};

/* Montgomery encoding of the P-256 curve coefficient b. */
static const U256 P256_B = {{
    0x29c4bddfu, 0xd89cdf62u, 0x78843090u, 0xacf005cdu,
    0xf7212ed6u, 0xe5a220abu, 0x04874834u, 0xdc30061du
}};
static const U256 P256_GX = {{
    0x18a9143cu, 0x79e730d4u, 0x5fedb601u, 0x75ba95fcu,
    0x77622510u, 0x79fb732bu, 0xa53755c6u, 0x18905f76u
}};
static const U256 P256_GY = {{
    0xce95560au, 0xddf25357u, 0xba19e45cu, 0x8b4ab8e4u,
    0xdd21f325u, 0xd2e88688u, 0x25885d85u, 0x8571ff18u
}};

#endif

/* Canonical SEC 1 affine encodings of 1G..15G.  Keeping canonical bytes makes
 * one immutable table serve both the four-by-64 and eight-by-32 limb builds;
 * each signing operation converts its 30 coordinates to the local Montgomery
 * representation before constant-shape selection. */
static const unsigned char P256_BASE_WINDOW[15][64] = {
    /* 1G */ "\x6b\x17\xd1\xf2\xe1\x2c\x42\x47\xf8\xbc\xe6\xe5\x63\xa4\x40\xf2\x77\x03\x7d\x81\x2d\xeb\x33\xa0\xf4\xa1\x39\x45\xd8\x98\xc2\x96\x4f\xe3\x42\xe2\xfe\x1a\x7f\x9b\x8e\xe7\xeb\x4a\x7c\x0f\x9e\x16\x2b\xce\x33\x57\x6b\x31\x5e\xce\xcb\xb6\x40\x68\x37\xbf\x51\xf5",
    /* 2G */ "\x7c\xf2\x7b\x18\x8d\x03\x4f\x7e\x8a\x52\x38\x03\x04\xb5\x1a\xc3\xc0\x89\x69\xe2\x77\xf2\x1b\x35\xa6\x0b\x48\xfc\x47\x66\x99\x78\x07\x77\x55\x10\xdb\x8e\xd0\x40\x29\x3d\x9a\xc6\x9f\x74\x30\xdb\xba\x7d\xad\xe6\x3c\xe9\x82\x29\x9e\x04\xb7\x9d\x22\x78\x73\xd1",
    /* 3G */ "\x5e\xcb\xe4\xd1\xa6\x33\x0a\x44\xc8\xf7\xef\x95\x1d\x4b\xf1\x65\xe6\xc6\xb7\x21\xef\xad\xa9\x85\xfb\x41\x66\x1b\xc6\xe7\xfd\x6c\x87\x34\x64\x0c\x49\x98\xff\x7e\x37\x4b\x06\xce\x1a\x64\xa2\xec\xd8\x2a\xb0\x36\x38\x4f\xb8\x3d\x9a\x79\xb1\x27\xa2\x7d\x50\x32",
    /* 4G */ "\xe2\x53\x4a\x35\x32\xd0\x8f\xbb\xa0\x2d\xde\x65\x9e\xe6\x2b\xd0\x03\x1f\xe2\xdb\x78\x55\x96\xef\x50\x93\x02\x44\x6b\x03\x08\x52\xe0\xf1\x57\x5a\x4c\x63\x3c\xc7\x19\xdf\xee\x5f\xda\x86\x2d\x76\x4e\xfc\x96\xc3\xf3\x0e\xe0\x05\x5c\x42\xc2\x3f\x18\x4e\xd8\xc6",
    /* 5G */ "\x51\x59\x0b\x7a\x51\x51\x40\xd2\xd7\x84\xc8\x56\x08\x66\x8f\xdf\xef\x8c\x82\xfd\x1f\x5b\xe5\x24\x21\x55\x4a\x0d\xc3\xd0\x33\xed\xe0\xc1\x7d\xa8\x90\x4a\x72\x7d\x8a\xe1\xbf\x36\xbf\x8a\x79\x26\x0d\x01\x2f\x00\xd4\xd8\x08\x88\xd1\xd0\xbb\x44\xfd\xa1\x6d\xa4",
    /* 6G */ "\xb0\x1a\x17\x2a\x76\xa4\x60\x2c\x92\xd3\x24\x2c\xb8\x97\xdd\xe3\x02\x4c\x74\x0d\xeb\xb2\x15\xb4\xc6\xb0\xaa\xe9\x3c\x22\x91\xa9\xe8\x5c\x10\x74\x32\x37\xda\xd5\x6f\xec\x0e\x2d\xfb\xa7\x03\x79\x1c\x00\xf7\x70\x1c\x7e\x16\xbd\xfd\x7c\x48\x53\x8f\xc7\x7f\xe2",
    /* 7G */ "\x8e\x53\x3b\x6f\xa0\xbf\x7b\x46\x25\xbb\x30\x66\x7c\x01\xfb\x60\x7e\xf9\xf8\xb8\xa8\x0f\xef\x5b\x30\x06\x28\x70\x31\x87\xb2\xa3\x73\xeb\x1d\xbd\xe0\x33\x18\x36\x6d\x06\x9f\x83\xa6\xf5\x90\x00\x53\xc7\x36\x33\xcb\x04\x1b\x21\xc5\x5e\x1a\x86\xc1\xf4\x00\xb4",
    /* 8G */ "\x62\xd9\x77\x9d\xbe\xe9\xb0\x53\x40\x42\x74\x2d\x3a\xb5\x4c\xad\xc1\xd2\x38\x98\x0f\xce\x97\xdb\xb4\xdd\x9d\xc1\xdb\x6f\xb3\x93\xad\x5a\xcc\xbd\x91\xe9\xd8\x24\x4f\xf1\x5d\x77\x11\x67\xce\xe0\xa2\xed\x51\xf6\xbb\xe7\x6a\x78\xda\x54\x0a\x6a\x0f\x09\x95\x7e",
    /* 9G */ "\xea\x68\xd7\xb6\xfe\xdf\x0b\x71\x87\x89\x38\xd5\x1d\x71\xf8\x72\x9e\x0a\xcb\x8c\x2c\x6d\xf8\xb3\xd7\x9e\x8a\x4b\x90\x94\x9e\xe0\x2a\x27\x44\xc9\x72\xc9\xfc\xe7\x87\x01\x4a\x96\x4a\x8e\xa0\xc8\x4d\x71\x4f\xea\xa4\xde\x82\x3f\xe8\x5a\x22\x4a\x4d\xd0\x48\xfa",
    /* 10G */ "\xce\xf6\x6d\x6b\x2a\x3a\x99\x3e\x59\x12\x14\xd1\xea\x22\x3f\xb5\x45\xca\x6c\x47\x1c\x48\x30\x6e\x4c\x36\x06\x94\x04\xc5\x72\x3f\x87\x86\x62\xa2\x29\xaa\xae\x90\x6e\x12\x3c\xdd\x9d\x3b\x4c\x10\x59\x0d\xed\x29\xfe\x75\x1e\xee\xca\x34\xbb\xaa\x44\xaf\x07\x73",
    /* 11G */ "\x3e\xd1\x13\xb7\x88\x3b\x4c\x59\x06\x38\x37\x9d\xb0\xc2\x1c\xda\x16\x74\x2e\xd0\x25\x50\x48\xbf\x43\x33\x91\xd3\x74\xbc\x21\xd1\x90\x99\x20\x9a\xcc\xc4\xc8\xa2\x24\xc8\x43\xaf\xa4\xf4\xc6\x8a\x09\x0d\x04\xda\x5e\x98\x89\xda\xe2\xf8\xee\xfc\xe8\x2a\x37\x40",
    /* 12G */ "\x74\x1d\xd5\xbd\xa8\x17\xd9\x5e\x46\x26\x53\x73\x20\xe5\xd5\x51\x79\x98\x30\x28\xb2\xf8\x2c\x99\xd5\x00\xc5\xee\x86\x24\xe3\xc4\x07\x70\xb4\x6a\x9c\x38\x5f\xdc\x56\x73\x83\x55\x48\x87\xb1\x54\x8e\xeb\x91\x2c\x35\xba\x5c\xa7\x19\x95\xff\x22\xcd\x44\x81\xd3",
    /* 13G */ "\x17\x7c\x83\x7a\xe0\xac\x49\x5a\x61\x80\x5d\xf2\xd8\x5e\xe2\xfc\x79\x2e\x28\x4b\x65\xea\xd5\x8a\x98\xe1\x5d\x9d\x46\x07\x2c\x01\x63\xbb\x58\xcd\x4e\xbe\xa5\x58\xa2\x40\x91\xad\xb4\x0f\x4e\x72\x26\xee\x14\xc3\xa1\xfb\x4d\xf3\x9c\x43\xbb\xe2\xef\xc7\xbf\xd8",
    /* 14G */ "\x54\xe7\x7a\x00\x1c\x38\x62\xb9\x7a\x76\x64\x7f\x43\x36\xdf\x3c\xf1\x26\xac\xbe\x7a\x06\x9c\x5e\x57\x09\x27\x73\x24\xd2\x92\x0b\xf5\x99\xf1\xbb\x29\xf4\x31\x75\x42\x12\x1f\x8c\x05\xa2\xe7\xc3\x71\x71\xea\x77\x73\x50\x90\x08\x1b\xa7\xc8\x2f\x60\xd0\xb3\x75",
    /* 15G */ "\xf0\x45\x4d\xc6\x97\x1a\xba\xe7\xad\xfb\x37\x89\x99\x88\x82\x65\xae\x03\xaf\x92\xde\x3a\x0e\xf1\x63\x66\x8c\x63\xe5\x9b\x9d\x5f\xb5\xb9\x3e\xe3\x59\x2e\x2d\x1f\x4e\x65\x94\xe5\x1f\x96\x43\xe6\x2a\x3b\x21\xce\x75\xb5\xfa\x3f\x47\xe5\x9c\xde\x0d\x03\x4f\x36",
};

static void
u256_from_bytes(U256 *result, const unsigned char bytes[CURVE_BYTES])
{
    *result = U256_ZERO;
    for (int index = 0; index < CURVE_BYTES; index++)
        result->limb[index / CURVE_WORD_BYTES] |=
            (CurveWord)bytes[index] << ((index % CURVE_WORD_BYTES) * 8);
}

static void
u256_from_big_endian(U256 *result, const unsigned char bytes[CURVE_BYTES])
{
    unsigned char reversed[CURVE_BYTES];
    for (int index = 0; index < CURVE_BYTES; index++)
        reversed[index] = bytes[CURVE_BYTES - 1 - index];
    u256_from_bytes(result, reversed);
}

static void
u256_to_bytes(unsigned char bytes[CURVE_BYTES], const U256 *value)
{
    for (int index = 0; index < CURVE_BYTES; index++)
        bytes[index] = (unsigned char)(
            value->limb[index / CURVE_WORD_BYTES] >>
            ((index % CURVE_WORD_BYTES) * 8));
}

static CurveWord
u256_sub(U256 *result, const U256 *left, const U256 *right)
{
    CurveWide borrow = 0;
    for (int index = 0; index < CURVE_LIMBS; index++) {
        CurveWide minuend = left->limb[index];
        CurveWide subtrahend = (CurveWide)right->limb[index] + borrow;
        result->limb[index] = (CurveWord)(minuend - subtrahend);
        borrow = minuend < subtrahend;
    }
    return (CurveWord)borrow;
}

static CurveWord
u256_add(U256 *result, const U256 *left, const U256 *right)
{
    CurveWide carry = 0;
    for (int index = 0; index < CURVE_LIMBS; index++) {
        CurveWide sum = (CurveWide)left->limb[index] + right->limb[index] + carry;
        result->limb[index] = (CurveWord)sum;
        carry = sum >> CURVE_WIDE_SHIFT;
    }
    return (CurveWord)carry;
}

static void
u256_select(U256 *result, const U256 *when_zero, const U256 *when_one,
            CurveWord choose_one)
{
    CurveWord mask = (CurveWord)0 - (choose_one & 1u);
    for (int index = 0; index < CURVE_LIMBS; index++)
        result->limb[index] = when_zero->limb[index] ^
            (mask & (when_zero->limb[index] ^ when_one->limb[index]));
}

static CurveWord
u256_is_zero(const U256 *value)
{
    CurveWord combined = 0;
    for (int index = 0; index < CURVE_LIMBS; index++)
        combined |= value->limb[index];
    return combined == 0;
}

static CurveWord
u256_bit(const U256 *value, int index)
{
    return (value->limb[index / CURVE_WORD_BITS] >>
            (index % CURVE_WORD_BITS)) & 1u;
}

static CurveWord
u256_nibble(const U256 *value, int index)
{
    int bit = index * 4;
    return (value->limb[bit / CURVE_WORD_BITS] >>
            (bit % CURVE_WORD_BITS)) & 0xfu;
}

#define CURVE_WNAF_BITS 257

#if defined(WREATH_CURVE_HAVE_INT128)
#define CURVE_UNROLL _Pragma("GCC unroll 8")
#else
#define CURVE_UNROLL
#endif

/* Width-five signed digits for public scalar multiplication. The extra limb
 * represents the carry from recoding 2^256 - 1 as 2^256 - 1. */
static void
u256_wnaf(int8_t digits[CURVE_WNAF_BITS], const U256 *scalar, int width)
{
    CurveWord work[CURVE_LIMBS + 1] = {0};
    CurveWord mask = ((CurveWord)1 << width) - 1u;
    int midpoint = 1 << (width - 1);
    for (int index = 0; index < CURVE_LIMBS; index++)
        work[index] = scalar->limb[index];
    for (int position = 0; position < CURVE_WNAF_BITS; position++) {
        int digit = 0;
        if (work[0] & 1u) {
            digit = (int)(work[0] & mask);
            if (digit > midpoint) digit -= 1 << width;
            if (digit > 0) {
                CurveWord borrow = (CurveWord)digit;
                for (int index = 0; index <= CURVE_LIMBS; index++) {
                    CurveWord before = work[index];
                    work[index] = before - borrow;
                    borrow = before < borrow;
                }
            }
            else {
                CurveWord carry = (CurveWord)(-digit);
                for (int index = 0; index <= CURVE_LIMBS; index++) {
                    CurveWide sum = (CurveWide)work[index] + carry;
                    work[index] = (CurveWord)sum;
                    carry = (CurveWord)(sum >> CURVE_WIDE_SHIFT);
                }
            }
        }
        digits[position] = (int8_t)digit;
        for (int index = 0; index < CURVE_LIMBS; index++)
            work[index] = (work[index] >> 1) |
                (work[index + 1] << (CURVE_WORD_BITS - 1));
        work[CURVE_LIMBS] >>= 1;
    }
}

static int
u256_compare(const U256 *left, const U256 *right)
{
    for (int index = CURVE_LIMBS - 1; index >= 0; index--) {
        if (left->limb[index] < right->limb[index]) return -1;
        if (left->limb[index] > right->limb[index]) return 1;
    }
    return 0;
}

#if defined(__GNUC__) || defined(__clang__)
#define CURVE_ALWAYS_INLINE static inline __attribute__((always_inline))
#else
#define CURVE_ALWAYS_INLINE static inline
#endif

CURVE_ALWAYS_INLINE void
montgomery_multiply_fixed(U256 *result, const U256 *left, const U256 *right,
                          const Modulus *modulus)
{
    CurveWord product[CURVE_LIMBS * 2 + 1] = {0};
    U256 candidate;
    U256 reduced;

    CURVE_UNROLL
    for (int i = 0; i < CURVE_LIMBS; i++) {
        CurveWide carry = 0;
        CURVE_UNROLL
        for (int j = 0; j < CURVE_LIMBS; j++) {
            CurveWide sum = (CurveWide)left->limb[i] * right->limb[j] +
                           product[i + j] + carry;
            product[i + j] = (CurveWord)sum;
            carry = sum >> CURVE_WIDE_SHIFT;
        }
        CURVE_UNROLL
        for (int k = i + CURVE_LIMBS; k <= CURVE_LIMBS * 2; k++) {
            CurveWide sum = (CurveWide)product[k] + carry;
            product[k] = (CurveWord)sum;
            carry = sum >> CURVE_WIDE_SHIFT;
        }
    }

    CURVE_UNROLL
    for (int i = 0; i < CURVE_LIMBS; i++) {
        CurveWord factor = product[i] * modulus->n0;
        CurveWide carry = 0;
        CURVE_UNROLL
        for (int j = 0; j < CURVE_LIMBS; j++) {
            CurveWide sum = (CurveWide)factor * modulus->n.limb[j] +
                           product[i + j] + carry;
            product[i + j] = (CurveWord)sum;
            carry = sum >> CURVE_WIDE_SHIFT;
        }
        CURVE_UNROLL
        for (int k = i + CURVE_LIMBS; k <= CURVE_LIMBS * 2; k++) {
            CurveWide sum = (CurveWide)product[k] + carry;
            product[k] = (CurveWord)sum;
            carry = sum >> CURVE_WIDE_SHIFT;
        }
    }

    CURVE_UNROLL
    for (int index = 0; index < CURVE_LIMBS; index++)
        candidate.limb[index] = product[index + CURVE_LIMBS];
    {
        CurveWord borrow = u256_sub(&reduced, &candidate, &modulus->n);
        CurveWord needs_reduction =
            (product[CURVE_LIMBS * 2] != 0) | (borrow ^ 1u);
        u256_select(result, &candidate, &reduced, needs_reduction);
    }
}

static void
montgomery_multiply(U256 *result, const U256 *left, const U256 *right,
                    const Modulus *modulus)
{
#if defined(WREATH_CURVE_HAVE_INT128)
    if (modulus == &P256_FIELD) {
        montgomery_multiply_fixed(result, left, right, &P256_FIELD);
        return;
    }
    montgomery_multiply_fixed(result, left, right, &P256_SCALAR_FIELD);
#else
    montgomery_multiply_fixed(result, left, right, modulus);
#endif
}

static void
field_from_normal(U256 *result, const U256 *value, const Modulus *modulus)
{
    montgomery_multiply(result, value, &modulus->r2, modulus);
}

static void
field_to_normal(U256 *result, const U256 *value, const Modulus *modulus)
{
    montgomery_multiply(result, value, &U256_ONE, modulus);
}

static void
field_add(U256 *result, const U256 *left, const U256 *right,
          const Modulus *modulus)
{
    U256 sum;
    U256 reduced;
    CurveWord carry = u256_add(&sum, left, right);
    CurveWord borrow = u256_sub(&reduced, &sum, &modulus->n);
    u256_select(result, &sum, &reduced, carry | (borrow ^ 1u));
}

static void
field_sub(U256 *result, const U256 *left, const U256 *right,
          const Modulus *modulus)
{
    U256 difference;
    U256 corrected;
    CurveWord borrow = u256_sub(&difference, left, right);
    (void)u256_add(&corrected, &difference, &modulus->n);
    u256_select(result, &difference, &corrected, borrow);
}

static void
field_negate(U256 *result, const U256 *value, const Modulus *modulus)
{
    U256 negative;
    (void)u256_sub(&negative, &modulus->n, value);
    u256_select(result, &negative, &U256_ZERO, u256_is_zero(value));
}

static void
field_multiply(U256 *result, const U256 *left, const U256 *right,
               const Modulus *modulus)
{
    montgomery_multiply(result, left, right, modulus);
}

static void
field_square(U256 *result, const U256 *value, const Modulus *modulus)
{
    montgomery_multiply(result, value, value, modulus);
}

static void
field_small_multiply(U256 *result, const U256 *value, unsigned factor,
                     const Modulus *modulus)
{
    U256 accumulated = U256_ZERO;
    U256 addend = *value;
    while (factor != 0) {
        if (factor & 1u) field_add(&accumulated, &accumulated, &addend, modulus);
        field_add(&addend, &addend, &addend, modulus);
        factor >>= 1;
    }
    *result = accumulated;
}

#if !defined(WREATH_CURVE_HAVE_INT128)
static void
field_power(U256 *result, const U256 *base, const U256 *exponent,
            const Modulus *modulus)
{
    U256 accumulated = modulus->one;
    for (int bit = 255; bit >= 0; bit--) {
        field_square(&accumulated, &accumulated, modulus);
        if (u256_bit(exponent, bit))
            field_multiply(&accumulated, &accumulated, base, modulus);
    }
    *result = accumulated;
}
#endif

/* The exponent is public and fixed. Four-bit windows save fifty field
 * multiplies over binary exponentiation for the 128-set-bit P-256 p - 2. */
static void
field_power_window4(U256 *result, const U256 *base, const U256 *exponent,
                    const Modulus *modulus)
{
    U256 powers[16];
    U256 accumulated = modulus->one;
    powers[0] = modulus->one;
    powers[1] = *base;
    for (int index = 2; index < 16; index++)
        field_multiply(&powers[index], &powers[index - 1], base, modulus);
    for (int index = 63; index >= 0; index--) {
        CurveWord digit = u256_nibble(exponent, index);
        for (int square = 0; square < 4; square++)
            field_square(&accumulated, &accumulated, modulus);
        if (digit != 0)
            field_multiply(&accumulated, &accumulated, &powers[digit], modulus);
    }
    *result = accumulated;
}

static int
py_to_u256(PyObject *object, U256 *result)
{
    unsigned char bytes[CURVE_BYTES];
    Py_ssize_t required;
    if (!PyLong_Check(object)) {
        PyErr_Format(PyExc_TypeError, "curve integer must be int, not %.200s",
                     Py_TYPE(object)->tp_name);
        return -1;
    }
    required = PyLong_AsNativeBytes(object, bytes, sizeof(bytes), PY_U256_FLAGS);
    if (required < 0) return -1;
    if (required > CURVE_BYTES) {
        PyErr_SetString(PyExc_OverflowError, "curve integer does not fit in 256 bits");
        return -1;
    }
    u256_from_bytes(result, bytes);
    return 0;
}

/* 1 means canonical, 0 means outside the field, -1 means a type error. */
static int
py_to_canonical_u256(PyObject *object, U256 *result, const U256 *bound)
{
    if (py_to_u256(object, result) < 0) {
        if (PyErr_ExceptionMatches(PyExc_OverflowError) ||
            PyErr_ExceptionMatches(PyExc_ValueError)) {
            PyErr_Clear();
            return 0;
        }
        return -1;
    }
    return u256_compare(result, bound) < 0;
}

static PyObject *
u256_to_py(const U256 *value)
{
    unsigned char bytes[CURVE_BYTES];
    u256_to_bytes(bytes, value);
    return PyLong_FromUnsignedNativeBytes(
        bytes, sizeof(bytes),
        Py_ASNATIVEBYTES_LITTLE_ENDIAN | Py_ASNATIVEBYTES_UNSIGNED_BUFFER);
}

static PyObject *
field_to_py(const U256 *value, const Modulus *modulus)
{
    U256 normal;
    field_to_normal(&normal, value, modulus);
    return u256_to_py(&normal);
}

static int
field_from_py(U256 *result, PyObject *object, const Modulus *modulus,
              const char *message)
{
    U256 normal;
    int canonical = py_to_canonical_u256(object, &normal, &modulus->n);
    if (canonical < 0) return -1;
    if (!canonical) {
        PyErr_SetString(PyExc_ValueError, message);
        return -1;
    }
    field_from_normal(result, &normal, modulus);
    return 0;
}

#if defined(WREATH_CURVE_HAVE_INT128)

#define ED_FE_MASK UINT64_C(0x7ffffffffffff)
static const EdFe ED_FE_ZERO = {{0, 0, 0, 0, 0}};
static const EdFe ED_FE_ONE = {{1, 0, 0, 0, 0}};

static void
ed_fe_carry(EdFe *value)
{
    for (int round = 0; round < 2; round++) {
        uint64_t carry;
        carry = value->limb[0] >> 51;
        value->limb[0] &= ED_FE_MASK;
        value->limb[1] += carry;
        carry = value->limb[1] >> 51;
        value->limb[1] &= ED_FE_MASK;
        value->limb[2] += carry;
        carry = value->limb[2] >> 51;
        value->limb[2] &= ED_FE_MASK;
        value->limb[3] += carry;
        carry = value->limb[3] >> 51;
        value->limb[3] &= ED_FE_MASK;
        value->limb[4] += carry;
        carry = value->limb[4] >> 51;
        value->limb[4] &= ED_FE_MASK;
        value->limb[0] += carry * 19;
    }
}

static void
ed_fe_from_normal(EdFe *result, const U256 *value)
{
    result->limb[0] = value->limb[0] & ED_FE_MASK;
    result->limb[1] = ((value->limb[0] >> 51) |
                       (value->limb[1] << 13)) & ED_FE_MASK;
    result->limb[2] = ((value->limb[1] >> 38) |
                       (value->limb[2] << 26)) & ED_FE_MASK;
    result->limb[3] = ((value->limb[2] >> 25) |
                       (value->limb[3] << 39)) & ED_FE_MASK;
    result->limb[4] = (value->limb[3] >> 12) & ED_FE_MASK;
}

static void
ed_fe_to_normal(U256 *result, const EdFe *value)
{
    EdFe carried = *value;
    U256 reduced;
    CurveWord borrow;
    ed_fe_carry(&carried);
    result->limb[0] = carried.limb[0] | (carried.limb[1] << 51);
    result->limb[1] = (carried.limb[1] >> 13) | (carried.limb[2] << 38);
    result->limb[2] = (carried.limb[2] >> 26) | (carried.limb[3] << 25);
    result->limb[3] = (carried.limb[3] >> 39) | (carried.limb[4] << 12);
    borrow = u256_sub(&reduced, result, &ED_FIELD.n);
    u256_select(result, result, &reduced, borrow ^ 1u);
}

static void
ed_fe_add(EdFe *result, const EdFe *left, const EdFe *right)
{
    EdFe sum;
    for (int index = 0; index < 5; index++)
        sum.limb[index] = left->limb[index] + right->limb[index];
    ed_fe_carry(&sum);
    *result = sum;
}

static void
ed_fe_sub(EdFe *result, const EdFe *left, const EdFe *right)
{
    EdFe difference;
    difference.limb[0] = left->limb[0] + 2 * (ED_FE_MASK - 18) - right->limb[0];
    for (int index = 1; index < 5; index++)
        difference.limb[index] =
            left->limb[index] + 2 * ED_FE_MASK - right->limb[index];
    ed_fe_carry(&difference);
    *result = difference;
}

static void
ed_fe_multiply(EdFe *result, const EdFe *left, const EdFe *right)
{
    __uint128_t h0 = (__uint128_t)left->limb[0] * right->limb[0] +
        19 * ((__uint128_t)left->limb[1] * right->limb[4] +
              (__uint128_t)left->limb[2] * right->limb[3] +
              (__uint128_t)left->limb[3] * right->limb[2] +
              (__uint128_t)left->limb[4] * right->limb[1]);
    __uint128_t h1 = (__uint128_t)left->limb[0] * right->limb[1] +
        (__uint128_t)left->limb[1] * right->limb[0] +
        19 * ((__uint128_t)left->limb[2] * right->limb[4] +
              (__uint128_t)left->limb[3] * right->limb[3] +
              (__uint128_t)left->limb[4] * right->limb[2]);
    __uint128_t h2 = (__uint128_t)left->limb[0] * right->limb[2] +
        (__uint128_t)left->limb[1] * right->limb[1] +
        (__uint128_t)left->limb[2] * right->limb[0] +
        19 * ((__uint128_t)left->limb[3] * right->limb[4] +
              (__uint128_t)left->limb[4] * right->limb[3]);
    __uint128_t h3 = (__uint128_t)left->limb[0] * right->limb[3] +
        (__uint128_t)left->limb[1] * right->limb[2] +
        (__uint128_t)left->limb[2] * right->limb[1] +
        (__uint128_t)left->limb[3] * right->limb[0] +
        19 * (__uint128_t)left->limb[4] * right->limb[4];
    __uint128_t h4 = (__uint128_t)left->limb[0] * right->limb[4] +
        (__uint128_t)left->limb[1] * right->limb[3] +
        (__uint128_t)left->limb[2] * right->limb[2] +
        (__uint128_t)left->limb[3] * right->limb[1] +
        (__uint128_t)left->limb[4] * right->limb[0];
    EdFe product;
    h1 += h0 >> 51;
    product.limb[0] = (uint64_t)h0 & ED_FE_MASK;
    h2 += h1 >> 51;
    product.limb[1] = (uint64_t)h1 & ED_FE_MASK;
    h3 += h2 >> 51;
    product.limb[2] = (uint64_t)h2 & ED_FE_MASK;
    h4 += h3 >> 51;
    product.limb[3] = (uint64_t)h3 & ED_FE_MASK;
    product.limb[4] = (uint64_t)h4 & ED_FE_MASK;
    product.limb[0] += (uint64_t)(h4 >> 51) * 19;
    ed_fe_carry(&product);
    *result = product;
}

static void
ed_fe_square(EdFe *result, const EdFe *value)
{
    ed_fe_multiply(result, value, value);
}

static void
ed_fe_square_n(EdFe *result, const EdFe *value, int count)
{
    EdFe power = *value;
    for (int index = 0; index < count; index++) ed_fe_square(&power, &power);
    *result = power;
}

/* z^(2^252 - 3), the exponent used by the square-root ratio. */
static void
ed_fe_pow22523(EdFe *result, const EdFe *z)
{
    EdFe t0, t1, t2;
    ed_fe_square(&t0, z);              /* 2 */
    ed_fe_square_n(&t1, &t0, 2);       /* 8 */
    ed_fe_multiply(&t1, z, &t1);       /* 9 */
    ed_fe_multiply(&t0, &t0, &t1);     /* 11 */
    ed_fe_square(&t0, &t0);            /* 22 */
    ed_fe_multiply(&t0, &t1, &t0);     /* 2^5 - 1 */
    ed_fe_square_n(&t1, &t0, 5);
    ed_fe_multiply(&t0, &t1, &t0);     /* 2^10 - 1 */
    ed_fe_square_n(&t1, &t0, 10);
    ed_fe_multiply(&t1, &t1, &t0);     /* 2^20 - 1 */
    ed_fe_square_n(&t2, &t1, 20);
    ed_fe_multiply(&t1, &t2, &t1);     /* 2^40 - 1 */
    ed_fe_square_n(&t1, &t1, 10);
    ed_fe_multiply(&t0, &t1, &t0);     /* 2^50 - 1 */
    ed_fe_square_n(&t1, &t0, 50);
    ed_fe_multiply(&t1, &t1, &t0);     /* 2^100 - 1 */
    ed_fe_square_n(&t2, &t1, 100);
    ed_fe_multiply(&t1, &t2, &t1);     /* 2^200 - 1 */
    ed_fe_square_n(&t1, &t1, 50);
    ed_fe_multiply(&t0, &t1, &t0);     /* 2^250 - 1 */
    ed_fe_square_n(&t0, &t0, 2);
    ed_fe_multiply(result, &t0, z);     /* 2^252 - 3 */
}

/* z^(p - 2), with eleven multiplies beyond the squarings. */
static void
ed_fe_invert(EdFe *result, const EdFe *z)
{
    EdFe z11, t1, t2, t3;
    ed_fe_square(&z11, z);              /* 2 */
    ed_fe_square_n(&t1, &z11, 2);       /* 8 */
    ed_fe_multiply(&t1, z, &t1);        /* 9 */
    ed_fe_multiply(&z11, &z11, &t1);    /* 11 */
    ed_fe_square(&t2, &z11);            /* 22 */
    ed_fe_multiply(&t1, &t1, &t2);      /* 2^5 - 1 */
    ed_fe_square_n(&t2, &t1, 5);
    ed_fe_multiply(&t1, &t2, &t1);      /* 2^10 - 1 */
    ed_fe_square_n(&t2, &t1, 10);
    ed_fe_multiply(&t2, &t2, &t1);      /* 2^20 - 1 */
    ed_fe_square_n(&t3, &t2, 20);
    ed_fe_multiply(&t2, &t3, &t2);      /* 2^40 - 1 */
    ed_fe_square_n(&t2, &t2, 10);
    ed_fe_multiply(&t1, &t2, &t1);      /* 2^50 - 1 */
    ed_fe_square_n(&t2, &t1, 50);
    ed_fe_multiply(&t2, &t2, &t1);      /* 2^100 - 1 */
    ed_fe_square_n(&t3, &t2, 100);
    ed_fe_multiply(&t2, &t3, &t2);      /* 2^200 - 1 */
    ed_fe_square_n(&t2, &t2, 50);
    ed_fe_multiply(&t1, &t2, &t1);      /* 2^250 - 1 */
    ed_fe_square_n(&t1, &t1, 5);        /* 2^255 - 32 */
    ed_fe_multiply(result, &t1, &z11);  /* 2^255 - 21 */
}

static void
ed_fe_negate(EdFe *result, const EdFe *value)
{
    ed_fe_sub(result, &ED_FE_ZERO, value);
}

static CurveWord
ed_fe_is_zero(const EdFe *value)
{
    U256 normal;
    ed_fe_to_normal(&normal, value);
    return u256_is_zero(&normal);
}

static void
ed_fe_select(EdFe *result, const EdFe *when_zero, const EdFe *when_one,
             CurveWord bit)
{
    uint64_t mask = UINT64_C(0) - (uint64_t)(bit & 1u);
    for (int index = 0; index < 5; index++)
        result->limb[index] = when_zero->limb[index] ^
            (mask & (when_zero->limb[index] ^ when_one->limb[index]));
}

static PyObject *
ed_fe_to_py(const EdFe *value)
{
    U256 normal;
    ed_fe_to_normal(&normal, value);
    return u256_to_py(&normal);
}

static int
ed_fe_from_py(EdFe *result, PyObject *object, const char *message)
{
    U256 normal;
    int canonical = py_to_canonical_u256(object, &normal, &ED_FIELD.n);
    if (canonical < 0) return -1;
    if (!canonical) {
        PyErr_SetString(PyExc_ValueError, message);
        return -1;
    }
    ed_fe_from_normal(result, &normal);
    return 0;
}

#else

#define ED_FE_ZERO U256_ZERO
#define ED_FE_ONE ED_FIELD.one
#define ed_fe_add(result, left, right) field_add(result, left, right, &ED_FIELD)
#define ed_fe_sub(result, left, right) field_sub(result, left, right, &ED_FIELD)
#define ed_fe_multiply(result, left, right) \
    field_multiply(result, left, right, &ED_FIELD)
#define ed_fe_square(result, value) field_square(result, value, &ED_FIELD)
#define ed_fe_negate(result, value) field_negate(result, value, &ED_FIELD)
#define ed_fe_is_zero(value) u256_is_zero(value)
#define ed_fe_select(result, zero, one, bit) u256_select(result, zero, one, bit)
#define ed_fe_pow22523(result, base) \
    field_power(result, base, &ED_POW22523_EXPONENT, &ED_FIELD)
#define ed_fe_invert(result, base) \
    field_power(result, base, &ED_INVERSE_EXPONENT, &ED_FIELD)
#define ed_fe_to_py(value) field_to_py(value, &ED_FIELD)
#define ed_fe_from_normal(result, value) field_from_normal(result, value, &ED_FIELD)

static void
ed_fe_to_normal(U256 *result, const EdFe *value)
{
    field_to_normal(result, value, &ED_FIELD);
}

static int
ed_fe_from_py(EdFe *result, PyObject *object, const char *message)
{
    return field_from_py(result, object, &ED_FIELD, message);
}

#endif

static int
scalar_from_py(PyObject *object, U256 *result)
{
    return py_to_u256(object, result);
}

static void
scalar_reduce_ed(U256 *scalar)
{
    for (int round = 0; round < 16; round++) {
        U256 reduced;
        CurveWord borrow = u256_sub(&reduced, scalar, &ED_ORDER);
        u256_select(scalar, scalar, &reduced, borrow ^ 1u);
    }
}

static void
scalar_reduce_le_bytes(U256 *result, const unsigned char *bytes,
                       Py_ssize_t length, const U256 *modulus)
{
    *result = U256_ZERO;
    for (Py_ssize_t bit = length * 8; bit-- > 0;) {
        U256 doubled, stepped, reduced;
        CurveWord borrow;
        (void)u256_add(&doubled, result, result);
        (void)u256_add(&stepped, &doubled, &U256_ONE);
        u256_select(result, &doubled, &stepped,
                    (bytes[bit / 8] >> (bit % 8)) & 1u);
        borrow = u256_sub(&reduced, result, modulus);
        u256_select(result, result, &reduced, borrow ^ 1u);
    }
}

static void
scalar_reduce_be_bytes(U256 *result, const unsigned char *bytes,
                       Py_ssize_t length, const U256 *modulus)
{
    *result = U256_ZERO;
    for (Py_ssize_t byte = 0; byte < length; byte++) {
        for (int bit = 7; bit >= 0; bit--) {
            U256 doubled, stepped, reduced;
            CurveWord borrow;
            (void)u256_add(&doubled, result, result);
            (void)u256_add(&stepped, &doubled, &U256_ONE);
            u256_select(result, &doubled, &stepped,
                        (bytes[byte] >> bit) & 1u);
            borrow = u256_sub(&reduced, result, modulus);
            u256_select(result, result, &reduced, borrow ^ 1u);
        }
    }
}

static void
scalar_add_ed(U256 *result, const U256 *left, const U256 *right)
{
    U256 sum, reduced;
    CurveWord carry = u256_add(&sum, left, right);
    CurveWord borrow = u256_sub(&reduced, &sum, &ED_ORDER);
    u256_select(result, &sum, &reduced, carry | (borrow ^ 1u));
}

static void
scalar_multiply_ed(U256 *result, const U256 *left, const U256 *right)
{
    U256 accumulated = U256_ZERO;
    for (int bit = 255; bit >= 0; bit--) {
        U256 doubled, stepped;
        scalar_add_ed(&doubled, &accumulated, &accumulated);
        scalar_add_ed(&stepped, &doubled, left);
        u256_select(&accumulated, &doubled, &stepped, u256_bit(right, bit));
    }
    *result = accumulated;
}

static int
ed_point_from_py(PyObject *object, EdPoint *point)
{
    PyObject *sequence = PySequence_Fast(object,
        "edwards point must have four coordinates");
    if (sequence == NULL) return -1;
    if (PySequence_Fast_GET_SIZE(sequence) != 4) {
        Py_DECREF(sequence);
        PyErr_SetString(PyExc_ValueError,
                        "edwards point must have four coordinates");
        return -1;
    }
    for (int index = 0; index < 4; index++) {
        EdFe *coordinate = index == 0 ? &point->x : index == 1 ? &point->y :
                            index == 2 ? &point->z : &point->t;
        if (ed_fe_from_py(coordinate, PySequence_Fast_GET_ITEM(sequence, index),
                          "edwards coordinate must be in [0, 2**255 - 19)") < 0) {
            Py_DECREF(sequence);
            return -1;
        }
    }
    Py_DECREF(sequence);
    return 0;
}

static PyObject *
ed_point_to_py(const EdPoint *point)
{
    PyObject *items[4] = {NULL, NULL, NULL, NULL};
    PyObject *result = NULL;
    items[0] = ed_fe_to_py(&point->x);
    items[1] = ed_fe_to_py(&point->y);
    items[2] = ed_fe_to_py(&point->z);
    items[3] = ed_fe_to_py(&point->t);
    if (items[0] != NULL && items[1] != NULL && items[2] != NULL && items[3] != NULL)
        result = PyTuple_Pack(4, items[0], items[1], items[2], items[3]);
    for (int index = 0; index < 4; index++) Py_XDECREF(items[index]);
    return result;
}

static EdPoint
ed_neutral(void)
{
    EdPoint result = {ED_FE_ZERO, ED_FE_ONE, ED_FE_ONE, ED_FE_ZERO};
    return result;
}

static void
ed_add(EdPoint *result, const EdPoint *left, const EdPoint *right)
{
    EdFe a, b, c, d, e, f, g, h, first, second;
    ed_fe_sub(&first, &left->y, &left->x);
    ed_fe_sub(&second, &right->y, &right->x);
    ed_fe_multiply(&a, &first, &second);
    ed_fe_add(&first, &left->y, &left->x);
    ed_fe_add(&second, &right->y, &right->x);
    ed_fe_multiply(&b, &first, &second);
    ed_fe_multiply(&first, &left->t, &right->t);
    ed_fe_multiply(&second, &first, &ED_D);
    ed_fe_add(&c, &second, &second);
    ed_fe_multiply(&first, &left->z, &right->z);
    ed_fe_add(&d, &first, &first);
    ed_fe_sub(&e, &b, &a);
    ed_fe_sub(&f, &d, &c);
    ed_fe_add(&g, &d, &c);
    ed_fe_add(&h, &b, &a);
    ed_fe_multiply(&result->x, &e, &f);
    ed_fe_multiply(&result->y, &g, &h);
    ed_fe_multiply(&result->z, &f, &g);
    ed_fe_multiply(&result->t, &e, &h);
}

/* Complete extended-coordinate doubling for a=-1.  Calling the generic add
 * formula with the same point spends ten field multiplies; the dedicated
 * formula needs four squares and four multiplies.  Every scalar operation pays
 * one doubling per bit, so this is the field-operation count that matters. */
static void
ed_double(EdPoint *result, const EdPoint *point)
{
    EdFe a, b, c, d, e, f, g, h, sum;
    ed_fe_square(&a, &point->x);
    ed_fe_square(&b, &point->y);
    ed_fe_square(&c, &point->z);
    ed_fe_add(&c, &c, &c);
    ed_fe_negate(&d, &a);
    ed_fe_add(&sum, &point->x, &point->y);
    ed_fe_square(&e, &sum);
    ed_fe_sub(&e, &e, &a);
    ed_fe_sub(&e, &e, &b);
    ed_fe_add(&g, &d, &b);
    ed_fe_sub(&f, &g, &c);
    ed_fe_sub(&h, &d, &b);
    ed_fe_multiply(&result->x, &e, &f);
    ed_fe_multiply(&result->y, &g, &h);
    ed_fe_multiply(&result->t, &e, &h);
    ed_fe_multiply(&result->z, &f, &g);
}

static void
ed_select(EdPoint *result, const EdPoint *when_zero, const EdPoint *when_one,
          CurveWord bit)
{
    ed_fe_select(&result->x, &when_zero->x, &when_one->x, bit);
    ed_fe_select(&result->y, &when_zero->y, &when_one->y, bit);
    ed_fe_select(&result->z, &when_zero->z, &when_one->z, bit);
    ed_fe_select(&result->t, &when_zero->t, &when_one->t, bit);
}

static void
ed_point_negate(EdPoint *result, const EdPoint *point)
{
    *result = *point;
    ed_fe_negate(&result->x, &point->x);
    ed_fe_negate(&result->t, &point->t);
}

static void
ed_scalar(EdPoint *result, const U256 *scalar, const EdPoint *point,
          int bits, int secret)
{
    EdPoint accumulated = ed_neutral();
    for (int index = bits - 1; index >= 0; index--) {
        EdPoint doubled;
        ed_double(&doubled, &accumulated);
        if (secret) {
            EdPoint stepped;
            ed_add(&stepped, &doubled, point);
            ed_select(&accumulated, &doubled, &stepped, u256_bit(scalar, index));
        }
        else if (u256_bit(scalar, index)) {
            ed_add(&accumulated, &doubled, point);
        }
        else {
            accumulated = doubled;
        }
    }
    *result = accumulated;
}

/* The base point is public and fixed, so a complete nibble table can live on
 * this operation's stack without introducing process-global mutable state.
 * Selection still touches every table entry: the scalar controls neither the
 * instruction shape nor the memory addresses read.  Compared with the generic
 * secret ladder this keeps the 256 doublings but replaces 254 additions with
 * 64 additions plus the 14 needed to build the operation-owned table. */
static void
ed_scalar_base_secret(EdPoint *result, const U256 *scalar)
{
    EdPoint table[16];
    EdPoint accumulated = ed_neutral();
    table[0] = accumulated;
    table[1] = ED_BASE_POINT;
    for (int index = 2; index < 16; index++)
        ed_add(&table[index], &table[index - 1], &ED_BASE_POINT);

    for (int nibble = 63; nibble >= 0; nibble--) {
        EdPoint selected = table[0];
        CurveWord digit = u256_nibble(scalar, nibble);
        for (int square = 0; square < 4; square++) {
            EdPoint doubled;
            ed_double(&doubled, &accumulated);
            accumulated = doubled;
        }
        for (CurveWord choice = 1; choice < 16; choice++) {
            CurveWord equal = ((digit ^ choice) - 1u) >>
                              (CURVE_WORD_BITS - 1);
            EdPoint chosen;
            ed_select(&chosen, &selected, &table[choice], equal);
            selected = chosen;
        }
        {
            EdPoint stepped;
            ed_add(&stepped, &accumulated, &selected);
            accumulated = stepped;
        }
    }
    *result = accumulated;
}

static int
ed_recover_x_field(EdFe *x, const EdFe *y, int sign)
{
    EdFe yy, numerator, denominator, denominator2, denominator3;
    EdFe denominator7, power, square, check, negative_numerator;
    ed_fe_square(&yy, y);
    ed_fe_sub(&numerator, &yy, &ED_FE_ONE);
    ed_fe_multiply(&denominator, &ED_D, &yy);
    ed_fe_add(&denominator, &denominator, &ED_FE_ONE);
    if (ed_fe_is_zero(&denominator)) return 0;

    /* sqrt(numerator / denominator) with one fixed exponentiation. */
    ed_fe_square(&denominator2, &denominator);
    ed_fe_multiply(&denominator3, &denominator2, &denominator);
    ed_fe_square(&denominator7, &denominator3);
    ed_fe_multiply(&denominator7, &denominator7, &denominator);
    ed_fe_multiply(&power, &numerator, &denominator7);
    ed_fe_pow22523(&power, &power);
    ed_fe_multiply(x, &numerator, &denominator3);
    ed_fe_multiply(x, x, &power);
    ed_fe_square(&square, x);
    ed_fe_multiply(&check, &square, &denominator);
    ed_fe_sub(&check, &check, &numerator);
    if (!ed_fe_is_zero(&check)) {
        ed_fe_negate(&negative_numerator, &numerator);
        ed_fe_multiply(&check, &square, &denominator);
        ed_fe_sub(&check, &check, &negative_numerator);
        if (!ed_fe_is_zero(&check)) return 0;
        ed_fe_multiply(x, x, &ED_SQRT_M1);
    }
    {
        U256 normal;
        ed_fe_to_normal(&normal, x);
        if (u256_is_zero(&normal) && sign) return 0;
        if ((int)(normal.limb[0] & 1u) != sign)
            ed_fe_negate(x, x);
    }
    return 1;
}

static int
ed_points_equal(const EdPoint *left, const EdPoint *right)
{
    EdFe first, second, difference;
    int equal;
    ed_fe_multiply(&first, &left->x, &right->z);
    ed_fe_multiply(&second, &right->x, &left->z);
    ed_fe_sub(&difference, &first, &second);
    equal = ed_fe_is_zero(&difference);
    ed_fe_multiply(&first, &left->y, &right->z);
    ed_fe_multiply(&second, &right->y, &left->z);
    ed_fe_sub(&difference, &first, &second);
    return equal && ed_fe_is_zero(&difference);
}

static int
ed_decode_bytes(EdPoint *point, const unsigned char *data, Py_ssize_t length)
{
    unsigned char encoded[CURVE_BYTES] = {0};
    U256 y_normal;
    EdFe y, x;
    int sign;
    if (length > CURVE_BYTES) return 0;
    memcpy(encoded, data, (size_t)length);
    sign = encoded[31] >> 7;
    encoded[31] &= 0x7f;
    u256_from_bytes(&y_normal, encoded);
    if (u256_compare(&y_normal, &ED_FIELD.n) >= 0) return 0;
    ed_fe_from_normal(&y, &y_normal);
    if (!ed_recover_x_field(&x, &y, sign)) return 0;
    point->x = x;
    point->y = y;
    point->z = ED_FE_ONE;
    ed_fe_multiply(&point->t, &x, &y);
    return 1;
}

static void
ed_double_scalar(EdPoint *result, const U256 *k1, const EdPoint *p1,
                 const U256 *k2, const EdPoint *p2)
{
    int8_t digits1[CURVE_WNAF_BITS], digits2[CURVE_WNAF_BITS];
    EdPoint twice1, twice2, odd1[8], odd2[8];
    *result = ed_neutral();
    u256_wnaf(digits1, k1, 5);
    u256_wnaf(digits2, k2, 5);
    odd1[0] = *p1;
    odd2[0] = *p2;
    ed_double(&twice1, p1);
    ed_double(&twice2, p2);
    for (int index = 1; index < 8; index++) {
        ed_add(&odd1[index], &odd1[index - 1], &twice1);
        ed_add(&odd2[index], &odd2[index - 1], &twice2);
    }
    for (int index = CURVE_WNAF_BITS - 1; index >= 0; index--) {
        EdPoint next;
        ed_double(&next, result);
        *result = next;
        if (digits1[index] != 0) {
            int digit = digits1[index];
            int magnitude = digit < 0 ? -digit : digit;
            const EdPoint *addend = &odd1[(magnitude - 1) / 2];
            EdPoint negative;
            if (digit < 0) {
                ed_point_negate(&negative, addend);
                addend = &negative;
            }
            ed_add(&next, result, addend);
            *result = next;
        }
        if (digits2[index] != 0) {
            int digit = digits2[index];
            int magnitude = digit < 0 ? -digit : digit;
            const EdPoint *addend = &odd2[(magnitude - 1) / 2];
            EdPoint negative;
            if (digit < 0) {
                ed_point_negate(&negative, addend);
                addend = &negative;
            }
            ed_add(&next, result, addend);
            *result = next;
        }
    }
}

PyObject *
wreath_curve_ed_add(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *left_object, *right_object;
    EdPoint left, right, result;
    if (!PyArg_ParseTuple(args, "OO:curve_ed_add", &left_object, &right_object))
        return NULL;
    if (ed_point_from_py(left_object, &left) < 0 ||
        ed_point_from_py(right_object, &right) < 0) return NULL;
    ed_add(&result, &left, &right);
    return ed_point_to_py(&result);
}

PyObject *
wreath_curve_ed_negate(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *point_object;
    EdPoint point;
    if (!PyArg_ParseTuple(args, "O:curve_ed_negate", &point_object)) return NULL;
    if (ed_point_from_py(point_object, &point) < 0) return NULL;
    ed_point_negate(&point, &point);
    return ed_point_to_py(&point);
}

PyObject *
wreath_curve_ed_equal(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *left_object, *right_object;
    EdPoint left, right;
    if (!PyArg_ParseTuple(args, "OO:curve_ed_equal", &left_object, &right_object))
        return NULL;
    if (ed_point_from_py(left_object, &left) < 0 ||
        ed_point_from_py(right_object, &right) < 0) return NULL;
    return PyBool_FromLong(ed_points_equal(&left, &right));
}

PyObject *
wreath_curve_ed_scalar(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *scalar_object, *point_object;
    U256 scalar;
    EdPoint point, result;
    int secret;
    if (!PyArg_ParseTuple(args, "OOp:curve_ed_scalar", &scalar_object,
                          &point_object, &secret)) return NULL;
    if (scalar_from_py(scalar_object, &scalar) < 0 ||
        ed_point_from_py(point_object, &point) < 0) return NULL;
    if (secret) {
        scalar_reduce_ed(&scalar);
        (void)u256_add(&scalar, &scalar, &ED_ORDER);
        (void)u256_add(&scalar, &scalar, &ED_ORDER);
        if (ed_points_equal(&point, &ED_BASE_POINT))
            ed_scalar_base_secret(&result, &scalar);
        else
            ed_scalar(&result, &scalar, &point, 254, 1);
    }
    else {
        ed_scalar(&result, &scalar, &point, 256, 0);
    }
    return ed_point_to_py(&result);
}

PyObject *
wreath_curve_ed_double_scalar(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *k1_object, *p1_object, *k2_object, *p2_object;
    U256 k1, k2;
    EdPoint p1, p2, result;
    if (!PyArg_ParseTuple(args, "OOOO:curve_ed_double_scalar", &k1_object,
                          &p1_object, &k2_object, &p2_object)) return NULL;
    if (scalar_from_py(k1_object, &k1) < 0 ||
        scalar_from_py(k2_object, &k2) < 0 ||
        ed_point_from_py(p1_object, &p1) < 0 ||
        ed_point_from_py(p2_object, &p2) < 0) return NULL;
    ed_double_scalar(&result, &k1, &p1, &k2, &p2);
    return ed_point_to_py(&result);
}

PyObject *
wreath_curve_ed_verify(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *public_object, *digest_object, *signature_object;
    const unsigned char *public_bytes, *digest, *signature;
    Py_ssize_t public_length, digest_length, signature_length;
    U256 s, challenge;
    EdPoint public_point, r_point, negative_public, left;
    if (!PyArg_ParseTuple(args, "OOO:curve_ed_verify", &public_object,
                          &digest_object, &signature_object)) return NULL;
    if (PyBytes_AsStringAndSize(public_object, (char **)&public_bytes,
                                &public_length) < 0 ||
        PyBytes_AsStringAndSize(digest_object, (char **)&digest,
                                &digest_length) < 0 ||
        PyBytes_AsStringAndSize(signature_object, (char **)&signature,
                                &signature_length) < 0) return NULL;
    if (public_length != CURVE_BYTES || digest_length != 64 ||
        signature_length != 64) Py_RETURN_FALSE;
    if (!ed_decode_bytes(&public_point, public_bytes, public_length) ||
        !ed_decode_bytes(&r_point, signature, CURVE_BYTES)) Py_RETURN_FALSE;
    u256_from_bytes(&s, signature + CURVE_BYTES);
    if (u256_compare(&s, &ED_ORDER) >= 0) Py_RETURN_FALSE;
    scalar_reduce_le_bytes(&challenge, digest, digest_length, &ED_ORDER);
    ed_point_negate(&negative_public, &public_point);
    ed_double_scalar(&left, &s, &ED_BASE_POINT, &challenge, &negative_public);
    return PyBool_FromLong(ed_points_equal(&left, &r_point));
}

PyObject *
wreath_curve_ed_recover_x(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *y_object;
    U256 y_normal;
    EdFe y, x;
    int sign;
    int canonical;
    if (!PyArg_ParseTuple(args, "Op:curve_ed_recover_x", &y_object, &sign))
        return NULL;
    canonical = py_to_canonical_u256(y_object, &y_normal, &ED_FIELD.n);
    if (canonical < 0) return NULL;
    if (!canonical) Py_RETURN_NONE;
    ed_fe_from_normal(&y, &y_normal);
    if (!ed_recover_x_field(&x, &y, sign)) Py_RETURN_NONE;
    return ed_fe_to_py(&x);
}

PyObject *
wreath_curve_ed_decode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *data_object;
    const unsigned char *data;
    Py_ssize_t length;
    EdPoint point;
    if (!PyArg_ParseTuple(args, "O:curve_ed_decode", &data_object)) return NULL;
    if (PyBytes_AsStringAndSize(data_object, (char **)&data, &length) < 0)
        return NULL;
    if (!ed_decode_bytes(&point, data, length)) Py_RETURN_NONE;
    return ed_point_to_py(&point);
}

static int
ed_encode_bytes(unsigned char encoded[CURVE_BYTES], const EdPoint *point)
{
    EdFe inverse, x, y;
    U256 x_normal, y_normal;
    if (ed_fe_is_zero(&point->z)) {
        PyErr_SetString(PyExc_ValueError,
                        "cannot encode an edwards point with z = 0");
        return -1;
    }
    ed_fe_invert(&inverse, &point->z);
    ed_fe_multiply(&x, &point->x, &inverse);
    ed_fe_multiply(&y, &point->y, &inverse);
    ed_fe_to_normal(&x_normal, &x);
    ed_fe_to_normal(&y_normal, &y);
    u256_to_bytes(encoded, &y_normal);
    encoded[31] |= (unsigned char)((x_normal.limb[0] & 1u) << 7);
    return 0;
}

PyObject *
wreath_curve_ed_encode(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *point_object;
    EdPoint point;
    PyObject *result;
    unsigned char encoded[CURVE_BYTES];
    if (!PyArg_ParseTuple(args, "O:curve_ed_encode", &point_object)) return NULL;
    if (ed_point_from_py(point_object, &point) < 0) return NULL;
    if (ed_encode_bytes(encoded, &point) < 0) return NULL;
    result = PyBytes_FromStringAndSize(NULL, CURVE_BYTES);
    if (result == NULL) return NULL;
    memcpy(PyBytes_AS_STRING(result), encoded, CURVE_BYTES);
    return result;
}

static void
ed_private_scalar(U256 *scalar, const unsigned char expanded[64])
{
    unsigned char clamped[CURVE_BYTES];
    memcpy(clamped, expanded, sizeof(clamped));
    clamped[0] &= 248u;
    clamped[31] &= 63u;
    clamped[31] |= 64u;
    u256_from_bytes(scalar, clamped);
    curve_secure_zero(clamped, sizeof(clamped));
}

static int
ed_public_from_expanded(unsigned char public_key[CURVE_BYTES],
                        U256 *private_scalar,
                        const unsigned char expanded[64])
{
    EdPoint point;
    ed_private_scalar(private_scalar, expanded);
    ed_scalar_base_secret(&point, private_scalar);
    return ed_encode_bytes(public_key, &point);
}

PyObject *
wreath_curve_ed_public_key(PyObject *Py_UNUSED(self), PyObject *seed_object)
{
    const unsigned char *seed;
    Py_ssize_t seed_length;
    unsigned char expanded[64], public_key[CURVE_BYTES];
    U256 private_scalar;
    CurveSha512 hash;
    PyObject *result = NULL;
    if (PyBytes_AsStringAndSize(seed_object, (char **)&seed, &seed_length) < 0)
        return NULL;
    if (seed_length != CURVE_BYTES) {
        PyErr_SetString(PyExc_ValueError, "an Ed25519 seed is 32 bytes");
        return NULL;
    }
    sha512_init(&hash);
    sha512_update(&hash, seed, CURVE_BYTES);
    sha512_final(&hash, expanded);
    if (ed_public_from_expanded(public_key, &private_scalar, expanded) < 0)
        goto done;
    result = PyBytes_FromStringAndSize(NULL, CURVE_BYTES);
    if (result != NULL)
        memcpy(PyBytes_AS_STRING(result), public_key, CURVE_BYTES);
done:
    curve_secure_zero(&hash, sizeof(hash));
    curve_secure_zero(expanded, sizeof(expanded));
    curve_secure_zero(&private_scalar, sizeof(private_scalar));
    curve_secure_zero(public_key, sizeof(public_key));
    return result;
}

PyObject *
wreath_curve_ed_sign(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *seed_object, *message_object;
    const unsigned char *seed, *message;
    Py_ssize_t seed_length, message_length;
    unsigned char expanded[64], nonce_digest[64], challenge_digest[64];
    unsigned char public_key[CURVE_BYTES], signature[64];
    U256 private_scalar, reduced_private, nonce, challenge, product, s;
    EdPoint nonce_point;
    CurveSha512 hash;
    PyObject *result = NULL;
    if (!PyArg_ParseTuple(args, "OO:curve_ed_sign", &seed_object,
                          &message_object)) return NULL;
    if (PyBytes_AsStringAndSize(seed_object, (char **)&seed, &seed_length) < 0 ||
        PyBytes_AsStringAndSize(message_object, (char **)&message,
                                &message_length) < 0) return NULL;
    if (seed_length != CURVE_BYTES) {
        PyErr_SetString(PyExc_ValueError, "an Ed25519 seed is 32 bytes");
        return NULL;
    }

    sha512_init(&hash);
    sha512_update(&hash, seed, CURVE_BYTES);
    sha512_final(&hash, expanded);
    if (ed_public_from_expanded(public_key, &private_scalar, expanded) < 0)
        goto done;

    sha512_init(&hash);
    sha512_update(&hash, expanded + CURVE_BYTES, CURVE_BYTES);
    sha512_update(&hash, message, (size_t)message_length);
    sha512_final(&hash, nonce_digest);
    scalar_reduce_le_bytes(&nonce, nonce_digest, 64, &ED_ORDER);
    ed_scalar_base_secret(&nonce_point, &nonce);
    if (ed_encode_bytes(signature, &nonce_point) < 0) goto done;

    sha512_init(&hash);
    sha512_update(&hash, signature, CURVE_BYTES);
    sha512_update(&hash, public_key, CURVE_BYTES);
    sha512_update(&hash, message, (size_t)message_length);
    sha512_final(&hash, challenge_digest);
    scalar_reduce_le_bytes(&challenge, challenge_digest, 64, &ED_ORDER);
    reduced_private = private_scalar;
    scalar_reduce_ed(&reduced_private);
    scalar_multiply_ed(&product, &challenge, &reduced_private);
    scalar_add_ed(&s, &nonce, &product);
    u256_to_bytes(signature + CURVE_BYTES, &s);

    result = PyBytes_FromStringAndSize(NULL, sizeof(signature));
    if (result != NULL)
        memcpy(PyBytes_AS_STRING(result), signature, sizeof(signature));
done:
    curve_secure_zero(&hash, sizeof(hash));
    curve_secure_zero(expanded, sizeof(expanded));
    curve_secure_zero(nonce_digest, sizeof(nonce_digest));
    curve_secure_zero(challenge_digest, sizeof(challenge_digest));
    curve_secure_zero(&private_scalar, sizeof(private_scalar));
    curve_secure_zero(&reduced_private, sizeof(reduced_private));
    curve_secure_zero(&nonce, sizeof(nonce));
    curve_secure_zero(&challenge, sizeof(challenge));
    curve_secure_zero(&product, sizeof(product));
    curve_secure_zero(&s, sizeof(s));
    curve_secure_zero(&nonce_point, sizeof(nonce_point));
    curve_secure_zero(public_key, sizeof(public_key));
    curve_secure_zero(signature, sizeof(signature));
    return result;
}

static int
p256_affine_from_py(PyObject *object, U256 *x, U256 *y)
{
    PyObject *sequence = PySequence_Fast(object,
        "P-256 affine point must have two coordinates");
    if (sequence == NULL) return -1;
    if (PySequence_Fast_GET_SIZE(sequence) != 2) {
        Py_DECREF(sequence);
        PyErr_SetString(PyExc_ValueError,
                        "P-256 affine point must have two coordinates");
        return -1;
    }
    if (field_from_py(x, PySequence_Fast_GET_ITEM(sequence, 0), &P256_FIELD,
                      "P-256 coordinate must be in [0, p)") < 0 ||
        field_from_py(y, PySequence_Fast_GET_ITEM(sequence, 1), &P256_FIELD,
                      "P-256 coordinate must be in [0, p)") < 0) {
        Py_DECREF(sequence);
        return -1;
    }
    Py_DECREF(sequence);
    return 0;
}

static P256Point
p256_infinity(void)
{
    P256Point result = {P256_FIELD.one, P256_FIELD.one, U256_ZERO};
    return result;
}

static uint32_t
p256_is_infinity(const P256Point *point)
{
    return u256_is_zero(&point->z);
}

static void
p256_select(P256Point *result, const P256Point *when_zero,
            const P256Point *when_one, uint32_t bit)
{
    u256_select(&result->x, &when_zero->x, &when_one->x, bit);
    u256_select(&result->y, &when_zero->y, &when_one->y, bit);
    u256_select(&result->z, &when_zero->z, &when_one->z, bit);
}

static void
p256_double(P256Point *result, const P256Point *point)
{
    U256 delta, gamma, beta, alpha, left, right, square;
    field_square(&delta, &point->z, &P256_FIELD);
    field_square(&gamma, &point->y, &P256_FIELD);
    field_multiply(&beta, &point->x, &gamma, &P256_FIELD);
    field_sub(&left, &point->x, &delta, &P256_FIELD);
    field_add(&right, &point->x, &delta, &P256_FIELD);
    field_multiply(&square, &left, &right, &P256_FIELD);
    field_small_multiply(&alpha, &square, 3, &P256_FIELD);
    field_square(&square, &alpha, &P256_FIELD);
    field_small_multiply(&right, &beta, 8, &P256_FIELD);
    field_sub(&result->x, &square, &right, &P256_FIELD);
    field_add(&left, &point->y, &point->z, &P256_FIELD);
    field_square(&square, &left, &P256_FIELD);
    field_sub(&left, &square, &gamma, &P256_FIELD);
    field_sub(&result->z, &left, &delta, &P256_FIELD);
    field_small_multiply(&left, &beta, 4, &P256_FIELD);
    field_sub(&right, &left, &result->x, &P256_FIELD);
    field_multiply(&left, &alpha, &right, &P256_FIELD);
    field_square(&square, &gamma, &P256_FIELD);
    field_small_multiply(&right, &square, 8, &P256_FIELD);
    field_sub(&result->y, &left, &right, &P256_FIELD);
}

static void
p256_add_affine(P256Point *result, const P256Point *point,
                const U256 *affine_x, const U256 *affine_y,
                int constant_shape)
{
    U256 zz, u2, s2, h, r, hh, i, j, v, left, right;
    uint32_t was_infinity = p256_is_infinity(point);
    uint32_t h_zero, r_zero;
    if (!constant_shape && was_infinity) {
        result->x = *affine_x;
        result->y = *affine_y;
        result->z = P256_FIELD.one;
        return;
    }
    field_square(&zz, &point->z, &P256_FIELD);
    field_multiply(&u2, affine_x, &zz, &P256_FIELD);
    field_multiply(&left, affine_y, &point->z, &P256_FIELD);
    field_multiply(&s2, &left, &zz, &P256_FIELD);
    field_sub(&h, &u2, &point->x, &P256_FIELD);
    field_sub(&r, &s2, &point->y, &P256_FIELD);
    h_zero = u256_is_zero(&h);
    r_zero = u256_is_zero(&r);
    if (!constant_shape && h_zero) {
        if (r_zero) p256_double(result, point);
        else *result = p256_infinity();
        return;
    }
    field_square(&hh, &h, &P256_FIELD);
    field_small_multiply(&i, &hh, 4, &P256_FIELD);
    field_multiply(&j, &h, &i, &P256_FIELD);
    field_add(&r, &r, &r, &P256_FIELD);
    field_multiply(&v, &point->x, &i, &P256_FIELD);
    field_square(&left, &r, &P256_FIELD);
    field_small_multiply(&right, &v, 2, &P256_FIELD);
    field_sub(&result->x, &left, &j, &P256_FIELD);
    field_sub(&result->x, &result->x, &right, &P256_FIELD);
    field_sub(&left, &v, &result->x, &P256_FIELD);
    field_multiply(&right, &r, &left, &P256_FIELD);
    field_multiply(&left, &point->y, &j, &P256_FIELD);
    field_add(&left, &left, &left, &P256_FIELD);
    field_sub(&result->y, &right, &left, &P256_FIELD);
    field_add(&left, &point->z, &h, &P256_FIELD);
    field_square(&right, &left, &P256_FIELD);
    field_sub(&left, &right, &zz, &P256_FIELD);
    field_sub(&result->z, &left, &hh, &P256_FIELD);
    if (constant_shape) {
        P256Point chosen = *result;
        P256Point infinity = p256_infinity();
        P256Point affine = {*affine_x, *affine_y, P256_FIELD.one};
        /* This arm is used only for (2 * prefix)G + G in the fixed-shape
         * secret ladder. Equality would require 2*prefix == 1 mod n, which a
         * prefix of a scalar below n cannot reach. h == 0 is therefore the
         * inverse case; the infinity input is selected separately below. */
        p256_select(&chosen, &chosen, &infinity, h_zero);
        p256_select(result, &chosen, &affine, was_infinity);
    }
}

/* 1 means affine output, 0 means the identity. */
static int
p256_to_affine(U256 *x, U256 *y, const P256Point *point)
{
    U256 inverse, inverse2, partial;
    if (p256_is_infinity(point)) return 0;
    field_power_window4(&inverse, &point->z, &P256_INVERSE_EXPONENT,
                        &P256_FIELD);
    field_square(&inverse2, &inverse, &P256_FIELD);
    field_multiply(x, &point->x, &inverse2, &P256_FIELD);
    field_multiply(&partial, &point->y, &inverse2, &P256_FIELD);
    field_multiply(y, &partial, &inverse, &P256_FIELD);
    return 1;
}

static void
p256_pair_to_affine(U256 *first_x, U256 *first_y,
                    U256 *second_x, U256 *second_y,
                    const P256Point *first, const P256Point *second)
{
    U256 product, inverse, first_inverse, second_inverse;
    U256 inverse2, partial;
    field_multiply(&product, &first->z, &second->z, &P256_FIELD);
    field_power_window4(&inverse, &product, &P256_INVERSE_EXPONENT,
                        &P256_FIELD);
    field_multiply(&first_inverse, &inverse, &second->z, &P256_FIELD);
    field_multiply(&second_inverse, &inverse, &first->z, &P256_FIELD);
    field_square(&inverse2, &first_inverse, &P256_FIELD);
    field_multiply(first_x, &first->x, &inverse2, &P256_FIELD);
    field_multiply(&partial, &first->y, &inverse2, &P256_FIELD);
    field_multiply(first_y, &partial, &first_inverse, &P256_FIELD);
    field_square(&inverse2, &second_inverse, &P256_FIELD);
    field_multiply(second_x, &second->x, &inverse2, &P256_FIELD);
    field_multiply(&partial, &second->y, &inverse2, &P256_FIELD);
    field_multiply(second_y, &partial, &second_inverse, &P256_FIELD);
}

static PyObject *
p256_affine_to_py(const P256Point *point)
{
    U256 x, y;
    PyObject *x_object = NULL, *y_object = NULL;
    if (!p256_to_affine(&x, &y, point)) Py_RETURN_NONE;
    x_object = field_to_py(&x, &P256_FIELD);
    y_object = field_to_py(&y, &P256_FIELD);
    return wreath_tuple2_from_owned(x_object, y_object);
}

static int
p256_on_curve_field(const U256 *x, const U256 *y)
{
    U256 left, x2, x3, three_x, right, difference;
    field_square(&left, y, &P256_FIELD);
    field_square(&x2, x, &P256_FIELD);
    field_multiply(&x3, &x2, x, &P256_FIELD);
    field_small_multiply(&three_x, x, 3, &P256_FIELD);
    field_sub(&right, &x3, &three_x, &P256_FIELD);
    field_add(&right, &right, &P256_B, &P256_FIELD);
    field_sub(&difference, &left, &right, &P256_FIELD);
    return u256_is_zero(&difference);
}

PyObject *
wreath_curve_p256_on_curve(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *x_object, *y_object;
    U256 x_normal, y_normal, x, y;
    int x_ok, y_ok;
    if (!PyArg_ParseTuple(args, "OO:curve_p256_on_curve", &x_object, &y_object))
        return NULL;
    x_ok = py_to_canonical_u256(x_object, &x_normal, &P256_FIELD.n);
    y_ok = py_to_canonical_u256(y_object, &y_normal, &P256_FIELD.n);
    if (x_ok < 0 || y_ok < 0) return NULL;
    if (!x_ok || !y_ok) Py_RETURN_FALSE;
    field_from_normal(&x, &x_normal, &P256_FIELD);
    field_from_normal(&y, &y_normal, &P256_FIELD);
    return PyBool_FromLong(p256_on_curve_field(&x, &y));
}

static void
p256_double_scalar(P256Point *result, const U256 *k1,
                   const U256 *p1x, const U256 *p1y,
                   const U256 *k2, const U256 *p2x, const U256 *p2y)
{
    int8_t digits1[CURVE_WNAF_BITS], digits2[CURVE_WNAF_BITS];
    U256 three1x, three1y, three2x, three2y;
    P256Point p1 = {*p1x, *p1y, P256_FIELD.one};
    P256Point p2 = {*p2x, *p2y, P256_FIELD.one};
    P256Point twice1, twice2, three1, three2;
    u256_wnaf(digits1, k1, 3);
    u256_wnaf(digits2, k2, 3);
    p256_double(&twice1, &p1);
    p256_double(&twice2, &p2);
    p256_add_affine(&three1, &twice1, p1x, p1y, 0);
    p256_add_affine(&three2, &twice2, p2x, p2y, 0);
    p256_pair_to_affine(&three1x, &three1y, &three2x, &three2y,
                        &three1, &three2);
    *result = p256_infinity();
    for (int index = CURVE_WNAF_BITS - 1; index >= 0; index--) {
        P256Point next;
        p256_double(&next, result);
        *result = next;
        if (digits1[index] != 0) {
            int digit = digits1[index];
            const U256 *add_x = digit == 1 || digit == -1 ? p1x : &three1x;
            const U256 *add_y = digit == 1 || digit == -1 ? p1y : &three1y;
            U256 negative_y;
            if (digit < 0) {
                field_negate(&negative_y, add_y, &P256_FIELD);
                add_y = &negative_y;
            }
            p256_add_affine(&next, result, add_x, add_y, 0);
            *result = next;
        }
        if (digits2[index] != 0) {
            int digit = digits2[index];
            const U256 *add_x = digit == 1 || digit == -1 ? p2x : &three2x;
            const U256 *add_y = digit == 1 || digit == -1 ? p2y : &three2y;
            U256 negative_y;
            if (digit < 0) {
                field_negate(&negative_y, add_y, &P256_FIELD);
                add_y = &negative_y;
            }
            p256_add_affine(&next, result, add_x, add_y, 0);
            *result = next;
        }
    }
}

/* Add projective points known to be distinct and non-inverse.  Verification
 * uses this only for (2Q) + (2i+1)Q, where Q has the prime P-256 order. */
static void
p256_add_distinct(P256Point *result, const P256Point *left,
                  const P256Point *right)
{
    U256 z1z1, z2z2, u1, u2, s1, s2, h, i, j, r, v, partial, other;
    field_square(&z1z1, &left->z, &P256_FIELD);
    field_square(&z2z2, &right->z, &P256_FIELD);
    field_multiply(&u1, &left->x, &z2z2, &P256_FIELD);
    field_multiply(&u2, &right->x, &z1z1, &P256_FIELD);
    field_multiply(&partial, &right->z, &z2z2, &P256_FIELD);
    field_multiply(&s1, &left->y, &partial, &P256_FIELD);
    field_multiply(&partial, &left->z, &z1z1, &P256_FIELD);
    field_multiply(&s2, &right->y, &partial, &P256_FIELD);
    field_sub(&h, &u2, &u1, &P256_FIELD);
    field_add(&partial, &h, &h, &P256_FIELD);
    field_square(&i, &partial, &P256_FIELD);
    field_multiply(&j, &h, &i, &P256_FIELD);
    field_sub(&partial, &s2, &s1, &P256_FIELD);
    field_add(&r, &partial, &partial, &P256_FIELD);
    field_multiply(&v, &u1, &i, &P256_FIELD);
    field_square(&partial, &r, &P256_FIELD);
    field_sub(&partial, &partial, &j, &P256_FIELD);
    field_add(&other, &v, &v, &P256_FIELD);
    field_sub(&result->x, &partial, &other, &P256_FIELD);
    field_sub(&partial, &v, &result->x, &P256_FIELD);
    field_multiply(&partial, &r, &partial, &P256_FIELD);
    field_multiply(&other, &s1, &j, &P256_FIELD);
    field_add(&other, &other, &other, &P256_FIELD);
    field_sub(&result->y, &partial, &other, &P256_FIELD);
    field_add(&partial, &left->z, &right->z, &P256_FIELD);
    field_square(&partial, &partial, &P256_FIELD);
    field_sub(&partial, &partial, &z1z1, &P256_FIELD);
    field_sub(&partial, &partial, &z2z2, &P256_FIELD);
    field_multiply(&result->z, &partial, &h, &P256_FIELD);
}

/* Convert eight non-identity projective points with one inversion. */
static void
p256_batch_to_affine_8(U256 x[8], U256 y[8], const P256Point points[8])
{
    U256 prefix[8], inverse, point_inverse, inverse2, partial;
    prefix[0] = points[0].z;
    for (int index = 1; index < 8; index++)
        field_multiply(&prefix[index], &prefix[index - 1],
                       &points[index].z, &P256_FIELD);
    field_power_window4(&inverse, &prefix[7], &P256_INVERSE_EXPONENT,
                        &P256_FIELD);
    for (int index = 7; index >= 0; index--) {
        if (index == 0)
            point_inverse = inverse;
        else
            field_multiply(&point_inverse, &inverse, &prefix[index - 1],
                           &P256_FIELD);
        field_multiply(&inverse, &inverse, &points[index].z, &P256_FIELD);
        field_square(&inverse2, &point_inverse, &P256_FIELD);
        field_multiply(&x[index], &points[index].x, &inverse2, &P256_FIELD);
        field_multiply(&partial, &points[index].y, &inverse2, &P256_FIELD);
        field_multiply(&y[index], &partial, &point_inverse, &P256_FIELD);
    }
}

/* Verification always multiplies the first scalar by G.  Use the immutable
 * odd entries from the signing table and build the arbitrary point's eight
 * odd multiples with one operation-local batch inversion. */
static void
p256_double_scalar_base(P256Point *result, const U256 *base_scalar,
                        const U256 *point_scalar,
                        const U256 *point_x, const U256 *point_y)
{
    int8_t base_digits[CURVE_WNAF_BITS], point_digits[CURVE_WNAF_BITS];
    U256 base_x[8], base_y[8], point_table_x[8], point_table_y[8], normal;
    P256Point point_table[8];
    P256Point twice;
    u256_wnaf(base_digits, base_scalar, 5);
    u256_wnaf(point_digits, point_scalar, 5);
    for (int index = 0; index < 8; index++) {
        int table_index = index * 2;
        u256_from_big_endian(&normal, P256_BASE_WINDOW[table_index]);
        field_from_normal(&base_x[index], &normal, &P256_FIELD);
        u256_from_big_endian(&normal,
                            P256_BASE_WINDOW[table_index] + CURVE_BYTES);
        field_from_normal(&base_y[index], &normal, &P256_FIELD);
    }
    point_table[0].x = *point_x;
    point_table[0].y = *point_y;
    point_table[0].z = P256_FIELD.one;
    p256_double(&twice, &point_table[0]);
    for (int index = 1; index < 8; index++)
        p256_add_distinct(&point_table[index], &point_table[index - 1],
                          &twice);
    p256_batch_to_affine_8(point_table_x, point_table_y, point_table);
    *result = p256_infinity();
    for (int index = CURVE_WNAF_BITS - 1; index >= 0; index--) {
        P256Point next;
        p256_double(&next, result);
        *result = next;
        if (base_digits[index] != 0) {
            int digit = base_digits[index];
            int magnitude = digit < 0 ? -digit : digit;
            const U256 *add_y = &base_y[(magnitude - 1) / 2];
            U256 negative_y;
            if (digit < 0) {
                field_negate(&negative_y, add_y, &P256_FIELD);
                add_y = &negative_y;
            }
            p256_add_affine(&next, result,
                            &base_x[(magnitude - 1) / 2], add_y, 0);
            *result = next;
        }
        if (point_digits[index] != 0) {
            int digit = point_digits[index];
            int magnitude = digit < 0 ? -digit : digit;
            const U256 *add_x = &point_table_x[(magnitude - 1) / 2];
            const U256 *add_y = &point_table_y[(magnitude - 1) / 2];
            U256 negative_y;
            if (digit < 0) {
                field_negate(&negative_y, add_y, &P256_FIELD);
                add_y = &negative_y;
            }
            p256_add_affine(&next, result, add_x, add_y, 0);
            *result = next;
        }
    }
}

PyObject *
wreath_curve_p256_double_scalar(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *k1_object, *p1_object, *k2_object, *p2_object;
    U256 k1, k2, p1x, p1y, p2x, p2y;
    P256Point result;
    if (!PyArg_ParseTuple(args, "OOOO:curve_p256_double_scalar", &k1_object,
                          &p1_object, &k2_object, &p2_object)) return NULL;
    if (scalar_from_py(k1_object, &k1) < 0 ||
        scalar_from_py(k2_object, &k2) < 0 ||
        p256_affine_from_py(p1_object, &p1x, &p1y) < 0 ||
        p256_affine_from_py(p2_object, &p2x, &p2y) < 0) return NULL;
    p256_double_scalar(&result, &k1, &p1x, &p1y, &k2, &p2x, &p2y);
    return p256_affine_to_py(&result);
}

PyObject *
wreath_curve_p256_verify(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *x_object, *y_object, *digest_object, *signature_object;
    const unsigned char *digest, *signature;
    Py_ssize_t digest_length, signature_length;
    U256 x_normal, y_normal, x, y, r, s, z, reduced;
    U256 s_field, inverse, z_field, r_field, u1_field, u2_field, u1, u2;
    U256 result_x, result_y;
    P256Point point;
    CurveWord borrow;
    int x_ok, y_ok;
    if (!PyArg_ParseTuple(args, "OOOO:curve_p256_verify", &x_object, &y_object,
                          &digest_object, &signature_object)) return NULL;
    if (PyBytes_AsStringAndSize(digest_object, (char **)&digest,
                                &digest_length) < 0 ||
        PyBytes_AsStringAndSize(signature_object, (char **)&signature,
                                &signature_length) < 0) return NULL;
    if (digest_length != CURVE_BYTES || signature_length != 64) Py_RETURN_FALSE;
    x_ok = py_to_canonical_u256(x_object, &x_normal, &P256_FIELD.n);
    y_ok = py_to_canonical_u256(y_object, &y_normal, &P256_FIELD.n);
    if (x_ok < 0 || y_ok < 0) return NULL;
    if (!x_ok || !y_ok) Py_RETURN_FALSE;
    field_from_normal(&x, &x_normal, &P256_FIELD);
    field_from_normal(&y, &y_normal, &P256_FIELD);
    if (!p256_on_curve_field(&x, &y)) Py_RETURN_FALSE;
    u256_from_big_endian(&r, signature);
    u256_from_big_endian(&s, signature + CURVE_BYTES);
    if (u256_is_zero(&r) || u256_is_zero(&s) ||
        u256_compare(&r, &P256_ORDER) >= 0 ||
        u256_compare(&s, &P256_ORDER) >= 0) Py_RETURN_FALSE;
    u256_from_big_endian(&z, digest);
    borrow = u256_sub(&reduced, &z, &P256_ORDER);
    u256_select(&z, &z, &reduced, borrow ^ 1u);
    field_from_normal(&s_field, &s, &P256_SCALAR_FIELD);
    field_power_window4(&inverse, &s_field, &P256_SCALAR_INVERSE_EXPONENT,
                        &P256_SCALAR_FIELD);
    field_from_normal(&z_field, &z, &P256_SCALAR_FIELD);
    field_from_normal(&r_field, &r, &P256_SCALAR_FIELD);
    field_multiply(&u1_field, &z_field, &inverse, &P256_SCALAR_FIELD);
    field_multiply(&u2_field, &r_field, &inverse, &P256_SCALAR_FIELD);
    field_to_normal(&u1, &u1_field, &P256_SCALAR_FIELD);
    field_to_normal(&u2, &u2_field, &P256_SCALAR_FIELD);
    p256_double_scalar_base(&point, &u1, &u2, &x, &y);
    if (!p256_to_affine(&result_x, &result_y, &point)) Py_RETURN_FALSE;
    field_to_normal(&result_x, &result_x, &P256_FIELD);
    borrow = u256_sub(&reduced, &result_x, &P256_ORDER);
    u256_select(&result_x, &result_x, &reduced, borrow ^ 1u);
    return PyBool_FromLong(u256_compare(&result_x, &r) == 0);
}

static void
p256_scalar_secret(P256Point *result, const U256 *scalar,
                   const U256 *x, const U256 *y)
{
    *result = p256_infinity();
    for (int index = 255; index >= 0; index--) {
        P256Point doubled, stepped;
        p256_double(&doubled, result);
        p256_add_affine(&stepped, &doubled, x, y, 1);
        p256_select(result, &doubled, &stepped, u256_bit(scalar, index));
    }
}

static void
p256_scalar_base_secret(P256Point *result, const U256 *scalar)
{
    U256 table_x[15], table_y[15];
    for (int index = 0; index < 15; index++) {
        U256 normal;
        u256_from_big_endian(&normal, P256_BASE_WINDOW[index]);
        field_from_normal(&table_x[index], &normal, &P256_FIELD);
        u256_from_big_endian(&normal, P256_BASE_WINDOW[index] + CURVE_BYTES);
        field_from_normal(&table_y[index], &normal, &P256_FIELD);
    }
    *result = p256_infinity();
    for (int nibble = 63; nibble >= 0; nibble--) {
        CurveWord digit = u256_nibble(scalar, nibble);
        U256 selected_x = table_x[0];
        U256 selected_y = table_y[0];
        P256Point stepped;
        for (int square = 0; square < 4; square++) {
            P256Point doubled;
            p256_double(&doubled, result);
            *result = doubled;
        }
        for (CurveWord choice = 2; choice <= 15; choice++) {
            CurveWord equal = ((digit ^ choice) - 1u) >>
                              (CURVE_WORD_BITS - 1);
            U256 chosen;
            u256_select(&chosen, &selected_x, &table_x[choice - 1], equal);
            selected_x = chosen;
            u256_select(&chosen, &selected_y, &table_y[choice - 1], equal);
            selected_y = chosen;
        }
        p256_add_affine(&stepped, result, &selected_x, &selected_y, 1);
        {
            CurveWord nonzero = (digit | (0u - digit)) >>
                                (CURVE_WORD_BITS - 1);
            P256Point unchanged = *result;
            p256_select(result, &unchanged, &stepped, (uint32_t)nonzero);
        }
    }
    curve_secure_zero(table_x, sizeof(table_x));
    curve_secure_zero(table_y, sizeof(table_y));
}

PyObject *
wreath_curve_p256_scalar(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *scalar_object, *point_object;
    U256 scalar, x, y;
    P256Point result = p256_infinity();
    if (!PyArg_ParseTuple(args, "OO:curve_p256_scalar", &scalar_object,
                          &point_object)) return NULL;
    if (scalar_from_py(scalar_object, &scalar) < 0) {
        if (PyErr_ExceptionMatches(PyExc_OverflowError)) PyErr_Clear();
        PyErr_SetString(PyExc_ValueError, "a P-256 scalar is in [1, n)");
        return NULL;
    }
    if (u256_is_zero(&scalar) || u256_compare(&scalar, &P256_ORDER) >= 0) {
        PyErr_SetString(PyExc_ValueError, "a P-256 scalar is in [1, n)");
        return NULL;
    }
    if (p256_affine_from_py(point_object, &x, &y) < 0) return NULL;
    if (u256_compare(&x, &P256_GX) == 0 &&
        u256_compare(&y, &P256_GY) == 0)
        p256_scalar_base_secret(&result, &scalar);
    else
        p256_scalar_secret(&result, &scalar, &x, &y);
    return p256_affine_to_py(&result);
}

PyObject *
wreath_curve_p256_sign(PyObject *Py_UNUSED(self), PyObject *args)
{
    PyObject *private_object, *digest_object, *nonce_object;
    const unsigned char *digest, *nonce_bytes;
    Py_ssize_t digest_length, nonce_length;
    U256 private_scalar, z, reduced, nonce_modulus, k, r, s, half_order;
    U256 high_s_threshold, low_s;
    U256 k_field, inverse, r_field, private_field, z_field, product, sum;
    U256 signature_scalar;
    P256Point point;
    CurveWord borrow;
    unsigned char signature[64];
    PyObject *result = NULL;
    int private_ok;
    if (!PyArg_ParseTuple(args, "OOO:curve_p256_sign", &private_object,
                          &digest_object, &nonce_object)) return NULL;
    if (PyBytes_AsStringAndSize(digest_object, (char **)&digest,
                                &digest_length) < 0 ||
        PyBytes_AsStringAndSize(nonce_object, (char **)&nonce_bytes,
                                &nonce_length) < 0) return NULL;
    if (digest_length != CURVE_BYTES) {
        PyErr_SetString(PyExc_ValueError, "a P-256 signing digest is 32 bytes");
        return NULL;
    }
    if (nonce_length != 64) {
        PyErr_SetString(PyExc_ValueError, "a P-256 nonce digest is 64 bytes");
        return NULL;
    }
    private_ok = py_to_canonical_u256(private_object, &private_scalar,
                                      &P256_ORDER);
    if (private_ok < 0) return NULL;
    if (!private_ok || u256_is_zero(&private_scalar)) {
        PyErr_SetString(PyExc_ValueError, "a P-256 private scalar is in [1, n)");
        return NULL;
    }

    nonce_modulus = P256_ORDER;
    nonce_modulus.limb[0] -= 1u;
    scalar_reduce_be_bytes(&k, nonce_bytes, nonce_length, &nonce_modulus);
    (void)u256_add(&k, &k, &U256_ONE);
    p256_scalar_base_secret(&point, &k);
    if (!p256_to_affine(&r, &reduced, &point)) goto done;
    field_to_normal(&r, &r, &P256_FIELD);
    borrow = u256_sub(&reduced, &r, &P256_ORDER);
    u256_select(&r, &r, &reduced, borrow ^ 1u);
    if (u256_is_zero(&r)) goto done;

    u256_from_big_endian(&z, digest);
    borrow = u256_sub(&reduced, &z, &P256_ORDER);
    u256_select(&z, &z, &reduced, borrow ^ 1u);
    field_from_normal(&k_field, &k, &P256_SCALAR_FIELD);
    field_power_window4(&inverse, &k_field, &P256_SCALAR_INVERSE_EXPONENT,
                        &P256_SCALAR_FIELD);
    field_from_normal(&r_field, &r, &P256_SCALAR_FIELD);
    field_from_normal(&private_field, &private_scalar, &P256_SCALAR_FIELD);
    field_from_normal(&z_field, &z, &P256_SCALAR_FIELD);
    field_multiply(&product, &r_field, &private_field, &P256_SCALAR_FIELD);
    field_add(&sum, &z_field, &product, &P256_SCALAR_FIELD);
    field_multiply(&signature_scalar, &inverse, &sum, &P256_SCALAR_FIELD);
    field_to_normal(&s, &signature_scalar, &P256_SCALAR_FIELD);
    if (u256_is_zero(&s)) goto done;

    half_order = P256_ORDER;
    for (int index = 0; index < CURVE_LIMBS; index++) {
        CurveWord high = index + 1 < CURVE_LIMBS ?
                         half_order.limb[index + 1] : 0;
        half_order.limb[index] = (half_order.limb[index] >> 1) |
            (high << (CURVE_WORD_BITS - 1));
    }
    (void)u256_add(&high_s_threshold, &half_order, &U256_ONE);
    borrow = u256_sub(&reduced, &s, &high_s_threshold);
    (void)u256_sub(&low_s, &P256_ORDER, &s);
    u256_select(&s, &low_s, &s, borrow);
    u256_to_bytes(signature, &r);
    u256_to_bytes(signature + CURVE_BYTES, &s);
    for (int index = 0; index < CURVE_BYTES / 2; index++) {
        unsigned char swap = signature[index];
        signature[index] = signature[CURVE_BYTES - 1 - index];
        signature[CURVE_BYTES - 1 - index] = swap;
        swap = signature[CURVE_BYTES + index];
        signature[CURVE_BYTES + index] = signature[63 - index];
        signature[63 - index] = swap;
    }
    result = PyBytes_FromStringAndSize(NULL, sizeof(signature));
    if (result != NULL)
        memcpy(PyBytes_AS_STRING(result), signature, sizeof(signature));
done:
    curve_secure_zero(&private_scalar, sizeof(private_scalar));
    curve_secure_zero(&z, sizeof(z));
    curve_secure_zero(&k, sizeof(k));
    curve_secure_zero(&s, sizeof(s));
    curve_secure_zero(&low_s, sizeof(low_s));
    curve_secure_zero(&k_field, sizeof(k_field));
    curve_secure_zero(&inverse, sizeof(inverse));
    curve_secure_zero(&private_field, sizeof(private_field));
    curve_secure_zero(&product, sizeof(product));
    curve_secure_zero(&sum, sizeof(sum));
    curve_secure_zero(&signature_scalar, sizeof(signature_scalar));
    curve_secure_zero(&point, sizeof(point));
    curve_secure_zero(signature, sizeof(signature));
    return result;
}

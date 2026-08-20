/* Length-limited canonical Huffman construction for DEFLATE (RFC 1951 §3.2).
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Two paths, and the choice between them is the whole design.
 *
 * The common case is an ordinary two-queue Huffman tree: leaves arrive already
 * sorted, internal nodes are produced in non-decreasing order, so no heap is
 * needed and the whole thing is linear in the alphabet. Almost always its
 * deepest code already fits in DEFLATE's 15-bit limit, and then it is both
 * optimal and cheap.
 *
 * When it does not fit, the answer is package-merge, which is exactly optimal
 * *subject to* the limit. Arm 2 first clamped the long codes and repaired the
 * Kraft sum with a heuristic; that repair failed on ordinary English prose, the
 * caller silently fell back to fixed Huffman, and the only symptom was a 21%
 * worse ratio with nothing logged anywhere. A cost that hides as ratio is worse
 * than one that hides as time. Package-merge has no such failure mode.
 *
 * Derived from arm 2's src/huff.c (MPL-2.0, this project). The frequency-sort
 * is arm 3's — insertion below 40 symbols, two-pass radix above — which is
 * cheaper than the bottom-up merge arm 2 used.
 */
#include "huff.h"

#include <string.h>

static void sort_by_freq(const uint32_t *freq, uint32_t *ord, uint32_t *tmp, int m) {
  if (m <= 40) { /* insertion sort wins outright at these sizes */
    for (int i = 1; i < m; i++) {
      uint32_t v = ord[i];
      uint32_t f = freq[v];
      int j = i;
      while (j && freq[ord[j - 1]] > f) { ord[j] = ord[j - 1]; j--; }
      ord[j] = v;
    }
    return;
  }
  unsigned cnt[256];
  for (int pass = 0; pass < 2; pass++) {
    unsigned sh = (unsigned)pass * 8;
    memset(cnt, 0, sizeof cnt);
    for (int i = 0; i < m; i++) cnt[(freq[ord[i]] >> sh) & 0xff]++;
    unsigned run = 0;
    for (int i = 0; i < 256; i++) { unsigned c = cnt[i]; cnt[i] = run; run += c; }
    for (int i = 0; i < m; i++) tmp[cnt[(freq[ord[i]] >> sh) & 0xff]++] = ord[i];
    memcpy(ord, tmp, (size_t)m * sizeof *ord);
  }
  /* Two passes cover frequencies below 65536. A block never holds more tokens
   * than that, so a symbol's count cannot exceed it. */
}

/* Exactly optimal under the length limit, and correspondingly expensive: about
 * maxlen merge passes over a list of up to 2m items.
 *
 * Coin-collector form: for each symbol there is one coin of each denomination
 * 2^-1 .. 2^-maxlen, all carrying that symbol's frequency as their value.
 * Choose coins totalling exactly m-1 at least cost; a symbol's code length is
 * the number of its coins chosen. Lists are built from the smallest
 * denomination upward, and because every merge takes the cheapest item first,
 * the leaves chosen at any level are always a prefix of the frequency-sorted
 * symbols -- which is what makes the selection recoverable from counts alone. */
static int package_merge(const uint32_t *freq, const uint32_t *used, int m, int maxlen,
                         uint8_t *len) {
  uint64_t a[GZ_HUFF_MAXSYM];
  for (int i = 0; i < m; i++) a[i] = freq[used[i]];

  /* |list_l| = m + |list_{l-1}|/2 < 2m for every l, so 2m slots suffice. */
  uint64_t cur[2 * GZ_HUFF_MAXSYM], prv[2 * GZ_HUFF_MAXSYM];
  uint8_t isleaf[16][2 * GZ_HUFF_MAXSYM];
  int cnt[16], pn = m;
  for (int i = 0; i < m; i++) prv[i] = a[i];
  cnt[1] = m;
  for (int l = 2; l <= maxlen; l++) {
    int np = pn / 2, i = 0, j = 0, k = 0;
    while (i < m || j < np) {
      uint64_t pv = (j < np) ? prv[2 * j] + prv[2 * j + 1] : UINT64_MAX;
      if (i < m && (j >= np || a[i] <= pv)) {
        cur[k] = a[i++];
        isleaf[l][k] = 1;
      } else {
        cur[k] = pv;
        j++;
        isleaf[l][k] = 0;
      }
      k++;
    }
    memcpy(prv, cur, (size_t)k * sizeof *prv);
    pn = k;
    cnt[l] = k;
  }

  /* Walk back down: s items are taken at the top, and each package among them
   * accounts for two items one level below. */
  int take[16], s = 2 * m - 2;
  for (int l = maxlen; l >= 2; l--) {
    if (s > cnt[l]) return -1;
    int leaves = 0;
    for (int k = 0; k < s; k++) leaves += isleaf[l][k];
    take[l] = leaves;
    s = 2 * (s - leaves);
  }
  if (s > m) return -1;
  take[1] = s; /* the smallest-denomination list is all leaves */

  for (int i = 0; i < m; i++) {
    int l = 0;
    for (int lv = 1; lv <= maxlen; lv++) l += take[lv] > i;
    if (!l) return -1;
    len[used[i]] = (uint8_t)l;
  }
  return m;
}

int wreath_gzip_encoder_huff_lengths(const uint32_t *freq, int nsym, int maxlen, uint8_t *len) {
  uint32_t used[GZ_HUFF_MAXSYM], scratch[GZ_HUFF_MAXSYM];
  int m = 0;
  for (int i = 0; i < nsym; i++) {
    len[i] = 0;
    if (freq[i]) used[m++] = (uint32_t)i;
  }
  if (m == 0) return 0;
  if (m == 1) {
    /* A one-symbol alphabet still needs a one-bit code: an empty code is not
     * representable and decoders (rightly) reject an over-subscribed table. */
    len[used[0]] = 1;
    return 1;
  }
  if (maxlen < 1 || (maxlen < 30 && (1u << maxlen) < (unsigned)m)) return -1;
  sort_by_freq(freq, used, scratch, m);

  /* Nodes 0..m-1 are leaves in ascending frequency; m..2m-2 are internal.
   * Both queues are consumed in order, so the tree is built without a heap. */
  uint64_t nf[2 * GZ_HUFF_MAXSYM];
  uint16_t lc[2 * GZ_HUFF_MAXSYM], rc[2 * GZ_HUFF_MAXSYM];
  uint8_t depth[2 * GZ_HUFF_MAXSYM];
  for (int i = 0; i < m; i++) nf[i] = freq[used[i]];
  int leaf = 0, node = m, next = m;
  while (next < 2 * m - 1) {
    /* Ties prefer a leaf, which keeps the tree shallower. */
    int x = (leaf < m && (node >= next || nf[leaf] <= nf[node])) ? leaf++ : node++;
    int y = (leaf < m && (node >= next || nf[leaf] <= nf[node])) ? leaf++ : node++;
    nf[next] = nf[x] + nf[y];
    lc[next] = (uint16_t)x;
    rc[next] = (uint16_t)y;
    next++;
  }
  int root = next - 1;
  depth[root] = 0;
  unsigned deepest = 0;
  for (int j = root; j >= m; j--) {
    uint8_t d = (uint8_t)(depth[j] + 1);
    depth[lc[j]] = d;
    depth[rc[j]] = d;
    if (d > deepest) deepest = d;
  }

  if (deepest > (unsigned)maxlen) {
    return package_merge(freq, used, m, maxlen, len);
  }
  for (int i = 0; i < m; i++) len[used[i]] = depth[i];
  return m;
}

void wreath_gzip_encoder_huff_codes(const uint8_t *len, int nsym, uint16_t *code) {
  uint16_t nextcode[16];
  int cnt[16];
  memset(cnt, 0, sizeof cnt);
  for (int i = 0; i < nsym; i++) cnt[len[i]]++;
  cnt[0] = 0;
  uint16_t c = 0;
  for (int l = 1; l < 16; l++) {
    c = (uint16_t)((c + cnt[l - 1]) << 1);
    nextcode[l] = c;
  }
  for (int i = 0; i < nsym; i++) {
    int l = len[i];
    if (!l) { code[i] = 0; continue; }
    unsigned v = nextcode[l]++;
    /* DEFLATE packs Huffman codes most-significant-bit first while the bit
     * writer appends least-significant-bit first, so store them reversed. */
    v = ((v & 0x5555u) << 1) | ((v >> 1) & 0x5555u);
    v = ((v & 0x3333u) << 2) | ((v >> 2) & 0x3333u);
    v = ((v & 0x0f0fu) << 4) | ((v >> 4) & 0x0f0fu);
    v = ((v & 0x00ffu) << 8) | ((v >> 8) & 0x00ffu);
    code[i] = (uint16_t)(v >> (16 - l));
  }
}

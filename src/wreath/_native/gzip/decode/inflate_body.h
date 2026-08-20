/* The budgeted inner loop of the inflate fast lane.
 *
 * SPDX-License-Identifier: MPL-2.0
 *
 * Included with GZ_CHECKDIST 1 and again with 0, inside each of the plain
 * (GZ_FUSED 0) and fused (GZ_FUSED 1) loop functions of each ISA arm. A decoded
 * distance is at most 24577 + 8191 = 32768, so once 32768 bytes have been
 * written the "distance reaches before the start of the output" test can never
 * fire and three instructions per match go away. Both choices are made once per
 * budget window, never per symbol -- adding one well-predicted branch per
 * symbol to this loop was measured at 2.3% by arm 3, which is more than either
 * specialisation is worth.
 *
 * The literal slots are arm 1's shape: one table load, one sign test, one
 * store. Subtables, end-of-block and invalid codes all sit behind that single
 * not-taken branch, so a literal never pays for them, and because GZ_K_MATCH is
 * zero a length code costs exactly one more test-and-branch.
 */
  while (budget--) {
    uint32_t e;
#if GZ_PREFETCH_OUT
    /* Aimed straight at the cache axis: the output is written strictly
     * forwards, so the line GZ_PREFETCH_OUT bytes ahead is one the loop is
     * certain to store into. Measured in RESULTS.md. */
    __builtin_prefetch(op + GZ_PREFETCH_OUT, 1, 0);
#endif
    GZ_REFILL();

#if GZ_FUSED
    {
      uint64_t f = fu[GZ_ROOT(bb, lroot, lmask)];
      if (f & GZ_F_N) {
        GZ_EMIT(f);
        f = fu[GZ_ROOT(bb, lroot, lmask)];
        if (f & GZ_F_N) {
          GZ_EMIT(f);
          f = fu[GZ_ROOT(bb, lroot, lmask)];
          if (f & GZ_F_N) {
            GZ_EMIT(f);
            continue;
          }
        }
      }
      e = (uint32_t)(f >> 32);
    }
#else
    /* The literal chain: GZ_LIT_CHAIN literals may be taken before the loop
     * goes back for a refill, and the innermost one `continue`s instead of
     * falling into the match path.
     *
     * The cap is a bit budget, and it is tighter than it looks. Only a code no
     * longer than the root reaches this chain at all -- a longer one is a
     * GZ_K_SUB entry, which is not a literal and leaves the chain -- so each
     * step here consumes at most `lroot` bits, not 15. A refill leaves cnt >=
     * 56; falling through to the match path then needs at most 15 for the
     * length code and 5 for its extra bits, so at most floor((56-20)/lroot)
     * literals may precede a fall-through, and one more may be taken if it
     * loops back for a refill. At lroot = 10 or 11 that is 3 + 1, which is why
     * 4 is the largest chain this budget admits. */
    GZ_LITCHAIN
#endif

    if (e & GZ_K_MASK) {
      if ((e & GZ_K_MASK) != GZ_K_SUB) {
        if ((e & GZ_K_MASK) == GZ_K_EOB) {
          GZ_DROP(e);
          ret = 1;
        } else {
          ret = GZ_ERR_DATA;
        }
        goto out;
      }
      bb >>= lroot;
      cnt -= lroot;
      e = lt[GZ_E_VAL(e) + (size_t)GZ_LOW(bb, GZ_E_XB(e))];
      if (GZ_IS_LIT(e)) {
        *op++ = GZ_E_LITBYTE(e);
        GZ_DROP(e);
        continue;
      }
      if (e & GZ_K_MASK) {
        if ((e & GZ_K_MASK) == GZ_K_EOB) {
          GZ_DROP(e);
          ret = 1;
        } else {
          ret = GZ_ERR_DATA;
        }
        goto out;
      }
    }
    {
      uint32_t xb, len, de, dist;
      GZ_DROP(e);
      xb = GZ_E_XB(e);
      len = GZ_E_VAL(e) + (uint32_t)GZ_LOW(bb, xb);
      bb >>= xb;
      cnt -= xb;

      /* A distance code plus its extra bits is at most 15 + 13 = 28 bits. When
       * the match was the iteration's first symbol there is still more than
       * that buffered, and the test is cheaper than the refill it skips. */
      if (GZ_CNT < GZ_DIST_REFILL) GZ_REFILL();

      de = dt[GZ_ROOT(bb, droot, dmask)];
      if (__builtin_expect((de & GZ_K_MASK) != 0, 0)) {
        if ((de & GZ_K_MASK) != GZ_K_SUB) {
          ret = GZ_ERR_DATA;
          goto out;
        }
        bb >>= droot;
        cnt -= droot;
        de = dt[GZ_E_VAL(de) + (size_t)GZ_LOW(bb, GZ_E_XB(de))];
        if (de & GZ_K_MASK) {
          ret = GZ_ERR_DATA;
          goto out;
        }
      }
      GZ_DROP(de);
      xb = GZ_E_XB(de);
      dist = GZ_E_VAL(de) + (uint32_t)GZ_LOW(bb, xb);
      bb >>= xb;
      cnt -= xb;
#if GZ_CHECKDIST
      if (__builtin_expect((size_t)dist > (size_t)(op - obase), 0)) {
        ret = GZ_ERR_DATA;
        goto out;
      }
#endif
      GZFN(wreath_gzip_decoder_copy_)(op, dist, len);
      op += len;
    }
  }
